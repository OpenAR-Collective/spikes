using Npgsql;

namespace Wax.Batches;

public sealed record BatchChunk(Guid ChunkPk, int Sequence, Guid RangeStart, Guid RangeEnd);

/// <summary>
/// Chunk-based batch work (WAX-0024). A coordinator divides a table's keys,
/// in key order, into fixed-size chunks; workers claim chunks with
/// SELECT FOR UPDATE SKIP LOCKED, each in a short transaction of its own, and
/// run one command per key through the normal command path.
/// </summary>
public sealed class BatchRunner(NpgsqlDataSource dataSource, string table, string keyColumn)
{
    /// <summary>Create the run and all of its chunks. Returns the run key.</summary>
    public async Task<Guid> CreateRunAsync(string jobName, int chunkSize, CancellationToken cancellation = default)
    {
        var runPk = Guid.NewGuid();
        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        await using var transaction = await connection.BeginTransactionAsync(cancellation);
        await using (var run = new NpgsqlCommand("INSERT INTO core.batch_job_run (batch_job_run_pk, job_name) VALUES ($1, $2)", connection, transaction))
        {
            run.Parameters.AddWithValue(runPk);
            run.Parameters.AddWithValue(jobName);
            await run.ExecuteNonQueryAsync(cancellation);
        }
        await using (var chunks = new NpgsqlCommand(
            $"INSERT INTO core.batch_job_chunk (batch_job_run_fk, chunk_sequence_number, range_start, range_end, status) " +
            // PostgreSQL has no min or max for uuid, so each chunk's first and
            // last keys come from its ordered keys.
            $"SELECT $1, chunk, (array_agg(key ORDER BY key))[1], (array_agg(key ORDER BY key DESC))[1], 'pending' FROM (" +
            $"SELECT {keyColumn} AS key, (row_number() OVER (ORDER BY {keyColumn}) - 1) / $2 AS chunk FROM {table}) keyed " +
            $"GROUP BY chunk",
            connection, transaction))
        {
            chunks.Parameters.AddWithValue(runPk);
            chunks.Parameters.AddWithValue((long)chunkSize);
            await chunks.ExecuteNonQueryAsync(cancellation);
        }
        await transaction.CommitAsync(cancellation);
        return runPk;
    }

    /// <summary>Claim the next pending chunk, or null when none is left.</summary>
    public async Task<BatchChunk?> ClaimAsync(Guid runPk, string instance, CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand(
            "UPDATE core.batch_job_chunk SET status = 'claimed', claimed_by_instance = $2, " +
            "claim_expires_at = now() + interval '10 minutes' " +
            "WHERE batch_job_chunk_pk = (SELECT batch_job_chunk_pk FROM core.batch_job_chunk " +
            "WHERE batch_job_run_fk = $1 AND status = 'pending' ORDER BY chunk_sequence_number " +
            "LIMIT 1 FOR UPDATE SKIP LOCKED) " +
            "RETURNING batch_job_chunk_pk, chunk_sequence_number, range_start, range_end");
        command.Parameters.AddWithValue(runPk);
        command.Parameters.AddWithValue(instance);
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        return await reader.ReadAsync(cancellation)
            ? new BatchChunk(reader.GetGuid(0), reader.GetInt32(1), reader.GetGuid(2), reader.GetGuid(3))
            : null;
    }

    /// <summary>The keys in a chunk, in key order.</summary>
    public async Task<List<Guid>> KeysAsync(BatchChunk chunk, CancellationToken cancellation = default)
    {
        var keys = new List<Guid>();
        await using var command = dataSource.CreateCommand(
            $"SELECT {keyColumn} FROM {table} WHERE {keyColumn} BETWEEN $1 AND $2 ORDER BY {keyColumn}");
        command.Parameters.AddWithValue(chunk.RangeStart);
        command.Parameters.AddWithValue(chunk.RangeEnd);
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        while (await reader.ReadAsync(cancellation))
        {
            keys.Add(reader.GetGuid(0));
        }
        return keys;
    }

    public async Task CompleteAsync(BatchChunk chunk, string? error = null, CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand(
            "UPDATE core.batch_job_chunk SET status = $2, completed_at = now(), error_detail = $3 WHERE batch_job_chunk_pk = $1");
        command.Parameters.AddWithValue(chunk.ChunkPk);
        command.Parameters.AddWithValue(error is null ? "complete" : "failed");
        command.Parameters.AddWithValue((object?)error ?? DBNull.Value);
        await command.ExecuteNonQueryAsync(cancellation);
    }

    public async Task FinishRunAsync(Guid runPk, CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand("UPDATE core.batch_job_run SET finished_at = now() WHERE batch_job_run_pk = $1");
        command.Parameters.AddWithValue(runPk);
        await command.ExecuteNonQueryAsync(cancellation);
    }

    /// <summary>Run every chunk of a run with the given number of workers, calling
    /// work for each key. Returns when no pending chunk is left.</summary>
    public async Task RunAsync(Guid runPk, int workers, string instance, Func<Guid, Task> work, CancellationToken cancellation = default)
    {
        await Task.WhenAll(Enumerable.Range(0, workers).Select(worker => Task.Run(async () =>
        {
            var name = $"{instance}/{worker}";
            while (await ClaimAsync(runPk, name, cancellation) is { } chunk)
            {
                foreach (var key in await KeysAsync(chunk, cancellation))
                {
                    await work(key);
                }
                await CompleteAsync(chunk, cancellation: cancellation);
            }
        }, cancellation)));
        await FinishRunAsync(runPk, cancellation);
    }
}
