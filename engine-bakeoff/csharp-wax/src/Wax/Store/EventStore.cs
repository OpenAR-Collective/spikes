using System.Collections.Concurrent;
using System.Text;
using System.Text.Json.Nodes;
using Npgsql;
using NpgsqlTypes;
using Wax.Commands;
using Wax.Events;
using Wax.Hashing;

namespace Wax.Store;

/// <summary>
/// One event about to be appended to core.event: every column the writer
/// supplies. The store assigns the other two, global_sequence_number and
/// recorded_at, in its insert trigger.
/// </summary>
public sealed record NewEvent(
    Guid EventPk,
    int AggregateSequenceNumber,
    string EventType,
    short SchemaVersion,
    Guid AggregatePkRef,
    string AggregateType,
    DateTimeOffset OccurredAt,
    Guid ActorPkRef,
    string ActorType,
    JsonObject? PriorValue,
    JsonObject? NewValue,
    Guid? ReasonCodeFk,
    string? Source,
    Guid? CorrelationPkRef,
    string? CorrelationType,
    JsonObject? Metadata,
    string? PriorEventHash,
    string EventHash)
{
    /// <summary>
    /// Shape one event and compute its hash over the fields WAX-0013 lists,
    /// chained to the aggregate's previous event. Both the command path and the
    /// bulk writer build their events here, so they cannot store different ones.
    /// </summary>
    public static NewEvent Create(
        Type eventType,
        string aggregateType,
        Guid aggregatePk,
        StreamHead head,
        JsonObject? prior,
        JsonObject? @new,
        JsonObject? metadata,
        Attribution attribution,
        Guid? reasonCode)
    {
        var name = WaxEventTypes.NameOf(eventType);
        var schemaVersion = WaxEventTypes.SchemaVersionOf(eventType);
        // The hashing-off diagnostic stores placeholders instead (see EventHashing).
        var priorHash = EventHashing.Enabled ? head.EventHash : head.Version > 0 ? EventHashing.Placeholder : null;
        var eventHash = !EventHashing.Enabled ? EventHashing.Placeholder : EventHasher.Compute(new EventHashInput(
            aggregateType,
            aggregatePk,
            name,
            schemaVersion,
            prior,
            @new,
            attribution.OccurredAt,
            attribution.ActorPk,
            attribution.ActorType,
            reasonCode,
            head.EventHash));
        return new NewEvent(
            Guid.NewGuid(),
            checked((int)(head.Version + 1)),
            name,
            schemaVersion,
            aggregatePk,
            aggregateType,
            attribution.OccurredAt,
            attribution.ActorPk,
            attribution.ActorType,
            prior,
            @new,
            reasonCode,
            attribution.Source,
            attribution.CorrelationPk,
            attribution.CorrelationType,
            metadata,
            priorHash,
            eventHash);
    }
}

/// <summary>
/// The append half of the event store abstraction in WAX-0001: the SQL that
/// writes core.event. The reader is <see cref="Projections.ProjectionReader"/>.
/// Nothing else in the codebase writes core.event.
/// </summary>
public static class EventStore
{
    const string Columns =
        "event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type, occurred_at, " +
        "actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, source, correlation_pk_ref, correlation_type, " +
        "metadata, prior_event_hash, event_hash";

    const string InsertSql =
        $"INSERT INTO core.event ({Columns}) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18)";

    // The rows go in the order they were recorded, so the trigger numbers them in that order.
    const string InsertManySql =
        $"INSERT INTO core.event ({Columns}) SELECT {Columns} FROM unnest(" +
        "$1::uuid[], $2::integer[], $3::text[], $4::smallint[], $5::uuid[], $6::text[], $7::timestamptz[], $8::uuid[], " +
        "$9::text[], $10::jsonb[], $11::jsonb[], $12::uuid[], $13::text[], $14::uuid[], $15::text[], $16::jsonb[], " +
        "$17::text[], $18::text[]) WITH ORDINALITY " +
        $"AS e({Columns}, n) ORDER BY n";

