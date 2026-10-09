using Npgsql;
using NpgsqlTypes;

namespace Wax.Store;

/// <summary>The last aggregate_sequence_number of an aggregate's stream and the hash of its last event.</summary>
public readonly record struct StreamHead(long Version, string? EventHash)
{
    public static readonly StreamHead Empty = new(0, null);
}

/// <summary>
/// Reads the head of each aggregate's stream straight from core.event. There
/// is no stream table to keep current: the unique index on
/// (aggregate_pk_ref, aggregate_sequence_number) finds each aggregate's last
/// event with one backward probe.
/// </summary>
public static class StreamHeads
{
    const string Sql =
        "SELECT k.id, e.aggregate_sequence_number, e.event_hash FROM unnest($1::uuid[]) AS k(id) " +
        "CROSS JOIN LATERAL (SELECT aggregate_sequence_number, event_hash FROM core.event " +
        "WHERE aggregate_pk_ref = k.id ORDER BY aggregate_sequence_number DESC LIMIT 1) e";

    // With hashing off (the bake-off's diagnostic), the head is not read at all;
    // a statement that returns nothing keeps each handler's batch shape.
    const string NothingSql = "SELECT NULL::uuid, NULL::integer, NULL::text WHERE $1::uuid[] IS NULL";

    public static NpgsqlBatchCommand Command(Guid[] streamIds) => new(Wax.Hashing.EventHashing.Enabled ? Sql : NothingSql)
    {
        Parameters = { new NpgsqlParameter { Value = streamIds, NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Uuid } },
    };

    /// <summary>Read one row of <see cref="Command"/>'s result.</summary>
    public static (Guid StreamId, StreamHead Head) Read(NpgsqlDataReader reader) =>
        (reader.GetGuid(0), new StreamHead(reader.GetInt32(1), reader.GetString(2)));
}
