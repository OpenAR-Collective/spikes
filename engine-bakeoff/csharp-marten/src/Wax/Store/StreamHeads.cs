using Npgsql;
using NpgsqlTypes;

namespace Wax.Store;

/// <summary>The last version of a stream and the hash of its last event.</summary>
public readonly record struct StreamHead(long Version, string? EventHash)
{
    public static readonly StreamHead Empty = new(0, null);
}

/// <summary>
/// Where the hash chain lives in Marten's tables. Marten's mt_events has no
/// event_hash or prior_event_hash column, so the two travel in the event's
/// headers, which Marten stores as jsonb beside the body.
/// </summary>
public static class StreamHeads
{
    public const string EventHashHeader = "event_hash";
    public const string PriorEventHashHeader = "prior_event_hash";

    // mt_streams holds each stream's current version, and the unique index on
    // (stream_id, version) finds the event at that version directly.
    const string Sql =
        "SELECT s.id, s.version, e.headers ->> 'event_hash' " +
        "FROM core.mt_streams s " +
        "JOIN core.mt_events e ON e.stream_id = s.id AND e.version = s.version " +
        "WHERE s.id = ANY($1)";

    // With hashing off (the bake-off's diagnostic), the head is not read at all;
    // a statement that returns nothing keeps each handler's batch shape.
    const string NothingSql = "SELECT NULL::uuid, NULL::bigint, NULL::text WHERE $1::uuid[] IS NULL";

    public static NpgsqlBatchCommand Command(Guid[] streamIds) => new(Wax.Hashing.EventHashing.Enabled ? Sql : NothingSql)
    {
        Parameters = { new NpgsqlParameter { Value = streamIds, NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Uuid } },
    };
}
