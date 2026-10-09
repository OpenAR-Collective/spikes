using System.Text.Json.Nodes;
using Npgsql;
using Wax.Events;
using Wax.Hashing;
using Wax.Store;

namespace Wax.Commands;

/// <summary>
/// What a command handler works through: the command's one connection and
/// transaction for its reads and row locks, and a queue of writes (events and
/// state) that go out as plain SQL in a single batch when the handler returns.
/// </summary>
public sealed class CommandContext
{
    readonly WriteQueue writes;
    readonly ReasonCodes reasons;
    readonly Dictionary<Guid, StreamHead> heads = [];

    internal CommandContext(
        NpgsqlConnection connection,
        NpgsqlTransaction transaction,
        WriteQueue writes,
        Attribution attribution,
        ReasonCodes reasons)
    {
        Connection = connection;
        Transaction = transaction;
        this.writes = writes;
        Attribution = attribution;
        this.reasons = reasons;
    }

    public NpgsqlConnection Connection { get; }

    public NpgsqlTransaction Transaction { get; }

    public Attribution Attribution { get; }

    public DateTimeOffset OccurredAt => Attribution.OccurredAt;

    public NpgsqlBatch CreateBatch() => new(Connection, Transaction);

    public NpgsqlCommand CreateCommand(string sql) => new(sql, Connection, Transaction);

    /// <summary>Queue a state write. It runs in the save batch, inside this transaction.</summary>
    public void QueueSql(string sql, params object[] values) => writes.Sql(sql, values);

    /// <summary>A batch command that reads the head of each named stream, so a
    /// handler can fetch heads in the same round trip as its row locks.</summary>
    public static NpgsqlBatchCommand StreamHeadsCommand(IEnumerable<Guid> streamIds) =>
        StreamHeads.Command(streamIds.Distinct().ToArray());

    /// <summary>Read the result of <see cref="StreamHeadsCommand"/>.</summary>
    public async Task ReadStreamHeadsAsync(NpgsqlDataReader reader, IEnumerable<Guid> requested, CancellationToken cancellation)
    {
        foreach (var streamId in requested)
        {
            heads[streamId] = StreamHead.Empty;
        }
        while (await reader.ReadAsync(cancellation))
        {
            var (streamId, head) = StreamHeads.Read(reader);
            heads[streamId] = head;
        }
    }

    public async Task LoadStreamHeadsAsync(IReadOnlyCollection<Guid> streamIds, CancellationToken cancellation)
    {
        await using var batch = CreateBatch();
        batch.BatchCommands.Add(StreamHeadsCommand(streamIds));
        await using var reader = await batch.ExecuteReaderAsync(cancellation);
        await ReadStreamHeadsAsync(reader, streamIds, cancellation);
    }

    /// <summary>
    /// Write one event for an aggregate root and save its state row. The event
    /// and the state share the handler's transaction, which is the atomicity
    /// invariant in WAX-0001; the hash is computed here, before the write, as
    /// WAX-0013 requires. The caller holds the row lock on the state row.
    /// </summary>
    public ValueTask RecordAsync<TEvent>(
        AggregateRoot aggregate,
        JsonObject? prior,
        JsonObject? @new,
        string reason,
        JsonObject? metadata = null,
        CancellationToken cancellation = default)
        where TEvent : WaxEvent, new() =>
        RecordAsync(typeof(TEvent), aggregate, prior, @new, reason, metadata, cancellation);

    /// <summary>The same, for an event type chosen at run time.</summary>
    public async ValueTask RecordAsync(
        Type eventType,
        AggregateRoot aggregate,
        JsonObject? prior,
        JsonObject? @new,
        string reason,
        JsonObject? metadata = null,
        CancellationToken cancellation = default)
    {
        if (!EventHashing.Enabled)
        {
            // The hashing-off diagnostic: no head was read, so the locked state
            // row's version is trusted; the unique index is the backstop.
            heads[aggregate.Pk] = new StreamHead(aggregate.AggregateSequenceNumber, null);
        }
        if (!heads.TryGetValue(aggregate.Pk, out var head))
        {
            if (aggregate.IsNew && aggregate.AggregateSequenceNumber == 0)
            {
                // A new aggregate has no stream yet. If one exists after all,
                // the unique index refuses a second first event and the
                // command is retried.
                head = StreamHead.Empty;
            }
            else
            {
                await LoadStreamHeadsAsync([aggregate.Pk], cancellation);
                head = heads[aggregate.Pk];
            }
        }
        if (head.Version != aggregate.AggregateSequenceNumber)
        {
            throw new WaxConcurrencyException(
                $"{aggregate.AggregateType} {aggregate.Pk}: expected version {aggregate.AggregateSequenceNumber}, found {head.Version}");
        }

        var reasonCode = await reasons.ResolveAsync(reason, cancellation);
        var appended = NewEvent.Create(eventType, aggregate.AggregateType, aggregate.Pk, head, prior, @new, metadata, Attribution, reasonCode);
        writes.Append(appended);

        heads[aggregate.Pk] = new StreamHead(appended.AggregateSequenceNumber, appended.EventHash);
        aggregate.AggregateSequenceNumber = appended.AggregateSequenceNumber;
        if (aggregate.IsNew && !aggregate.Inserted)
        {
            aggregate.QueueInsert(this);
            aggregate.Inserted = true;
        }
        else
        {
            aggregate.QueueUpdate(this);
        }
    }
}
