using Microsoft.Extensions.Logging;
using Npgsql;
using NpgsqlTypes;
using Wax.Commands;
using Wax.Events;
using Wax.Projections;

namespace Wax.Rules;

/// <summary>
/// One "when X happens, do Y" post-event rule (WAX-0023): a trigger event
/// type, a condition, and an action that is a command. The action is keyed so
/// that a second delivery of the same event is answered as success.
/// </summary>
public interface IRule
{
    Type TriggerType { get; }

    Task ExecuteAsync(StoredEvent trigger, CommandDispatcher commands, CancellationToken cancellation);
}

/// <summary>
/// Runs post-event rules after the triggering write has committed, from the
/// event stream. Foundation-written for the engine bake-off, the same design
/// as the Python side's wax/rules.py, and reading with the projection reader's
/// high-water mark, so it never skips an event whose transaction committed late.
///
/// Delivery is at least once and in global order. A runner reads the events its
/// rules care about, past its position and up to the high-water mark, runs each
/// matching rule, and sends each action through the normal command path in its
/// own transaction. Only after the actions have committed does it advance its
/// position, so a crash means a second delivery, never a lost one. A runner may
/// be one of several partitions; each takes the aggregates whose key hashes to
/// its partition, which keeps every aggregate's events in order.
/// </summary>
public sealed class RuleRunner
{
    /// <summary>The position a seed loader sets, so that every partition created
    /// later starts at the head of the seed instead of replaying it.</summary>
    public const string BasePosition = "wax.rules:base";

    readonly NpgsqlDataSource dataSource;
    readonly ILookup<string, IRule> rulesByTrigger;
    readonly string[] triggerTypes;
    readonly CommandDispatcher commands;
    readonly int partition;
    readonly int partitions;
    readonly int batchSize;
    readonly ILogger? logger;
    HighWaterMark mark;
    bool registered;
    long actionsRun;
    long actionsRefused;

    public RuleRunner(
        NpgsqlDataSource dataSource,
        IReadOnlyList<IRule> rules,
        CommandDispatcher commands,
        int partition = 0,
        int partitions = 1,
        int batchSize = 500,
        ILogger? logger = null)
    {
        this.dataSource = dataSource;
        rulesByTrigger = rules.ToLookup(rule => WaxEventTypes.NameOf(rule.TriggerType), StringComparer.Ordinal);
        triggerTypes = rulesByTrigger.Select(group => group.Key).Order(StringComparer.Ordinal).ToArray();
        this.commands = commands;
        this.partition = partition;
        this.partitions = partitions;
        this.batchSize = batchSize;
        this.logger = logger;
        mark = new HighWaterMark(dataSource);
        Name = $"wax.rules:{partition}/{partitions}";
    }

    public string Name { get; }

    public long ActionsRun => Interlocked.Read(ref actionsRun);

    public long ActionsRefused => Interlocked.Read(ref actionsRefused);

    async Task<long> PositionAsync(CancellationToken cancellation)
    {
        if (!registered)
        {
            // A partition seen for the first time starts at the base position.
            await using var insert = dataSource.CreateCommand(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest, last_global_sequence_number) " +
                "SELECT $1, 'rules', COALESCE((SELECT last_global_sequence_number FROM core.projection_checkpoint " +
                "WHERE projection_name = $2), 0) ON CONFLICT (projection_name) DO NOTHING");
            insert.Parameters.AddWithValue(Name);
            insert.Parameters.AddWithValue(BasePosition);
            await insert.ExecuteNonQueryAsync(cancellation);
            registered = true;
        }
        await using var command = dataSource.CreateCommand(
            "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = $1");
        command.Parameters.AddWithValue(Name);
        return (long)(await command.ExecuteScalarAsync(cancellation))!;
    }

    /// <summary>Deliver at most one page. Returns how many events it delivered.</summary>
    public async Task<int> RunOnceAsync(CancellationToken cancellation = default)
    {
        var safe = await mark.AdvanceAsync(cancellation);
        var position = await PositionAsync(cancellation);
        if (position >= safe)
        {
            return 0;
        }
        var events = new List<StoredEvent>();
        var sql =
            $"SELECT {StoredEvent.Columns} FROM core.event WHERE global_sequence_number > $1 AND global_sequence_number <= $2 " +
            "AND event_type = ANY($3)";
        if (partitions > 1)
        {
            sql += " AND (hashtext(aggregate_pk_ref::text) & 2147483647) % $5 = $6";
        }
        sql += " ORDER BY global_sequence_number LIMIT $4";
        await using (var command = dataSource.CreateCommand(sql))
        {
            command.Parameters.AddWithValue(position);
            command.Parameters.AddWithValue(safe);
            command.Parameters.Add(new NpgsqlParameter { Value = triggerTypes, NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Text });
            command.Parameters.AddWithValue(batchSize);
            if (partitions > 1)
            {
                command.Parameters.AddWithValue(partitions);
                command.Parameters.AddWithValue(partition);
            }
            await using var reader = await command.ExecuteReaderAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                events.Add(StoredEvent.Read(reader));
            }
        }
        foreach (var trigger in events)
        {
            foreach (var rule in rulesByTrigger[trigger.EventType])
            {
                try
                {
                    await rule.ExecuteAsync(trigger, commands, cancellation);
                    Interlocked.Increment(ref actionsRun);
                }
                catch (CommandRejectedException refused)
                {
                    // A refusal is an answer, not a fault: the rule's condition no
                    // longer held when its action ran. It is not retried (WAX-0023).
                    Interlocked.Increment(ref actionsRefused);
                    logger?.LogDebug("Rule action for event {Event} refused: {Code}", trigger.EventPk, refused.Code);
                }
            }
        }
        // Nothing at or below the mark can still appear, so a short page may
        // carry the position to the mark itself.
        var newPosition = events.Count == batchSize ? events[^1].GlobalSequenceNumber : safe;
        await using (var advance = dataSource.CreateCommand(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = $2, updated_at = now() WHERE projection_name = $1"))
        {
            advance.Parameters.AddWithValue(Name);
            advance.Parameters.AddWithValue(newPosition);
            await advance.ExecuteNonQueryAsync(cancellation);
        }
        return events.Count;
    }

    /// <summary>Run until stopped, sleeping the poll interval whenever a pass finds nothing.</summary>
    public async Task RunAsync(TimeSpan pollInterval, CancellationToken stoppingToken)
    {
        logger?.LogInformation("Rule runner {Name} started", Name);
        while (!stoppingToken.IsCancellationRequested)
        {
            int delivered;
            try
            {
                delivered = await RunOnceAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                return;
            }
            catch (Exception error)
            {
                // The position did not move, so the page is delivered again.
                logger?.LogError(error, "Rule runner {Name}: pass failed; retrying", Name);
                mark = new HighWaterMark(dataSource);
                delivered = 0;
                await Delay(TimeSpan.FromSeconds(1), stoppingToken);
            }
            if (delivered == 0)
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
}
