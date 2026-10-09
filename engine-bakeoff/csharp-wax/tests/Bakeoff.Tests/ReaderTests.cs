using System.Text.Json.Nodes;
using JasperFx.Events;
using JasperFx.Events.Daemon;
using JasperFx.Events.Projections;
using Marten;
using Marten.Events.Projections;
using Marten.Services;
using Npgsql;
using Wax.Commands;
using Wax.Events;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

[WaxEventType("reader.test")]
public sealed record ReaderTest : WaxEvent;

/// <summary>A throwaway projection that records the labels it is handed, once
/// each, in the order it first saw them.</summary>
sealed class CaptureProjection : EventProjection
{
    readonly string prefix;

    public CaptureProjection(string name, string prefix)
    {
        Name = name;
        this.prefix = prefix;
        IncludeType<ReaderTest>();
    }

    public List<string> Seen { get; } = [];

    public Func<string, bool> Poisoned { get; set; } = _ => false;

    public override ValueTask ApplyAsync(IDocumentOperations operations, IEvent e, CancellationToken cancellation)
    {
        var label = ((ReaderTest)e.Data).NewValue!["label"]!.GetValue<string>();
        if (!label.StartsWith(prefix, StringComparison.Ordinal))
        {
            return ValueTask.CompletedTask;
        }
        if (Poisoned(label))
        {
            throw new InvalidOperationException("cannot handle this one");
        }
        lock (Seen)
        {
            if (!Seen.Contains(label))
            {
                Seen.Add(label);
            }
        }
        return ValueTask.CompletedTask;
    }
}