    /// <summary>One event, as one statement for the caller's batch. The caller
    /// holds the aggregate's row lock and has checked its version; the unique
    /// index on (aggregate_pk_ref, aggregate_sequence_number) is the backstop.</summary>
    public static NpgsqlBatchCommand InsertCommand(NewEvent e) => new(InsertSql)
    {
        Parameters =
        {
            new NpgsqlParameter { Value = e.EventPk, NpgsqlDbType = NpgsqlDbType.Uuid },
            new NpgsqlParameter { Value = e.AggregateSequenceNumber, NpgsqlDbType = NpgsqlDbType.Integer },
            new NpgsqlParameter { Value = e.EventType, NpgsqlDbType = NpgsqlDbType.Text },
            new NpgsqlParameter { Value = e.SchemaVersion, NpgsqlDbType = NpgsqlDbType.Smallint },
            new NpgsqlParameter { Value = e.AggregatePkRef, NpgsqlDbType = NpgsqlDbType.Uuid },
            new NpgsqlParameter { Value = e.AggregateType, NpgsqlDbType = NpgsqlDbType.Text },
            new NpgsqlParameter { Value = e.OccurredAt, NpgsqlDbType = NpgsqlDbType.TimestampTz },
            new NpgsqlParameter { Value = e.ActorPkRef, NpgsqlDbType = NpgsqlDbType.Uuid },
            new NpgsqlParameter { Value = e.ActorType, NpgsqlDbType = NpgsqlDbType.Text },
            Json(e.PriorValue),
            Json(e.NewValue),
            new NpgsqlParameter { Value = (object?)e.ReasonCodeFk ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Uuid },
            new NpgsqlParameter { Value = (object?)e.Source ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Text },
            new NpgsqlParameter { Value = (object?)e.CorrelationPkRef ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Uuid },
            new NpgsqlParameter { Value = (object?)e.CorrelationType ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Text },
            Json(e.Metadata),
            new NpgsqlParameter { Value = (object?)e.PriorEventHash ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Text },
            new NpgsqlParameter { Value = e.EventHash, NpgsqlDbType = NpgsqlDbType.Text },
        },
    };

    /// <summary>Many events in one set-based statement, for the bulk workloads.</summary>
    public static NpgsqlBatchCommand InsertManyCommand(IReadOnlyList<NewEvent> events) => new(InsertManySql)
    {
        Parameters =
        {
            Array(events.Select(e => e.EventPk).ToArray(), NpgsqlDbType.Uuid),
            Array(events.Select(e => e.AggregateSequenceNumber).ToArray(), NpgsqlDbType.Integer),
            Array(events.Select(e => e.EventType).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.SchemaVersion).ToArray(), NpgsqlDbType.Smallint),
            Array(events.Select(e => e.AggregatePkRef).ToArray(), NpgsqlDbType.Uuid),
            Array(events.Select(e => e.AggregateType).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.OccurredAt).ToArray(), NpgsqlDbType.TimestampTz),
            Array(events.Select(e => e.ActorPkRef).ToArray(), NpgsqlDbType.Uuid),
            Array(events.Select(e => e.ActorType).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.PriorValue?.ToJsonString()).ToArray(), NpgsqlDbType.Jsonb),
            Array(events.Select(e => e.NewValue?.ToJsonString()).ToArray(), NpgsqlDbType.Jsonb),
            Array(events.Select(e => e.ReasonCodeFk).ToArray(), NpgsqlDbType.Uuid),
            Array(events.Select(e => e.Source).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.CorrelationPkRef).ToArray(), NpgsqlDbType.Uuid),
            Array(events.Select(e => e.CorrelationType).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.Metadata?.ToJsonString()).ToArray(), NpgsqlDbType.Jsonb),
            Array(events.Select(e => e.PriorEventHash).ToArray(), NpgsqlDbType.Text),
            Array(events.Select(e => e.EventHash).ToArray(), NpgsqlDbType.Text),
        },
    };

    static NpgsqlParameter Json(JsonObject? value) =>
        new() { Value = (object?)value?.ToJsonString() ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Jsonb };

    static NpgsqlParameter Array<T>(T[] values, NpgsqlDbType type) =>
        new() { Value = values, NpgsqlDbType = NpgsqlDbType.Array | type };

    /// <summary>The sequence's position now: at or past every event already written.</summary>
    public const string PositionSql =
        "SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.event_global_sequence_seq";
}

/// <summary>SQL written with ? placeholders, as PostgreSQL's $1, $2, and so on.</summary>
public static class SqlText
{
    static readonly ConcurrentDictionary<string, string> Positional = new();

    public static string ToPositional(string sql) => Positional.GetOrAdd(sql, static text =>
    {
        var builder = new StringBuilder(text.Length + 16);
        var index = 0;
        foreach (var character in text)
        {
            if (character == '?')
            {
                builder.Append('$').Append(++index);
            }
            else
            {
                builder.Append(character);
            }
        }
        return builder.ToString();
    });

    /// <summary>A batch command from ?-placeholder SQL and its values in order.</summary>
    public static NpgsqlBatchCommand Command(string sql, IEnumerable<object?> values)
    {
        var command = new NpgsqlBatchCommand(ToPositional(sql));
        foreach (var value in values)
        {
            command.Parameters.Add(new NpgsqlParameter { Value = value ?? DBNull.Value });
        }
        return command;
    }
}
