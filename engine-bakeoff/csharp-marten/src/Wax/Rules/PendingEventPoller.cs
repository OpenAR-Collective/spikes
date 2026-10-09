using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Wax.Rules;

/// <summary>
/// Fires Pending Event Register entries as they come due (WAX-0027). The
/// register waits on the wall clock rather than on an event, so it is polled.
/// Each due entry is fired by a command that claims the entry's row with
/// SELECT FOR UPDATE SKIP LOCKED, so several pollers never fire one entry twice.
/// </summary>
public sealed class PendingEventPoller(
    NpgsqlDataSource dataSource,
    Func<Guid, CancellationToken, Task> fire,
    int parallelism = 16,
    TimeSpan? pollInterval = null,
    ILogger<PendingEventPoller>? logger = null) : BackgroundService
{
    const string DueSql =
        "SELECT pending_event_pk FROM core.pending_event WHERE status = 'pending' AND due_at <= now() " +
        "ORDER BY due_at LIMIT 1000";

    readonly TimeSpan interval = pollInterval ?? TimeSpan.FromMilliseconds(250);

    /// <summary>Fire every entry due now. Returns how many were due.</summary>
    public async Task<int> PollOnceAsync(CancellationToken cancellation)
    {
        var due = new List<Guid>();
        await using (var command = dataSource.CreateCommand(DueSql))
        await using (var reader = await command.ExecuteReaderAsync(cancellation))
        {
            while (await reader.ReadAsync(cancellation))
            {
                due.Add(reader.GetGuid(0));
            }
        }
        await Parallel.ForEachAsync(
            due,
            new ParallelOptions { MaxDegreeOfParallelism = parallelism, CancellationToken = cancellation },
            async (pendingEventPk, token) => await fire(pendingEventPk, token));
        return due.Count;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                if (await PollOnceAsync(stoppingToken) == 0)
                {
                    await Task.Delay(interval, stoppingToken);
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception error)
            {
                logger?.LogError(error, "Firing due register entries failed; trying again");
                await Task.Delay(interval, stoppingToken);
            }
        }
    }
}
