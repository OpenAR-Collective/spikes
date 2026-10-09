using System.Reflection;
using System.Text.Json.Nodes;
using HiveAR;
using Marten.Services;
using Npgsql;
using Wax.Commands;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

// Ported from the Python side's tests/test_event_store.py.
public sealed class EventStoreTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Placing_an_account_writes_event_and_state_together()
    {
        var accountPk = await scenario.Place();
        var placed = Assert.Single(await scenario.LoadStream(accountPk));
        Assert.Equal("account.placed", placed.Type);
        Assert.Equal(1, placed.Version);
        Assert.Null(placed.PriorValue);
        Assert.Null(placed.PriorEventHash);
        Scenario.JsonEqual("""{"principal": 100000, "interest": 5000}""", placed.NewValue!["balance_buckets"]);
        Assert.Equal(1, await scenario.Scalar<int>("SELECT aggregate_sequence_number FROM ar.account WHERE account_pk = $1", accountPk));
    }

    [Fact]
    public void State_cannot_be_written_outside_a_command()
    {
        // The Python side checks this at run time. In C# it does not compile:
        // state rows are saved only through a CommandContext, which only the
        // dispatcher can construct, and the save methods are not public.
        Assert.Empty(typeof(CommandContext).GetConstructors(BindingFlags.Instance | BindingFlags.Public));
        var insert = typeof(AggregateRoot).GetMethod("QueueInsert", BindingFlags.Instance | BindingFlags.NonPublic)!;
        Assert.False(insert.IsPublic);
    }

    [Theory]
    [InlineData("UPDATE core.mt_events SET type = 'x'")]
    [InlineData("DELETE FROM core.mt_events")]
    public async Task Events_are_immutable_in_the_database(string statement)
    {
        await scenario.Place();
        var error = await Assert.ThrowsAsync<PostgresException>(() => scenario.Execute(statement));
        Assert.Contains("append-only", error.MessageText);
    }

    [Fact]
    public async Task Append_rejects_a_stale_expected_version()
    {
        var accountPk = await scenario.Place();
        await using var connection = await db.Engine.DataSource.OpenConnectionAsync();
        await using var transaction = await connection.BeginTransactionAsync();
        await using var session = db.Engine.Store.LightweightSession(SessionOptions.ForTransaction(transaction, shouldAutoCommit: false));
        var context = new CommandContext(
            connection,
            transaction,
            session,
            new Attribution(Scenario.Actor, "system", DateTimeOffset.UtcNow, null, null, null),
            await ReasonCodes.LoadAsync(db.Engine.DataSource));
        var stale = new AccountState
        {
            Pk = accountPk,
            AggregateSequenceNumber = 0,
            ClientAccountNumber = "x",
            DebtType = "x",
            ContractType = "purchase",
            CurrencyCode = "USD",
            ListDate = new DateOnly(2026, 1, 1),
            ItemizationDate = new DateOnly(2026, 1, 1),
            InterestAccruedThrough = new DateOnly(2026, 1, 1),
        };
        await Assert.ThrowsAsync<WaxConcurrencyException>(() =>
            context.RecordAsync<AccountPlaced>(stale, new JsonObject { ["status"] = "active" }, new JsonObject { ["status"] = "active" }, "account_placed").AsTask());
    }

    [Fact]
    public async Task Hash_chain_verifies_and_detects_a_forged_link()
    {
        var accountPk = await scenario.Place();
        await scenario.Post([accountPk], 2_500);
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk));

        // Forge a third event whose prior hash points nowhere.
        var body = new JsonObject
        {
            ["aggregate_type"] = "account",
            ["schema_version"] = 1,
            ["occurred_at"] = "2026-10-08T00:00:00+00:00",
            ["actor_pk_ref"] = Scenario.Actor.ToString(),
            ["actor_type"] = "system",
        };
        await scenario.Execute(
            "INSERT INTO core.mt_events (seq_id, id, stream_id, version, data, type, mt_dotnet_type, headers) " +
            "VALUES (nextval('core.mt_events_sequence'), gen_random_uuid(), $1, 3, $2::jsonb, 'account.forged', 'forged', $3::jsonb)",
            accountPk,
            body.ToJsonString(),
            """{"prior_event_hash": "not-the-real-hash", "event_hash": "made-up"}""");
        var problems = await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk);
        Assert.Contains(problems, problem => problem.Contains("prior_event_hash"));
        Assert.Contains(problems, problem => problem.Contains("event_hash does not match"));
    }

    [Fact]
    public async Task Hashes_match_the_python_side_byte_for_byte()
    {
        var vectors = JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(AppContext.BaseDirectory, "hash-vectors.json")))!.AsArray();
        Assert.NotEmpty(vectors);
        foreach (var vector in vectors)
        {
            var computed = Wax.Hashing.EventHasher.Compute(new Wax.Hashing.EventHashInput(
                vector!["aggregate_type"]!.GetValue<string>(),
                Guid.Parse(vector["aggregate_pk_ref"]!.GetValue<string>()),
                vector["event_type"]!.GetValue<string>(),
                vector["schema_version"]!.GetValue<int>(),
                vector["prior_value"] as JsonObject,
                vector["new_value"] as JsonObject,
                DateTimeOffset.Parse(vector["occurred_at"]!.GetValue<string>(), System.Globalization.CultureInfo.InvariantCulture),
                Guid.Parse(vector["actor_pk_ref"]!.GetValue<string>()),
                vector["actor_type"]!.GetValue<string>(),
                vector["reason_code_fk"]?.GetValue<string>() is { } reason ? Guid.Parse(reason) : null,
                vector["prior_event_hash"]?.GetValue<string>()));
            Assert.True(vector["expected_hash"]!.GetValue<string>() == computed, $"vector {vector["name"]}");
        }
    }
}
