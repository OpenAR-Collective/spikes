// Adapted from the HiveAR prototype by Andy Troiano
// (https://github.com/atroiano/platform-design, commit 361b6aa,
// prototype/wax/projections.py, class HighWaterMark), Apache License 2.0.
// Modified by The OpenAR Collective contributors.
// Changes: ported to C#, reading through its own connections from a data
// source, so it can never run inside another transaction.

using Npgsql;
using Wax.Store;

namespace Wax.Projections;

/// <summary>
/// The highest global_sequence_number that is safe to read up to.
///
/// Sequence values are handed out at insert, not at commit, so a row with a
/// lower number can become visible after a row with a higher one. Reading past
/// it would skip it for good.
///
/// A sample pairs the sequence position L with a transaction id N taken just
/// after it. core.event's insert trigger forces a transaction id before it
/// calls nextval(), so every event numbered L or lower belongs to a
/// transaction older than N. Once the oldest transaction still running is N or
/// newer, all of those have finished, and everything up to L is settled.
///
/// The mark is this reader's memory and nothing more. It is thrown away
/// whenever the database restarts, is swapped, or shows a sequence position
/// below one already seen, which is what a restore or a failover looks like.
/// </summary>
public sealed class HighWaterMark(NpgsqlDataSource dataSource, int maxPending = 64)
{
    // The sequence position, and something that changes whenever the database
    // underneath this reader is not the one it was a moment ago.
    const string PositionSql =
        "SELECT CASE WHEN is_called THEN last_value ELSE 0 END, " +
        "pg_postmaster_start_time()::text || '/' || current_database() " +
        "FROM core.event_global_sequence_seq";

    readonly LinkedList<(long Boundary, long Position)> pending = new();
    string? identity;

    public long Value { get; internal set; }

    internal long LastSampled { get; set; }

    void Forget(string? newIdentity)
    {
        identity = newIdentity;
        pending.Clear();
        LastSampled = 0;
        Value = 0;
    }

    public async Task<long> AdvanceAsync(CancellationToken cancellation = default)
    {
        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        long position;
        string sampledIdentity;
        await using (var command = new NpgsqlCommand(PositionSql, connection))
        await using (var reader = await command.ExecuteReaderAsync(cancellation))
        {
            await reader.ReadAsync(cancellation);
            position = reader.GetInt64(0);
            sampledIdentity = reader.GetString(1);
        }
        if (sampledIdentity != identity || position < LastSampled)
        {
            Forget(sampledIdentity);
        }
        if (position > LastSampled)
        {
            // The position first, then a transaction id of our own: every event
            // at or below that position belongs to an older transaction.
            await using var transaction = await connection.BeginTransactionAsync(cancellation);
            await using (var again = new NpgsqlCommand(EventStore.PositionSql, connection, transaction))
            {
                position = (long)(await again.ExecuteScalarAsync(cancellation))!;
            }
            long boundary;
            await using (var xid = new NpgsqlCommand("SELECT pg_current_xact_id()::text::bigint", connection, transaction))
            {
                boundary = (long)(await xid.ExecuteScalarAsync(cancellation))!;
            }
            await transaction.CommitAsync(cancellation);
            if (pending.Count == maxPending)
            {
                pending.RemoveLast();
            }
            pending.AddLast((boundary, position));
            LastSampled = position;
        }
        if (pending.Count > 0)
        {
            long oldestRunning;
            await using (var xmin = new NpgsqlCommand("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint", connection))
            {
                oldestRunning = (long)(await xmin.ExecuteScalarAsync(cancellation))!;
            }
            while (pending.First is { } first && first.Value.Boundary <= oldestRunning)
            {
                Value = Math.Max(Value, first.Value.Position);
                pending.RemoveFirst();
            }
        }
        return Value;
    }
}
