using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using Npgsql;
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
        "SELECT stream_id, version, type, data, headers FROM core.mt_events ORDER BY stream_id, version";

    const string OneSql =
        "SELECT stream_id, version, type, data, headers FROM core.mt_events WHERE stream_id = $1 ORDER BY version";

    public static async Task<ChainReport> VerifyAllAsync(NpgsqlDataSource dataSource, CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand(AllSql);
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
            var streamId = reader.GetGuid(0);
            var version = reader.GetInt64(1);
            var eventType = reader.GetString(2);
            var body = JsonNode.Parse(reader.GetString(3))!.AsObject();
            var headers = reader.IsDBNull(4) ? null : JsonNode.Parse(reader.GetString(4))?.AsObject();
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

            var storedPrior = Text(headers, StreamHeads.PriorEventHashHeader);
            var storedHash = Text(headers, StreamHeads.EventHashHeader);
            if (storedPrior != priorHash)
            {
                problems.Add(new(streamId, version, $"version {version}: prior_event_hash does not match predecessor"));
            }
            var recomputed = EventHasher.Compute(new EventHashInput(
                Text(body, "aggregate_type") ?? "",
                streamId,
                eventType,
                body["schema_version"]?.GetValue<int>() ?? 0,
                body["prior_value"] as JsonObject,
                body["new_value"] as JsonObject,
                DateTimeOffset.Parse(Text(body, "occurred_at") ?? "", CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
                Guid.Parse(Text(body, "actor_pk_ref") ?? Guid.Empty.ToString()),
                Text(body, "actor_type") ?? "",
                Text(body, "reason_code_fk") is { } reason ? Guid.Parse(reason) : null,
                storedPrior));
            if (recomputed != storedHash)
            {
                problems.Add(new(streamId, version, $"version {version}: event_hash does not match content"));
            }
            priorHash = storedHash;
        }
        return new ChainReport(streams, events, problems);
    }

    static string? Text(JsonObject? obj, string key) =>
        obj?[key] is JsonValue value && value.GetValueKind() == JsonValueKind.String ? value.GetValue<string>() : null;
}
