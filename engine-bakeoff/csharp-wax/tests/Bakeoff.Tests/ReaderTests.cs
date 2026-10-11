// Adapted from the HiveAR prototype by Andy Troiano
// (https://github.com/atroiano/platform-design, commit 361b6aa,
// prototype/tests/test_projection_reader.py), Apache License 2.0.
// Modified by The OpenAR Collective contributors.
// Changes: ported to C# and xunit against the C# port of the reader. A wait
// that depends on the high-water mark settling retries for a few seconds,
// because the mark waits on the oldest transaction holding a transaction ID
// anywhere on the server and other test classes run at the same time. The
// test that the reader refuses to run inside another transaction has no
// counterpart: the C# reader opens its own connections and cannot be handed
// one. Added: two readers sharing a projection apply each event once.

using System.Text.Json.Nodes;
using Npgsql;
using Wax.Projections;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>
/// The reader in Wax.Projections: no skipped event, per-aggregate order, one
/// failing projection does not stop the others, an unknown event type stops a
/// projection until a release knows it, and the mark is forgotten when the
/// database goes back in time.
/// </summary>
public sealed class ReaderTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    static readonly TimeSpan Patience = TimeSpan.FromSeconds(15);

    /// <summary>A throwaway projection that records the labels it is handed,
    /// once each, in the order it first saw them.</summary>
    sealed class Capture
    {
        public List<string> Seen { get; } = [];

        public Func<string, bool> Poisoned { get; set; } = _ => false;

        public Projection Build(string name, string[]? handles = null, string[]? ignores = null)
        {
            var projection = new Projection(name, 1, ["reader_test"]);
            foreach (var eventType in handles ?? ["reader.test"])
            {
                projection.On(eventType, Handle);
            }
            foreach (var eventType in ignores ?? [])
            {
                projection.Ignore(eventType);
            }
            return projection;
        }

        void Handle(Wax.Events.StoredEvent e, ProjectionWrites writes)
        {
            var label = e.NewValue!["label"]!.GetValue<string>();
            if (Poisoned(label))
            {
                throw new InvalidOperationException("cannot handle this one\0");
            }
            if (!Seen.Contains(label))
            {
                Seen.Add(label);
            }
        }
    }

    static string NewName(string kind) => $"test.{kind}_{Guid.NewGuid():N}"[..24];

    /// <summary>A reader over these projections, each starting from the present:
    /// earlier tests' events are not this test's business.</summary>
    async Task<ProjectionReader> Reader(params Projection[] projections)
    {
        var reader = new ProjectionReader(db.Engine.DataSource, projections);
        await reader.RegisterCheckpointsAsync();
        foreach (var projection in projections)
        {
            await AtPresent(projection.Name);
        }
        return reader;
    }

    Task AtPresent(string name) => new Scenario(db).Execute(
        $"UPDATE core.projection_checkpoint SET last_global_sequence_number = ({EventStore.PositionSql}) WHERE projection_name = $1", name);

    static async Task Write(NpgsqlConnection connection, NpgsqlTransaction? transaction, Guid aggregate, int sequence, string label, string eventType = "reader.test")
    {
        await using var command = new NpgsqlCommand(
            "INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, " +
            "aggregate_type, occurred_at, actor_pk_ref, actor_type, new_value, prior_event_hash, event_hash) " +
            "VALUES (gen_random_uuid(), $1, $2, 1, $3, 'reader_test', now(), $4, 'system', $5::jsonb, $6, $7)",
            connection, transaction);
        command.Parameters.AddWithValue(sequence);
        command.Parameters.AddWithValue(eventType);
        command.Parameters.AddWithValue(aggregate);
        command.Parameters.AddWithValue(Scenario.Actor);
        command.Parameters.AddWithValue(new JsonObject { ["label"] = label }.ToJsonString());
        command.Parameters.AddWithValue(sequence == 1 ? DBNull.Value : "x");
        command.Parameters.AddWithValue($"hash-{label}");
        await command.ExecuteNonQueryAsync();
    }

    async Task Write(Guid aggregate, int sequence, string label, string eventType = "reader.test")
    {
        await using var connection = await db.Engine.DataSource.OpenConnectionAsync();
        await Write(connection, null, aggregate, sequence, label, eventType);
    }

    /// <summary>Run passes until done holds, or fail after a while.</summary>
    static async Task Drain(ProjectionReader reader, Func<Task<bool>> done, string what)
    {
        var deadline = DateTime.UtcNow + Patience;
        while (true)
        {
            while (await reader.RunOnceAsync() > 0)
            {
            }
            if (await done())
            {
                return;
            }
            if (DateTime.UtcNow > deadline)
            {
                Assert.Fail($"timed out waiting until {what}");
            }
            await Task.Delay(50);
        }
    }

    static Task Drain(ProjectionReader reader, Func<bool> done, string what) => Drain(reader, () => Task.FromResult(done()), what);

    /// <summary>Run passes for a moment, for a test that expects nothing to happen.</summary>
    static async Task Idle(ProjectionReader reader)
    {
        for (var pass = 0; pass < 5; pass++)
        {
            await reader.RunOnceAsync();
            await Task.Delay(50);
        }
    }

    static async Task<ProjectionStatus> Row(ProjectionReader reader, string name) =>
        (await reader.StatusAsync()).Single(row => row.Projection == name);

    [Fact]
    public async Task An_uncommitted_lower_sequence_is_never_skipped()
    {
        var capture = new Capture();
        var reader = await Reader(capture.Build(NewName("capture")));
        await using var second = await db.Engine.DataSource.OpenConnectionAsync();
        await using var open = await second.BeginTransactionAsync();
        await Write(second, open, Guid.NewGuid(), 1, "slow");   // takes the lower sequence number, stays open
        await Write(Guid.NewGuid(), 1, "fast");                 // higher number, already committed

        await Idle(reader);
        Assert.True(capture.Seen.Count == 0, "reading past the open transaction would lose its event: saw " + string.Join(", ", capture.Seen));

        await open.CommitAsync();
        await Drain(reader, () => capture.Seen.Count == 2, "both events are seen");
        Assert.Equal(["slow", "fast"], capture.Seen);
    }

    [Fact]
    public async Task One_aggregate_is_delivered_in_its_own_order()
    {
        // The transaction that started first writes the aggregate's later event.
        var capture = new Capture();
        var reader = await Reader(capture.Build(NewName("capture")));
        var aggregate = Guid.NewGuid();
        await using var second = await db.Engine.DataSource.OpenConnectionAsync();
        await using var older = await second.BeginTransactionAsync();
        await Write(second, older, Guid.NewGuid(), 1, "unrelated");   // the older transaction id
        await Write(aggregate, 1, "first");                           // commits first
        await Write(second, older, aggregate, 2, "second");           // written after, by the older transaction
        await older.CommitAsync();

        await Drain(reader, () => capture.Seen.Count == 3, "all three events are seen");
        Assert.True(capture.Seen.IndexOf("first") < capture.Seen.IndexOf("second"));
    }

    [Fact]
    public async Task A_burned_sequence_number_does_not_leave_the_projection_behind()
    {
        // A rolled-back insert uses up a number no event will ever carry.
        var capture = new Capture();
        var projection = capture.Build(NewName("capture"));
        var reader = await Reader(projection);
        await Write(Guid.NewGuid(), 1, "kept");
        await using (var second = await db.Engine.DataSource.OpenConnectionAsync())
        await using (var discarded = await second.BeginTransactionAsync())
        {
            await Write(second, discarded, Guid.NewGuid(), 1, "discarded");
            await discarded.RollbackAsync();
        }

        var burned = await reader.SequencePositionAsync();
        await Drain(reader, async () => (await Row(reader, projection.Name)).Position >= burned, "the checkpoint passes the burned number");
        Assert.Equal(["kept"], capture.Seen);
    }

    [Fact]
    public async Task A_failing_projection_stops_alone_and_resumes()
    {
        var healthy = new Capture();
        var broken = new Capture();
        var poisoned = true;
        broken.Poisoned = label => poisoned && label == "bad";
        var brokenProjection = broken.Build(NewName("broken"));
        var reader = await Reader(healthy.Build(NewName("capture")), brokenProjection);

        var aggregate = Guid.NewGuid();
        await Write(aggregate, 1, "good");
        await Write(aggregate, 2, "bad");
        await Write(aggregate, 3, "after");
        await Drain(reader, () => healthy.Seen.Count == 3, "the healthy projection sees all three");

        Assert.Equal(["good", "bad", "after"], healthy.Seen);
        Assert.Equal(["good"], broken.Seen);   // it must stop at the event, not skip it
        var state = await Row(reader, brokenProjection.Name);
        Assert.Equal("handler_error", state.FailureKind);
        Assert.Contains("cannot handle", state.FailureDetail);

        poisoned = false;
        await reader.RetryAsync(brokenProjection.Name);
        await Drain(reader, () => broken.Seen.Count == 3, "the repaired projection catches up");
        Assert.Equal(["good", "bad", "after"], broken.Seen);
    }

    [Fact]
    public async Task An_unknown_event_type_stops_the_projection_until_a_release_that_knows_it()
    {
        // Skipping an event that changed state would leave the read model wrong
        // with no sign. An older release stops; a newer one picks the event up.
        var name = NewName("versioned");
        var older = new Capture();
        var olderReader = await Reader(older.Build(name));
        var aggregate = Guid.NewGuid();
        await Write(aggregate, 1, "known");
        await Write(aggregate, 2, "adjusted", "reader.adjusted");
        await Write(aggregate, 3, "later");

        await Drain(olderReader, async () => (await Row(olderReader, name + ".v1")).FailedAt is not null, "the older release stops");
        Assert.Equal(["known"], older.Seen);
        Assert.Equal("unknown_event_type", (await Row(olderReader, name + ".v1")).FailureKind);

        // The next release handles the new type. Its definition differs, so it
        // may not touch the old version's rows until it is adopted as the definition.
        var newer = new Capture();
        var newerReader = new ProjectionReader(db.Engine.DataSource, [newer.Build(name, handles: ["reader.test", "reader.adjusted"])]);
        await Idle(newerReader);
        Assert.Empty(newer.Seen);
        Assert.False((await Row(newerReader, name + ".v1")).DefinitionMatches);

        await newerReader.AcceptDefinitionAsync(name + ".v1");
        await Drain(newerReader, () => newer.Seen.Count == 2, "the newer release picks the event up");
        Assert.Equal(["adjusted", "later"], newer.Seen);
        Assert.Null((await Row(newerReader, name + ".v1")).FailedAt);
    }

    [Fact]
    public async Task An_ignored_event_type_is_passed_over_on_purpose()
    {
        var capture = new Capture();
        var projection = capture.Build(NewName("capture"), ignores: ["reader.noise"]);
        var reader = await Reader(projection);
        var aggregate = Guid.NewGuid();
        await Write(aggregate, 1, "noise", "reader.noise");
        await Write(aggregate, 2, "signal");

        await Drain(reader, () => capture.Seen.Count == 1, "the signal is seen");
        Assert.Equal(["signal"], capture.Seen);
        Assert.Null((await Row(reader, projection.Name)).FailedAt);
    }

    [Fact]
    public async Task The_mark_is_forgotten_when_the_database_goes_back_in_time()
    {
        // After a restore or a failover the sequence can sit below a mark this
        // reader already trusted. Reading on the old mark would skip events.
        var mark = new HighWaterMark(db.Engine.DataSource);
        var position = await new ProjectionReader(db.Engine.DataSource, []).SequencePositionAsync();
        var deadline = DateTime.UtcNow + Patience;
        while (await mark.AdvanceAsync() < position)
        {
            Assert.True(DateTime.UtcNow < deadline, "the mark never settled");
            await Task.Delay(50);
        }

        mark.Value = position + 1_000;   // as if the store had been ahead
        mark.LastSampled = position + 1_000;
        Assert.True(await mark.AdvanceAsync() <= await new ProjectionReader(db.Engine.DataSource, []).SequencePositionAsync());
    }

    [Fact]
    public async Task Two_readers_sharing_a_projection_apply_each_event_once()
    {
        // run-engine may start several readers; they share each projection
        // through its checkpoint row, claimed with SKIP LOCKED. A table with a
        // unique key catches any event applied twice: the projection would stop.
        await new Scenario(db).Execute("CREATE TABLE IF NOT EXISTS reader_test_applied (label text PRIMARY KEY)");
        var projection = new Projection(NewName("shared"), 1, ["reader_test"])
            .On("reader.test", (e, writes) => writes.Sql("INSERT INTO reader_test_applied (label) VALUES (?)", e.NewValue!["label"]!.GetValue<string>()));
        var first = await Reader(projection);
        var second = new ProjectionReader(db.Engine.DataSource, [projection], batchSize: 7);

        var prefix = $"{Guid.NewGuid():N}"[..8];
        await Task.WhenAll(Enumerable.Range(0, 4).Select(writer => Task.Run(async () =>
        {
            for (var i = 0; i < 50; i++)
            {
                await Write(Guid.NewGuid(), 1, $"{prefix}-{writer}-{i}");
            }
        })));
        var target = await first.SequencePositionAsync();

        var deadline = DateTime.UtcNow + Patience;
        while ((await Row(first, projection.Name)).Position < target)
        {
            Assert.True(DateTime.UtcNow < deadline, "the readers did not catch up");
            await Task.WhenAll(first.RunOnceAsync(), second.RunOnceAsync());
        }
        Assert.Null((await Row(first, projection.Name)).FailedAt);
        Assert.Equal(200L, await new Scenario(db).Scalar<long>("SELECT count(*) FROM reader_test_applied WHERE label LIKE $1", prefix + "-%"));
    }
}
