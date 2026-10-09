// Adapted from the HiveAR prototype by Andy Troiano
// (https://github.com/atroiano/platform-design, commit 361b6aa,
// prototype/wax/projections.py), Apache License 2.0.
// Modified by The OpenAR Collective contributors.
// Changes: ported to C#. A handler queues SQL rather than running it, so the
// reader can send a page of events' writes in one round trip. The definition
// digest covers the projection's declared shape (its aggregate types, the
// event types it handles and ignores, and the methods that handle them)
// rather than the handlers' source text, which a compiled assembly does not
// keep; a change of logic alone is therefore a new version by discipline.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Npgsql;
using Wax.Events;
using Wax.Store;

namespace Wax.Projections;

/// <summary>An event on an aggregate a projection covers, of a type it neither handles nor ignores.</summary>
public sealed class UnknownEventTypeException(string eventType) : Exception(eventType)
{
    public string EventType { get; } = eventType;
}

/// <summary>The read model writes a handler queues for one event. They run in
/// the reader's transaction, with the checkpoint's advance.</summary>
public sealed class ProjectionWrites
{
    readonly List<(string Sql, object?[] Values)> statements = [];

    /// <summary>A statement with ? placeholders and its values in order.</summary>
    public void Sql(string sql, params object?[] values) => statements.Add((sql, values));

    /// <summary>Fresh batch commands for the queued statements, one set per attempt.</summary>
    internal IEnumerable<NpgsqlBatchCommand> Commands() => statements.Select(statement => SqlText.Command(statement.Sql, statement.Values));
}

/// <summary>
/// One version of one read model.
///
/// AggregateTypes says which aggregates the read model covers. An event on a
/// covered aggregate must have a handler or be listed as ignored; any other
/// type stops the projection, because skipping an event that changed state
/// would leave the read model wrong without a sign. Changing a handler or the
/// table's shape means registering a new version, which builds beside the old
/// one from the start of the stream.
/// </summary>
public class Projection
{
    readonly Dictionary<string, Action<StoredEvent, ProjectionWrites>> handlers = new(StringComparer.Ordinal);
    readonly HashSet<string> ignores = new(StringComparer.Ordinal);
    string? digest;

    public Projection(string baseName, int version, IEnumerable<string> aggregateTypes)
    {
        AggregateTypes = aggregateTypes.ToHashSet(StringComparer.Ordinal);
        if (AggregateTypes.Count == 0)
        {
            throw new ArgumentException("A projection must say which aggregate types it covers", nameof(aggregateTypes));
        }
        Name = $"{baseName}.v{version}";
    }

    public string Name { get; }

    public IReadOnlySet<string> AggregateTypes { get; }

    public Projection On<TEvent>(Action<StoredEvent, ProjectionWrites> handler) where TEvent : WaxEvent =>
        On(WaxEventTypes.NameOf<TEvent>(), handler);

    public Projection On(string eventType, Action<StoredEvent, ProjectionWrites> handler)
    {
        if (!handlers.TryAdd(eventType, handler))
        {
            throw new InvalidOperationException($"{Name} already handles {eventType}");
        }
        digest = null;
        return this;
    }

    public Projection Ignore<TEvent>() where TEvent : WaxEvent => Ignore(WaxEventTypes.NameOf<TEvent>());

    public Projection Ignore(string eventType)
    {
        ignores.Add(eventType);
        digest = null;
        return this;
    }

    /// <summary>The handler for an event, or null when the event is not this
    /// projection's business. Throws for an unknown type on a covered aggregate.</summary>
    public Action<StoredEvent, ProjectionWrites>? HandlerFor(StoredEvent e)
    {
        if (!AggregateTypes.Contains(e.AggregateType))
        {
            return null;
        }
        if (handlers.TryGetValue(e.EventType, out var handler))
        {
            return handler;
        }
        return ignores.Contains(e.EventType) ? null : throw new UnknownEventTypeException(e.EventType);
    }

    /// <summary>Identifies this version's definition. Two releases that share a
    /// version name must agree on it, or neither may touch the other's rows.</summary>
    public string Digest() => digest ??= Compute();

    string Compute()
    {
        var definition = new SortedDictionary<string, object>(StringComparer.Ordinal)
        {
            ["aggregate_types"] = AggregateTypes.Order(StringComparer.Ordinal).ToArray(),
            ["ignores"] = ignores.Order(StringComparer.Ordinal).ToArray(),
            ["handlers"] = new SortedDictionary<string, string>(
                handlers.ToDictionary(pair => pair.Key, pair => $"{pair.Value.Method.DeclaringType?.FullName}.{pair.Value.Method.Name}"),
                StringComparer.Ordinal),
        };
        return Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(definition))));
    }
}
