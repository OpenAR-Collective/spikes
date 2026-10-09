// Adapted from the HiveAR prototype by Andy Troiano
// (https://github.com/atroiano/platform-design, commit 361b6aa,
// prototype/wax/projections.py and
// prototype/wax/management/commands/run_projections.py), Apache License 2.0.
// Modified by The OpenAR Collective contributors.
// Changes: ported to C#. A page of events is read only for the aggregate
// types a projection covers, and its read model writes go out in one batch
// inside a savepoint; when the batch fails, the page is replayed one event at
// a time, as in the original. The worker loop leaves out the release checks
// and heartbeat of the blue-green drill, which the bake-off does not run.

using Microsoft.Extensions.Logging;
using Npgsql;
using NpgsqlTypes;
using Wax.Events;
using Wax.Store;

namespace Wax.Projections;

/// <summary>Position and health of one projection version.</summary>
public sealed record ProjectionStatus(
    string Projection,
    long Position,
    long SequencePosition,
    bool DefinitionMatches,
    DateTimeOffset? FailedAt,
    string? FailureKind,
    long? FailedGlobalSequenceNumber,
    string? FailureDetail);

/// <summary>
/// Asynchronous read model maintenance: the subscribe operation in WAX-0001.
///
/// Events are delivered in global_sequence_number order, which is also each
/// aggregate's own order, and never past the high-water mark. A page of
/// events, the read model writes they cause, and the checkpoint's advance
/// commit in one transaction, so a handler's writes land exactly once. A
/// projection that cannot handle an event stops at that event and records why.
/// It never skips, and it does not hold the others back. Several readers may
/// run at once: each projection's checkpoint row is claimed with
/// SELECT FOR UPDATE SKIP LOCKED, so they share the projections between them.
/// </summary>
public sealed class ProjectionReader(
    NpgsqlDataSource dataSource,
    IReadOnlyList<Projection> projections,
    ILogger? logger = null,
    int batchSize = 500)
{
    HighWaterMark mark = new(dataSource);

    public IReadOnlyList<Projection> Projections => projections;

    /// <summary>Forget the mark. Called after any error or reconnect.</summary>
    public void Reset() => mark = new HighWaterMark(dataSource);

    public async Task<long> SequencePositionAsync(CancellationToken cancellation = default)
    {
        await using var command = dataSource.CreateCommand(EventStore.PositionSql);
        return (long)(await command.ExecuteScalarAsync(cancellation))!;
    }

    /// <summary>A checkpoint row for every projection that has none, at the start of the stream.</summary>
    public async Task RegisterCheckpointsAsync(CancellationToken cancellation = default)
    {
        foreach (var projection in projections)
        {
            await using var command = dataSource.CreateCommand(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest) VALUES ($1, $2) " +
                "ON CONFLICT (projection_name) DO NOTHING");
            command.Parameters.AddWithValue(projection.Name);
            command.Parameters.AddWithValue(projection.Digest());
            await command.ExecuteNonQueryAsync(cancellation);
        }
    }

    /// <summary>Adopt this release's definition for a version. Only for a version no
    /// release has shipped yet: afterward, a changed definition is a new version.</summary>
    public Task AcceptDefinitionAsync(string name, CancellationToken cancellation = default) =>
        ExecuteAsync(
            "UPDATE core.projection_checkpoint SET definition_digest = $2, updated_at = now() WHERE projection_name = $1",
            cancellation, name, projections.Single(projection => projection.Name == name).Digest());

    /// <summary>Clear a recorded failure so the projection resumes at the same event.</summary>
    public Task RetryAsync(string name, CancellationToken cancellation = default) =>
        ExecuteAsync(
            "UPDATE core.projection_checkpoint SET failed_at = NULL, failure_kind = NULL, failed_global_sequence_number = NULL, " +
            "failure_detail = NULL, updated_at = now() WHERE projection_name = $1",
            cancellation, name);

    public async Task<IReadOnlyList<ProjectionStatus>> StatusAsync(CancellationToken cancellation = default)
    {
        var position = await SequencePositionAsync(cancellation);
        var byName = projections.ToDictionary(projection => projection.Name);
        var rows = new List<ProjectionStatus>();
        await using var command = dataSource.CreateCommand(
            "SELECT projection_name, last_global_sequence_number, definition_digest, failed_at, failure_kind, " +
            "failed_global_sequence_number, failure_detail FROM core.projection_checkpoint " +
            "WHERE projection_name = ANY($1) ORDER BY projection_name");
        command.Parameters.AddWithValue(byName.Keys.ToArray());
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        while (await reader.ReadAsync(cancellation))
        {
            var name = reader.GetString(0);
            rows.Add(new ProjectionStatus(
                name,
                reader.GetInt64(1),
                position,
                reader.GetString(2) == byName[name].Digest(),
                reader.IsDBNull(3) ? null : reader.GetFieldValue<DateTimeOffset>(3),
                reader.IsDBNull(4) ? null : reader.GetString(4),
                reader.IsDBNull(5) ? null : reader.GetInt64(5),
                reader.IsDBNull(6) ? null : reader.GetString(6)));
        }
        return rows;
    }

    /// <summary>
    /// Advance every healthy projection by at most one page. Returns how many
    /// events were applied. A projection that cannot handle an event stops at
    /// that event and records why; it never skips, and it does not hold the
    /// others back.
    /// </summary>
    public async Task<int> RunOnceAsync(CancellationToken cancellation = default)
    {
        var safe = await mark.AdvanceAsync(cancellation);
        var handled = 0;
        foreach (var projection in projections)
        {
            try
            {
                handled += await AdvanceAsync(projection, safe, cancellation);
            }
            catch (Exception error) when (error is not OperationCanceledException)
            {
                // A failure outside any one event, such as at commit. The pass for
                // this projection rolled back; record it and move on to the next.
                logger?.LogError(error, "{Projection}: pass failed", projection.Name);
                try
                {
                    await RecordFailureAsync(null, projection.Name, "handler_error", null, $"{error.GetType().Name}: {error.Message}", cancellation);
                }
                catch (Exception recordError) when (recordError is not OperationCanceledException)
                {
                    logger?.LogError(recordError, "{Projection}: could not record the failure", projection.Name);
                }
            }
        }
        return handled;
    }

    /// <summary>Run until stopped, sleeping the poll interval whenever a pass finds nothing.</summary>
    public async Task RunAsync(TimeSpan pollInterval, CancellationToken stoppingToken)
    {
        logger?.LogInformation("Projection reader started for {Count} projections", projections.Count);
        var registered = false;
        while (!stoppingToken.IsCancellationRequested)
        {
            int handled;
            try
            {
                if (!registered)
                {
                    await RegisterCheckpointsAsync(stoppingToken);
                    registered = true;
                }
                handled = await RunOnceAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception error)
            {
                // A database restart must not end the reader, and whatever it knew
                // about the old connection is void.
                logger?.LogError(error, "Projection pass failed; retrying");
                Reset();
                handled = 0;
                await Delay(TimeSpan.FromSeconds(2), stoppingToken);
            }
            if (handled == 0)
            {
                await Delay(pollInterval, stoppingToken);
            }
        }
    }

    static async Task Delay(TimeSpan delay, CancellationToken stoppingToken)
    {
        try
        {
            await Task.Delay(delay, stoppingToken);
        }
        catch (OperationCanceledException)
        {
        }
    }

    async Task<int> AdvanceAsync(Projection projection, long safe, CancellationToken cancellation)
    {
        long last;
        string digest;
        string? failureKind;
        long? failedSequence;
        bool failed;
        bool found;
        await using (var command = dataSource.CreateCommand(
            "SELECT last_global_sequence_number, definition_digest, failed_at IS NOT NULL, failure_kind, failed_global_sequence_number " +
            "FROM core.projection_checkpoint WHERE projection_name = $1"))
        {
            command.Parameters.AddWithValue(projection.Name);
            await using var reader = await command.ExecuteReaderAsync(cancellation);
            found = await reader.ReadAsync(cancellation);
            if (found)
            {
                last = reader.GetInt64(0);
                digest = reader.GetString(1);
                failed = reader.GetBoolean(2);
                failureKind = reader.IsDBNull(3) ? null : reader.GetString(3);
                failedSequence = reader.IsDBNull(4) ? null : reader.GetInt64(4);
            }
            else
            {
                (last, digest, failed, failureKind, failedSequence) = (0, projection.Digest(), false, null, null);
            }
        }
        if (!found)
        {
            await RegisterCheckpointsAsync(cancellation);
        }
        if (digest != projection.Digest())
        {
            // Another release defines this version differently. Leave it alone.
            return 0;
        }
        if (failed)
        {
            // A newer release may know a type an older one stopped on.
            if (failureKind != "unknown_event_type" || failedSequence is null)
            {
                return 0;
            }
            var blocker = await LoadEventAsync(failedSequence.Value, cancellation);
            try
            {
                if (blocker is not null)
                {
                    projection.HandlerFor(blocker);
                }
            }
            catch (UnknownEventTypeException)
            {
                return 0;
            }
            await RetryAsync(projection.Name, cancellation);
        }
        if (last >= safe)
        {
            return 0;
        }

        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        await using var transaction = await connection.BeginTransactionAsync(cancellation);
        // SKIP LOCKED lets several readers share a projection version.
        long locked;
        await using (var claim = new NpgsqlCommand(
            "SELECT last_global_sequence_number FROM core.projection_checkpoint " +
            "WHERE projection_name = $1 AND failed_at IS NULL FOR UPDATE SKIP LOCKED", connection, transaction))
        {
            claim.Parameters.AddWithValue(projection.Name);
            if (await claim.ExecuteScalarAsync(cancellation) is not long claimed)
            {
                return 0;
            }
            locked = claimed;
        }

        var events = new List<StoredEvent>();
        await using (var page = new NpgsqlCommand(
            $"SELECT {StoredEvent.Columns} FROM core.event WHERE global_sequence_number > $1 AND global_sequence_number <= $2 " +
            "AND aggregate_type = ANY($3) ORDER BY global_sequence_number LIMIT $4", connection, transaction))
        {
            page.Parameters.AddWithValue(locked);
            page.Parameters.AddWithValue(safe);
            page.Parameters.Add(new NpgsqlParameter { Value = projection.AggregateTypes.ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Text });
            page.Parameters.AddWithValue(batchSize);
            await using var reader = await page.ExecuteReaderAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                events.Add(StoredEvent.Read(reader));
            }
        }

        var (applied, failure) = events.Count == 0
            ? (null, null)
            : await ApplyAsync(projection, events, connection, transaction, cancellation);
        // Nothing at or below the mark can still appear, so a short page that
        // applied cleanly may carry the checkpoint past numbers no event will carry.
        var position = failure is null && events.Count < batchSize ? safe : applied ?? locked;
        await using (var advance = new NpgsqlCommand(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = $2, updated_at = now() WHERE projection_name = $1",
            connection, transaction))
        {
            advance.Parameters.AddWithValue(projection.Name);
            advance.Parameters.AddWithValue(position);
            await advance.ExecuteNonQueryAsync(cancellation);
        }
        if (failure is { } stopped)
        {
            await RecordFailureAsync(transaction, projection.Name, stopped.Kind, stopped.Sequence, stopped.Detail, cancellation);
        }
        await transaction.CommitAsync(cancellation);
        return applied is null ? 0 : events.Count(e => e.GlobalSequenceNumber <= applied);
    }

    sealed record Failure(string Kind, long Sequence, string Detail);

    /// <summary>
    /// Apply a page in order. The handlers queue their writes in memory, so a
    /// handler that throws, or an unknown type, is found before anything is
    /// sent. The writes of the events before it go out as one batch inside a
    /// savepoint; if the batch fails, it is rolled back and replayed one event
    /// at a time, each in its own savepoint, to find the event that fails.
    /// Returns the last sequence applied and the failure the projection stopped at.
    /// </summary>
    async Task<(long? Applied, Failure? Failure)> ApplyAsync(
        Projection projection, List<StoredEvent> events, NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken cancellation)
    {
        var prepared = new List<(StoredEvent Event, ProjectionWrites Writes)>(events.Count);
        Failure? failure = null;
        foreach (var e in events)
        {
            var writes = new ProjectionWrites();
            try
            {
                projection.HandlerFor(e)?.Invoke(e, writes);
            }
            catch (UnknownEventTypeException unknown)
            {
                logger?.LogError("{Projection} does not know event type {EventType}; stopped at {Sequence}", projection.Name, unknown.EventType, e.GlobalSequenceNumber);
                failure = new Failure("unknown_event_type", e.GlobalSequenceNumber, unknown.EventType);
                break;
            }
            catch (Exception error)
            {
                logger?.LogError(error, "{Projection} stopped at global_sequence_number {Sequence}", projection.Name, e.GlobalSequenceNumber);
                failure = new Failure("handler_error", e.GlobalSequenceNumber, $"{error.GetType().Name}: {error.Message}");
                break;
            }
            prepared.Add((e, writes));
        }
        if (prepared.Count == 0)
        {
            return (null, failure);
        }

        await transaction.SaveAsync("page", cancellation);
        try
        {
            await SendAsync(prepared.SelectMany(item => item.Writes.Commands()), connection, transaction, cancellation);
            await transaction.ReleaseAsync("page", cancellation);
            return (prepared[^1].Event.GlobalSequenceNumber, failure);
        }
        catch (PostgresException)
        {
            await transaction.RollbackAsync("page", cancellation);
            logger?.LogWarning("{Projection}: page failed, replaying one event at a time", projection.Name);
        }

        long? applied = null;
        foreach (var (e, writes) in prepared)
        {
            await transaction.SaveAsync("event", cancellation);
            try
            {
                await SendAsync(writes.Commands(), connection, transaction, cancellation);
                await transaction.ReleaseAsync("event", cancellation);
                applied = e.GlobalSequenceNumber;
            }
            catch (PostgresException error)
            {
                await transaction.RollbackAsync("event", cancellation);
                logger?.LogError(error, "{Projection} stopped at global_sequence_number {Sequence}", projection.Name, e.GlobalSequenceNumber);
                return (applied, new Failure("handler_error", e.GlobalSequenceNumber, $"{error.GetType().Name}: {error.Message}"));
            }
        }
        return (applied, failure);
    }

    static async Task SendAsync(IEnumerable<NpgsqlBatchCommand> commands, NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken cancellation)
    {
        await using var batch = new NpgsqlBatch(connection, transaction);
        foreach (var command in commands)
        {
            batch.BatchCommands.Add(command);
        }
        if (batch.BatchCommands.Count == 0)
        {
            return;
        }
        // A deferred constraint would otherwise fail at commit, attributed to nothing.
        batch.BatchCommands.Add(new NpgsqlBatchCommand("SET CONSTRAINTS ALL IMMEDIATE"));
        await batch.ExecuteNonQueryAsync(cancellation);
    }

    async Task<StoredEvent?> LoadEventAsync(long sequence, CancellationToken cancellation)
    {
        await using var command = dataSource.CreateCommand($"SELECT {StoredEvent.Columns} FROM core.event WHERE global_sequence_number = $1");
        command.Parameters.AddWithValue(sequence);
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        return await reader.ReadAsync(cancellation) ? StoredEvent.Read(reader) : null;
    }

    async Task RecordFailureAsync(
        NpgsqlTransaction? transaction, string name, string kind, long? sequence, string detail, CancellationToken cancellation)
    {
        detail = detail.Replace("\0", "", StringComparison.Ordinal);
        detail = detail[..Math.Min(detail.Length, 2000)];
        var connection = transaction?.Connection ?? await dataSource.OpenConnectionAsync(cancellation);
        try
        {
            await using var command = new NpgsqlCommand(
                "UPDATE core.projection_checkpoint SET failed_at = now(), failure_kind = $2, failed_global_sequence_number = $3, " +
                "failure_detail = $4, updated_at = now() WHERE projection_name = $1",
                connection, transaction);
            command.Parameters.AddWithValue(name);
            command.Parameters.AddWithValue(kind);
            command.Parameters.Add(new NpgsqlParameter { Value = (object?)sequence ?? DBNull.Value, NpgsqlDbType = NpgsqlDbType.Bigint });
            command.Parameters.AddWithValue(detail);
            await command.ExecuteNonQueryAsync(cancellation);
        }
        finally
        {
            if (transaction is null)
            {
                await connection.DisposeAsync();
            }
        }
    }

    async Task ExecuteAsync(string sql, CancellationToken cancellation, params object[] values)
    {
        await using var command = dataSource.CreateCommand(sql);
        foreach (var value in values)
        {
            command.Parameters.AddWithValue(value);
        }
        await command.ExecuteNonQueryAsync(cancellation);
    }

    /// <summary>Run passes until every projection has reached the sequence position
    /// as it stood when called, or has stopped on a failure. For tests and tools.</summary>
    public async Task CatchUpAsync(TimeSpan timeout, CancellationToken cancellation = default)
    {
        await RegisterCheckpointsAsync(cancellation);
        var target = await SequencePositionAsync(cancellation);
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            var handled = await RunOnceAsync(cancellation);
            var status = await StatusAsync(cancellation);
            if (status.All(row => row.Position >= target || row.FailedAt is not null))
            {
                return;
            }
            if (DateTime.UtcNow > deadline)
            {
                throw new TimeoutException($"the read models did not reach {target} in {timeout}");
            }
            if (handled == 0)
            {
                await Task.Delay(50, cancellation);
            }
        }
    }
}
