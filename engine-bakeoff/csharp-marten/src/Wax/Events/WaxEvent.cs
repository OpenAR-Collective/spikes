using System.Collections.Concurrent;
using System.Reflection;
using System.Text.Json.Nodes;

namespace Wax.Events;

/// <summary>
/// The body of every Wax event: the enriched columns of WAX-0001 Enriched Event
/// Sourcing that Marten's own event table does not have. Marten's columns carry
/// the rest: the stream id is aggregate_pk_ref, the stream version is
/// aggregate_sequence_number, seq_id is global_sequence_number, type is
/// event_type, and the headers carry event_hash and prior_event_hash.
/// </summary>
public abstract record WaxEvent
{
    public string AggregateType { get; init; } = "";
    public short SchemaVersion { get; init; } = 1;
    public JsonObject? PriorValue { get; init; }
    public JsonObject? NewValue { get; init; }
    public DateTimeOffset OccurredAt { get; init; }
    public Guid ActorPkRef { get; init; }
    public string ActorType { get; init; } = "";
    public Guid? ReasonCodeFk { get; init; }
    public string? Source { get; init; }
    public Guid? CorrelationPkRef { get; init; }
    public string? CorrelationType { get; init; }
    public JsonObject? Metadata { get; init; }
}

/// <summary>The event_type a Wax event is stored and hashed under.</summary>
[AttributeUsage(AttributeTargets.Class, Inherited = false)]
public sealed class WaxEventTypeAttribute(string name) : Attribute
{
    public string Name { get; } = name;
}

public static class WaxEventTypes
{
    static readonly ConcurrentDictionary<Type, string> Names = new();

    public static string NameOf(Type type) => Names.GetOrAdd(type, static t =>
        t.GetCustomAttribute<WaxEventTypeAttribute>()?.Name
        ?? throw new InvalidOperationException($"{t.Name} has no [WaxEventType]"));

    public static string NameOf<TEvent>() where TEvent : WaxEvent => NameOf(typeof(TEvent));

    /// <summary>Every Wax event type declared in the given assemblies.</summary>
    public static IEnumerable<Type> DeclaredIn(IEnumerable<Assembly> assemblies) =>
        assemblies.SelectMany(assembly => assembly.GetTypes())
            .Where(type => type is { IsAbstract: false } && type.IsSubclassOf(typeof(WaxEvent)))
            .Where(type => type.GetCustomAttribute<WaxEventTypeAttribute>() is not null);
}
