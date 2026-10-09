using System.Collections.Concurrent;
using System.Reflection;
using System.Text.Json.Nodes;

namespace Wax.Events;

/// <summary>
/// A Wax event type. Each subtype names one event_type, by its
/// <see cref="WaxEventTypeAttribute"/>, so that handlers, projections, and
/// rules refer to event types by C# type rather than by string. The event's
/// content is not kept on the type: it is the row in core.event, which
/// <see cref="StoredEvent"/> carries.
/// </summary>
public abstract record WaxEvent;

/// <summary>The event_type a Wax event is stored and hashed under, and its schema version.</summary>
[AttributeUsage(AttributeTargets.Class, Inherited = false)]
public sealed class WaxEventTypeAttribute(string name) : Attribute
{
    public string Name { get; } = name;

    public short SchemaVersion { get; init; } = 1;
}

public static class WaxEventTypes
{
    static readonly ConcurrentDictionary<Type, WaxEventTypeAttribute> Declared = new();
    static readonly ConcurrentDictionary<string, Type> Types = new();

    static WaxEventTypeAttribute Of(Type type) => Declared.GetOrAdd(type, static t =>
    {
        var declared = t.GetCustomAttribute<WaxEventTypeAttribute>()
            ?? throw new InvalidOperationException($"{t.Name} has no [WaxEventType]");
        Types.TryAdd(declared.Name, t);
        return declared;
    });

    public static string NameOf(Type type) => Of(type).Name;

    public static string NameOf<TEvent>() where TEvent : WaxEvent => NameOf(typeof(TEvent));

    public static short SchemaVersionOf(Type type) => Of(type).SchemaVersion;

    /// <summary>The event type stored under a name, for an event chosen at run
    /// time, such as the target of a Pending Event Register entry.</summary>
    public static Type TypeOf(string name) =>
        Types.TryGetValue(name, out var type) ? type : throw new InvalidOperationException($"No event type is registered as {name}");

    public static bool IsKnown(string name) => Types.ContainsKey(name);

    /// <summary>Make every Wax event type declared in the given assemblies known by name.</summary>
    public static void Register(IEnumerable<Assembly> assemblies)
    {
        foreach (var type in assemblies.SelectMany(assembly => assembly.GetTypes())
            .Where(type => type is { IsAbstract: false } && type.IsSubclassOf(typeof(WaxEvent)))
            .Where(type => type.GetCustomAttribute<WaxEventTypeAttribute>() is not null))
        {
            Of(type);
        }
    }
}

/// <summary>
/// One row of core.event, as WAX-0001 defines it: what the reader hands to
/// projections and rules, and what the chain verifier checks.
/// </summary>
public sealed record StoredEvent(
    Guid EventPk,
    long GlobalSequenceNumber,
    int AggregateSequenceNumber,
    string EventType,
    short SchemaVersion,
    Guid AggregatePkRef,
    string AggregateType,
    DateTimeOffset OccurredAt,
    DateTimeOffset RecordedAt,
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
    /// <summary>The columns in the order <see cref="Read"/> expects.</summary>
    public const string Columns =
        "event_pk, global_sequence_number, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, " +
        "aggregate_type, occurred_at, recorded_at, actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, " +
        "source, correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash";

    public bool Is<TEvent>() where TEvent : WaxEvent => EventType == WaxEventTypes.NameOf<TEvent>();

    public static StoredEvent Read(Npgsql.NpgsqlDataReader reader) => new(
        reader.GetGuid(0),
        reader.GetInt64(1),
        reader.GetInt32(2),
        reader.GetString(3),
        reader.GetInt16(4),
        reader.GetGuid(5),
        reader.GetString(6),
        reader.GetFieldValue<DateTimeOffset>(7),
        reader.GetFieldValue<DateTimeOffset>(8),
        reader.GetGuid(9),
        reader.GetString(10),
        Json(reader, 11),
        Json(reader, 12),
        reader.IsDBNull(13) ? null : reader.GetGuid(13),
        reader.IsDBNull(14) ? null : reader.GetString(14),
        reader.IsDBNull(15) ? null : reader.GetGuid(15),
        reader.IsDBNull(16) ? null : reader.GetString(16),
        Json(reader, 17),
        reader.IsDBNull(18) ? null : reader.GetString(18),
        reader.GetString(19));

    static JsonObject? Json(Npgsql.NpgsqlDataReader reader, int ordinal) =>
        reader.IsDBNull(ordinal) ? null : JsonNode.Parse(reader.GetString(ordinal)) as JsonObject;
}
