using System.Collections.Concurrent;
using System.Linq.Expressions;
using System.Reflection;
using System.Text.Json.Nodes;
using JasperFx.Events;
using Marten;
using Npgsql;
using Wax.Events;
using Wax.Hashing;
using Wax.Store;

namespace Wax.Commands;

/// <summary>
/// What a command handler works through: the command's one connection and
/// transaction for its reads and row locks, and a queue of writes (events and
/// state) that Marten sends in a single batch when the handler returns.
/// </summary>
public sealed class CommandContext
{
    readonly IDocumentSession session;
    readonly ReasonCodes reasons;
    readonly Dictionary<Guid, StreamHead> heads = [];

    // Marten names an event after the static type it is wrapped as, so each
    // body is wrapped as its own concrete type, never as WaxEvent.
    static readonly ConcurrentDictionary<Type, Func<WaxEvent, IEvent>> Wrappers = new();

    static IEvent Wrap(WaxEvent body) => Wrappers.GetOrAdd(body.GetType(), static type =>
    {
        var asEvent = typeof(Event).GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Single(method => method.Name == nameof(Event.AsEvent) && method.IsGenericMethodDefinition && method.GetParameters().Length == 1)
            .MakeGenericMethod(type);
        var parameter = Expression.Parameter(typeof(WaxEvent));
        return Expression.Lambda<Func<WaxEvent, IEvent>>(
            Expression.Convert(Expression.Call(asEvent, Expression.Convert(parameter, type)), typeof(IEvent)),
            parameter).Compile();
    })(body);

    internal CommandContext(
        NpgsqlConnection connection,
        NpgsqlTransaction transaction,
        IDocumentSession session,
        Attribution attribution,
        ReasonCodes reasons)
    {
        Connection = connection;
        Transaction = transaction;
        this.session = session;
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
    public void QueueSql(string sql, params object[] values) => session.QueueSqlCommand(sql, values);

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
            heads[reader.GetGuid(0)] = new StreamHead(reader.GetInt64(1), reader.IsDBNull(2) ? null : reader.GetString(2));
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
        if (!heads.TryGetValue(aggregate.Pk, out var head))
        {
            if (aggregate.IsNew && aggregate.AggregateSequenceNumber == 0)
            {
                // A new aggregate has no stream yet. If one exists after all,
                // starting it again fails at save and the command is retried.
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
        var body = ((WaxEvent)Activator.CreateInstance(eventType)!) with
        {
            AggregateType = aggregate.AggregateType,
            PriorValue = prior,
            NewValue = @new,
            OccurredAt = Attribution.OccurredAt,
            ActorPkRef = Attribution.ActorPk,
            ActorType = Attribution.ActorType,
            ReasonCodeFk = reasonCode,
            Source = Attribution.Source,
            CorrelationPkRef = Attribution.CorrelationPk,
            CorrelationType = Attribution.CorrelationType,
            Metadata = metadata,
        };
        var eventHash = EventHasher.Compute(new EventHashInput(
            aggregate.AggregateType,
            aggregate.Pk,
            WaxEventTypes.NameOf(eventType),
            body.SchemaVersion,
            prior,
            @new,
            Attribution.OccurredAt,
            Attribution.ActorPk,
            Attribution.ActorType,
            reasonCode,
            head.EventHash));

        var wrapped = Wrap(body).WithHeader(StreamHeads.EventHashHeader, eventHash);
        if (head.EventHash is not null)
        {
            wrapped = wrapped.WithHeader(StreamHeads.PriorEventHashHeader, head.EventHash);
        }
        if (head.Version == 0)
        {
            session.Events.StartStream(aggregate.Pk, wrapped);
        }
        else
        {
            session.Events.Append(aggregate.Pk, wrapped);
        }

        heads[aggregate.Pk] = new StreamHead(head.Version + 1, eventHash);
        aggregate.AggregateSequenceNumber = checked((int)(head.Version + 1));
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
