using JasperFx.Events;
using JasperFx.Events.Daemon;
using JasperFx.Events.Projections;
using Marten;
using Marten.Subscriptions;
using Microsoft.Extensions.Logging;
using Wax.Commands;

namespace Wax.Rules;

/// <summary>
/// One "when X happens, do Y" post-event rule (WAX-0023): a trigger event
/// type, a condition, and an action that is a command. The action is keyed so
/// that a second delivery of the same event is answered as success.
/// </summary>
public interface IRule
{
    Type TriggerType { get; }

    Task ExecuteAsync(IEvent trigger, CommandDispatcher commands, CancellationToken cancellation);
}

/// <summary>
/// Runs post-event rules after the triggering write has committed, from the
/// event stream, as a Marten subscription processed by the async daemon.
/// Delivery is at least once. Each aggregate's events are handled in order;
/// different aggregates in the same page run in parallel.
/// </summary>
public sealed class RuleRunner : SubscriptionBase
{
    readonly ILookup<Type, IRule> rulesByTrigger;
    readonly CommandDispatcher commands;
    readonly int parallelism;
    readonly ILogger? logger;
    long actionsRun;
    long actionsRefused;

    public RuleRunner(IReadOnlyList<IRule> rules, CommandDispatcher commands, int parallelism = 16, ILogger? logger = null)
    {
        Name = "wax_rule_runner";
        rulesByTrigger = rules.ToLookup(rule => rule.TriggerType);
        this.commands = commands;
        this.parallelism = parallelism;
        this.logger = logger;
        foreach (var trigger in rulesByTrigger.Select(group => group.Key))
        {
            IncludeType(trigger);
        }
        Options.BatchSize = 1000;
    }

    public long ActionsRun => Interlocked.Read(ref actionsRun);

    public long ActionsRefused => Interlocked.Read(ref actionsRefused);

    public override async Task<IChangeListener> ProcessEventsAsync(
        EventRange page, ISubscriptionController controller, IDocumentOperations operations, CancellationToken cancellationToken)
    {
        if (parallelism <= 1)
        {
            // One worker: strictly in global order.
            foreach (var trigger in page.Events)
            {
                await RunRulesAsync(trigger, cancellationToken);
            }
        }
        else
        {
            // Partitioned by aggregate: each aggregate's events stay in order.
            await Parallel.ForEachAsync(
                page.Events.GroupBy(e => e.StreamId),
                new ParallelOptions { MaxDegreeOfParallelism = parallelism, CancellationToken = cancellationToken },
                async (stream, token) =>
                {
                    foreach (var trigger in stream)
                    {
                        await RunRulesAsync(trigger, token);
                    }
                });
        }
        // Marten advances the position only after this returns, so every
        // action has committed first: delivery is at least once.
        return NullChangeListener.Instance;
    }

    async Task RunRulesAsync(IEvent trigger, CancellationToken cancellation)
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
                // A refused action takes the rule's failure path, which here
                // is to record it and continue (WAX-0023).
                Interlocked.Increment(ref actionsRefused);
                logger?.LogInformation("Rule action for event {Event} refused: {Code}", trigger.Id, refused.Code);
            }
        }
    }
}