/// <summary>
/// The Python side's tests/test_projection_reader.py, asked of Marten's async
/// daemon instead of the prototype's own reader: no event is skipped, each
/// aggregate's events arrive in its own order, a rolled-back sequence number
/// does not hold a projection back, and one failing projection stops alone.
/// </summary>
public sealed class ReaderTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    static readonly TimeSpan Patience = TimeSpan.FromSeconds(30);

    (DocumentStore Store, string Prefix) Build(params CaptureProjection[] projections)
    {
        var store = DocumentStore.For(options =>
        {
            WaxStoreSetup.Configure(options, db.Engine.DataSource, [], mayChangeSchema: true);
            options.Events.MapEventType<ReaderTest>(WaxEventTypes.NameOf<ReaderTest>());
            foreach (var projection in projections)
            {
                options.Projections.Add(projection, ProjectionLifecycle.Async);
            }
        });
        return (store, "");
    }

    static string NewPrefix() => $"{Guid.NewGuid():N}"[..8] + ":";

    async Task Write(DocumentStore store, NpgsqlTransaction? open, Guid aggregate, string label, bool start = true)
    {
        var body = new ReaderTest
        {
            AggregateType = "reader_test",
            NewValue = new JsonObject { ["label"] = label },
            OccurredAt = DateTimeOffset.UtcNow,
            ActorPkRef = Scenario.Actor,
            ActorType = "system",
        };
        if (db.Engine.Commands.AppendMode == AppendMode.Direct)
        {
            // The same plain SQL the direct append writes, so the daemon's gap
            // handling is tested against it too. No hash: the reader ignores it.
            var connection = open?.Connection ?? await db.Engine.DataSource.OpenConnectionAsync();
            var transaction = open ?? await connection.BeginTransactionAsync();
            long priorVersion = 0;
            if (!start)
            {
                await using var version = new NpgsqlCommand("SELECT version FROM core.mt_streams WHERE id = $1", connection, transaction);
                version.Parameters.AddWithValue(aggregate);
                priorVersion = (long)(await version.ExecuteScalarAsync())!;
            }
            await using (var writes = IWriteQueue.Create(AppendMode.Direct, connection, transaction, store))
            {
                writes.Append(aggregate, priorVersion, body, "not-checked", null);
                await writes.FlushAsync(CancellationToken.None);
            }
            if (open is null)
            {
                await transaction.CommitAsync();
                await connection.DisposeAsync();
            }
            return;
        }
        await using var session = open is null
            ? store.LightweightSession()
            : store.LightweightSession(SessionOptions.ForTransaction(open, shouldAutoCommit: false));
        if (start)
        {
            session.Events.StartStream(aggregate, body);
        }
        else
        {
            session.Events.Append(aggregate, body);
        }
        await session.SaveChangesAsync();
    }

    static async Task Eventually(Func<bool> condition, string what)
    {
        var deadline = DateTime.UtcNow + Patience;
        while (!condition())
        {
            if (DateTime.UtcNow > deadline)
            {
                Assert.Fail($"timed out waiting until {what}");
            }
            await Task.Delay(100);
        }
    }

    [Fact]
    public async Task An_uncommitted_lower_sequence_is_never_skipped()
    {
        var prefix = NewPrefix();
        var capture = new CaptureProjection("test_capture_" + prefix[..8], prefix);
        var (store, _) = Build(capture);
        using var _store = store;

        await using var slowConnection = await db.Engine.DataSource.OpenConnectionAsync();
        await using var slow = await slowConnection.BeginTransactionAsync();
        await Write(store, slow, Guid.NewGuid(), prefix + "slow");   // takes the lower sequence number, stays open
        await Write(store, null, Guid.NewGuid(), prefix + "fast");   // higher number, already committed

        using var daemon = await store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        // Longer than Marten's stale-sequence threshold, so a daemon that gave up
        // on the open transaction's number would have moved past it by now.
        await Task.Delay(TimeSpan.FromSeconds(8));
        Assert.True(capture.Seen.Count == 0, "reading past the open transaction would lose its event: saw " + string.Join(", ", capture.Seen));

        await slow.CommitAsync();
        await Eventually(() => capture.Seen.Count == 2, "both events are seen");
        Assert.Equal([prefix + "slow", prefix + "fast"], capture.Seen);
        await daemon.StopAllAsync();
    }

    [Fact]
    public async Task One_aggregate_is_delivered_in_its_own_order()
    {
        // The transaction that started first writes the aggregate's later event.
        var prefix = NewPrefix();
        var capture = new CaptureProjection("test_capture_" + prefix[..8], prefix);
        var (store, _) = Build(capture);
        using var _store = store;
        var aggregate = Guid.NewGuid();

        await using var olderConnection = await db.Engine.DataSource.OpenConnectionAsync();
        await using var older = await olderConnection.BeginTransactionAsync();
        await Write(store, older, Guid.NewGuid(), prefix + "unrelated");   // the older transaction
        await Write(store, null, aggregate, prefix + "first");              // commits first
        await Write(store, older, aggregate, prefix + "second", start: false);
        await older.CommitAsync();

        using var daemon = await store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        await Eventually(() => capture.Seen.Count == 3, "all three events are seen");
        Assert.True(capture.Seen.IndexOf(prefix + "first") < capture.Seen.IndexOf(prefix + "second"));
        await daemon.StopAllAsync();
    }

    [Fact]
    public async Task A_burned_sequence_number_does_not_leave_the_projection_behind()
    {
        // A rolled-back insert uses up a number no event will ever carry.
        var prefix = NewPrefix();
        var capture = new CaptureProjection("test_capture_" + prefix[..8], prefix);
        var (store, _) = Build(capture);
        using var _store = store;

        await Write(store, null, Guid.NewGuid(), prefix + "kept");
        await using (var connection = await db.Engine.DataSource.OpenConnectionAsync())
        await using (var discarded = await connection.BeginTransactionAsync())
        {
            await Write(store, discarded, Guid.NewGuid(), prefix + "discarded");
            await discarded.RollbackAsync();
        }
        await Write(store, null, Guid.NewGuid(), prefix + "after");

        using var daemon = await store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        await Eventually(() => capture.Seen.Count == 2, "the events either side of the gap are seen");
        Assert.Equal([prefix + "kept", prefix + "after"], capture.Seen);
        await daemon.StopAllAsync();
    }

    [Fact]
    public async Task A_failing_projection_stops_alone_and_resumes()
    {
        var prefix = NewPrefix();
        var healthy = new CaptureProjection("test_healthy_" + prefix[..8], prefix);
        var broken = new CaptureProjection("test_broken_" + prefix[..8], prefix);
        var poisoned = true;
        broken.Poisoned = label => poisoned && label == prefix + "bad";
        var (store, _) = Build(healthy, broken);
        using var _store = store;

        var aggregate = Guid.NewGuid();
        await Write(store, null, aggregate, prefix + "good");
        await Write(store, null, aggregate, prefix + "bad", start: false);
        await Write(store, null, aggregate, prefix + "after", start: false);

        using var daemon = await store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        await Eventually(() => healthy.Seen.Count == 3, "the healthy projection sees all three");
        await Task.Delay(TimeSpan.FromSeconds(3));
        Assert.Equal([prefix + "good", prefix + "bad", prefix + "after"], healthy.Seen);
        // It must stop at the event, not skip it.
        Assert.DoesNotContain(prefix + "after", broken.Seen);
        Assert.DoesNotContain(prefix + "bad", broken.Seen);

        poisoned = false;
        var shard = $"{broken.Name}:All";
        daemon.EjectPausedShard(shard);
        await daemon.StartAgentAsync(shard, CancellationToken.None);
        await Eventually(() => broken.Seen.Count == 3, "the repaired projection catches up");
        Assert.Equal([prefix + "good", prefix + "bad", prefix + "after"], broken.Seen);
        await daemon.StopAllAsync();
    }
}
