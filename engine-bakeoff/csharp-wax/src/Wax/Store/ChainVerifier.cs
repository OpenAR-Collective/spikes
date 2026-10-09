using Npgsql;
using Wax.Events;
using Wax.Hashing;

namespace Wax.Store;

public sealed record ChainProblem(Guid StreamId, long Version, string Problem);

public sealed record ChainReport(long Streams, long Events, IReadOnlyList<ChainProblem> Problems);

/// <summary>
/// Recomputes every stream's hash chain from what is stored (WAX-0013): each
/// event's hash from its content, each link to its predecessor's hash, and
/// each version with no gap before it.
/// </summary>
public static class ChainVerifier
{
    const string AllSql =
        $"SELECT {StoredEvent.Columns} FROM core.event ORDER BY aggregate_pk_ref, aggregate_sequence_number";

    const string OneSql =
        $"SELECT {StoredEvent.Columns} FROM core.event WHERE aggregate_pk_ref = $1 ORDER BY aggregate_sequence_number";

    public static async Task<ChainReport> VerifyAllAsync(NpgsqlDataSource dataSource, CancellationToken cancellation = default)
    {
        // One pass over every event; at bake-off sizes it runs for minutes.
        await using var command = dataSource.CreateCommand(AllSql);
        command.CommandTimeout = 0;
        return await VerifyAsync(command, cancellation);
    }

    public static async Task<IReadOnlyList<string>> VerifyStreamAsync(NpgsqlDataSource dataSource, Guid streamId, CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand(OneSql);
        command.Parameters.AddWithValue(streamId);
        var report = await VerifyAsync(command, cancellation);
        return report.Problems.Select(problem => problem.Problem).ToList();
    }

    static async Task<ChainReport> VerifyAsync(NpgsqlCommand command, CancellationToken cancellation)
    {
        var problems = new List<ChainProblem>();
        long streams = 0, events = 0;
        Guid? current = null;
        string? priorHash = null;
        long expectedVersion = 0;

        await using var reader = await command.ExecuteReaderAsync(cancellation);
        while (await reader.ReadAsync(cancellation))
        {
            var e = StoredEvent.Read(reader);
            var streamId = e.AggregatePkRef;
            long version = e.AggregateSequenceNumber;
            events++;

            if (streamId != current)
            {
                current = streamId;
                streams++;
                priorHash = null;
                expectedVersion = 0;
            }
            expectedVersion++;
            if (version != expectedVersion)
            {
                problems.Add(new(streamId, version, $"gap before version {version}"));
                expectedVersion = version;
            }

            if (e.PriorEventHash != priorHash)
            {
                problems.Add(new(streamId, version, $"version {version}: prior_event_hash does not match predecessor"));
            }
            var recomputed = EventHasher.Compute(new EventHashInput(
                e.AggregateType,
                streamId,
                e.EventType,
                e.SchemaVersion,
                e.PriorValue,
                e.NewValue,
                e.OccurredAt,
                e.ActorPkRef,
                e.ActorType,
                e.ReasonCodeFk,
                e.PriorEventHash));
            if (recomputed != e.EventHash)
            {
                problems.Add(new(streamId, version, $"version {version}: event_hash does not match content"));
            }
            priorHash = e.EventHash;
        }
        return new ChainReport(streams, events, problems);
    }
}
