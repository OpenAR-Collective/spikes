using Npgsql;
using Wax;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>
/// The plan-choice read the bench makes after its timer: pg_prepared_statements
/// shows each session only its own statements, so the read must reach every
/// connection the pool holds, and what it finds depends on the reset Npgsql
/// gives a connection returned to the pool.
/// </summary>
public sealed class PlanChoicesTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    const string Sql = "SELECT count(*) FROM core.event WHERE global_sequence_number > $1";

    NpgsqlDataSource Source(bool noResetOnClose, int maxAutoPrepare, string? options = null) => NpgsqlDataSource.Create(
        new NpgsqlConnectionStringBuilder(db.ConnectionString)
        {
            // Its own application name, so the count of open connections sees
            // only this data source's, not the fixture's.
            ApplicationName = $"plan-choices-{Guid.NewGuid():N}"[..30],
            NoResetOnClose = noResetOnClose,
            MaxAutoPrepare = maxAutoPrepare,
            AutoPrepareMinUsages = 2,
            Options = options,
        }.ConnectionString);

    static async Task Run(NpgsqlConnection connection, int times, bool prepare)
    {
        await using var command = new NpgsqlCommand(Sql, connection);
        command.Parameters.Add(new NpgsqlParameter { Value = 0L });
        if (prepare)
        {
            await command.PrepareAsync();
        }
        for (var i = 0; i < times; i++)
        {
            await command.ExecuteScalarAsync();
        }
    }

    [Fact]
    public async Task Every_pooled_connection_is_read_once()
    {
        await using var source = Source(noResetOnClose: true, maxAutoPrepare: 0);
        await using (var first = await source.OpenConnectionAsync())
        await using (var second = await source.OpenConnectionAsync())
        {
            await Run(first, 7, prepare: true);
            await Run(second, 3, prepare: true);
        }

        var connections = await PlanChoices.ReadAsync(source);

        Assert.Equal(2, connections.Count);
        Assert.Equal(2, connections.Select(connection => connection.Pid).Distinct().Count());
        var statements = connections
            .Select(connection => Assert.Single(connection.Statements, statement => statement.Text == Sql))
            .ToList();
        Assert.Equal([3L, 7L], statements.Select(statement => statement.GenericPlans + statement.CustomPlans).Order());
        // The same text on both connections, so the same hash of it.
        Assert.Single(statements.Select(statement => statement.Hash).Distinct());
        Assert.Matches("^[0-9a-f]{8}$", statements[0].Hash);
    }

    [Fact]
    public async Task The_lighter_reset_keeps_what_auto_prepare_prepared()
    {
        // Max Auto Prepare alone: Npgsql resets a returned connection, but once
        // it has a statement prepared the reset leaves out DEALLOCATE ALL.
        // Three uses in the first session prepare the statement; three more
        // sessions on the same pooled connection then follow resets.
        await using var source = Source(noResetOnClose: false, maxAutoPrepare: 20);
        for (var i = 0; i < 4; i++)
        {
            await using var connection = await source.OpenConnectionAsync();
            await Run(connection, i == 0 ? 3 : 1, prepare: false);
        }

        var read = Assert.Single(await PlanChoices.ReadAsync(source));

        var statement = Assert.Single(read.Statements, statement => statement.Text == Sql);
        Assert.InRange(statement.GenericPlans + statement.CustomPlans, 4, 6);
    }

    [Fact]
    public async Task Discard_all_leaves_nothing_to_read()
    {
        // Npgsql's defaults: a returned connection gets DISCARD ALL, which
        // deallocates its prepared statements, so an empty read is expected.
        await using var source = Source(noResetOnClose: false, maxAutoPrepare: 0);
        await using (var connection = await source.OpenConnectionAsync())
        {
            await Run(connection, 3, prepare: false);
        }

        var read = Assert.Single(await PlanChoices.ReadAsync(source));

        Assert.Equal(0, read.Prepared);
        Assert.Empty(read.Statements);
        Assert.NotEqual(0, read.Pid);
    }

    [Fact]
    public async Task Each_connection_reports_the_settings_its_session_runs_with()
    {
        // The diagnostic options reach every pooled connection's session, and
        // the read reports them, so a result shows what its bench ran with.
        await using var source = Source(noResetOnClose: true, maxAutoPrepare: 0,
            WaxSettings.SessionOptions("force_custom_plan", "off"));
        await using (var first = await source.OpenConnectionAsync())
        await using (var second = await source.OpenConnectionAsync())
        {
            await Run(first, 1, prepare: false);
            await Run(second, 1, prepare: false);
        }

        var connections = await PlanChoices.ReadAsync(source);

        Assert.Equal(2, connections.Count);
        Assert.All(connections, connection =>
        {
            Assert.Equal("off", connection.SynchronousCommit);
            Assert.Equal("force_custom_plan", connection.PlanCacheMode);
        });
    }

    [Fact]
    public async Task Without_options_a_session_runs_the_server_defaults()
    {
        await using var source = Source(noResetOnClose: true, maxAutoPrepare: 0);
        await using (var connection = await source.OpenConnectionAsync())
        {
            await Run(connection, 1, prepare: false);
        }

        var read = Assert.Single(await PlanChoices.ReadAsync(source));

        Assert.Equal("on", read.SynchronousCommit);
        Assert.Equal("auto", read.PlanCacheMode);
    }

    [Fact]
    public void Session_options_combine_and_refuse_what_they_do_not_know()
    {
        Assert.Null(WaxSettings.SessionOptions(null, null));
        Assert.Equal("-c plan_cache_mode=auto", WaxSettings.SessionOptions("auto", null));
        Assert.Equal("-c synchronous_commit=off", WaxSettings.SessionOptions(null, "off"));
        Assert.Equal("-c plan_cache_mode=force_custom_plan -c synchronous_commit=on",
            WaxSettings.SessionOptions("force_custom_plan", "on"));
        Assert.Throws<InvalidOperationException>(() => WaxSettings.SessionOptions(null, "local"));
        Assert.Throws<InvalidOperationException>(() => WaxSettings.SessionOptions("custom", null));
    }
}
