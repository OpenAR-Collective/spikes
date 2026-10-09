using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace Wax.Hashing;

/// <summary>The fields one event's hash covers.</summary>
public readonly record struct EventHashInput(
    string AggregateType,
    Guid AggregatePkRef,
    string EventType,
    int SchemaVersion,
    JsonObject? PriorValue,
    JsonObject? NewValue,
    DateTimeOffset OccurredAt,
    Guid ActorPkRef,
    string ActorType,
    Guid? ReasonCodeFk,
    string? PriorEventHash);

/// <summary>
/// Event hash computation (WAX-0013 Event Chain Integrity and Tamper-Evidence).
/// Stands in for the Wax Security Module, which owns this in the design. It
/// covers the same fields, in the same canonical form, as the Python side's
/// wax/hashing.py, so the two produce identical hashes for identical events.
/// </summary>
public static class EventHasher
{
    public static string Compute(in EventHashInput input)
    {
        var builder = new StringBuilder(512);
        builder.Append('{');
        // The keys in sorted order, as Python's sort_keys writes them.
        Key(builder, "actor_pk_ref", first: true);
        CanonicalJson.WriteString(builder, input.ActorPkRef.ToString());
        Key(builder, "actor_type");
        CanonicalJson.WriteString(builder, input.ActorType);
        Key(builder, "aggregate_pk_ref");
        CanonicalJson.WriteString(builder, input.AggregatePkRef.ToString());
        Key(builder, "aggregate_type");
        CanonicalJson.WriteString(builder, input.AggregateType);
        Key(builder, "event_type");
        CanonicalJson.WriteString(builder, input.EventType);
        Key(builder, "new_value");
        CanonicalJson.Write(builder, input.NewValue);
        Key(builder, "occurred_at");
        CanonicalJson.WriteString(builder, IsoFormat.Microseconds(input.OccurredAt));
        Key(builder, "prior_event_hash");
        if (input.PriorEventHash is null)
        {
            builder.Append("null");
        }
        else
        {
            CanonicalJson.WriteString(builder, input.PriorEventHash);
        }
        Key(builder, "prior_value");
        CanonicalJson.Write(builder, input.PriorValue);
        Key(builder, "reason_code_fk");
        if (input.ReasonCodeFk is { } reason)
        {
            CanonicalJson.WriteString(builder, reason.ToString());
        }
        else
        {
            builder.Append("null");
        }
        Key(builder, "schema_version");
        builder.Append(input.SchemaVersion);
        builder.Append('}');

        // ensure_ascii means the canonical text is ASCII, so its UTF-8 bytes are its chars.
        Span<byte> digest = stackalloc byte[SHA256.HashSizeInBytes];
        SHA256.HashData(Encoding.ASCII.GetBytes(builder.ToString()), digest);
        return Convert.ToHexStringLower(digest);
    }

    static void Key(StringBuilder builder, string name, bool first = false)
    {
        if (!first)
        {
            builder.Append(',');
        }
        builder.Append('"').Append(name).Append("\":");
    }
}
