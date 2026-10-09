using Npgsql;
using Wax.Commands;
using Wax.Events;
using Wax.Hashing;
using Wax.Rules;

namespace HiveAR;

/// <summary>
/// The real-time workload's rules (WORKLOADS.md), the "when X happens, do Y"
/// post-event workflows of WAX-0023. Foundation-written. Each action is a
/// command sent as the rule runner, correlated to the event that triggered it,
/// and keyed so that a second delivery is answered as success.
/// </summary>
public static class HiveArRules
{
    public static readonly Guid RuleRunnerActor = DerivedUuid.From("bakeoff:rule-runner");

    public static IReadOnlyList<IRule> All(NpgsqlDataSource dataSource, double secondsPerDay) =>
    [
        new PaidInFull(dataSource),
        new Schedule<DisputeOpened>(WaxEventTypes.NameOf<AccountDisputeReviewDue>(), days: 30, secondsPerDay),
        new ReviewQueue(),
        new Schedule<AccountPlaced>(WaxEventTypes.NameOf<AccountFirstContactDue>(), days: 1, secondsPerDay),
        new FirstContactTask(),
    ];

    static Task Act(CommandDispatcher commands, ICommand action, StoredEvent trigger, CancellationToken cancellation) =>
        commands.DispatchAsync(action, RuleRunnerActor, "system", "rule", trigger.EventPk, "rule", cancellation);

    /// <summary>When a payment is applied and the account is active with a
    /// total balance of zero, mark it paid in full.</summary>
    sealed class PaidInFull(NpgsqlDataSource dataSource) : IRule
    {
        public Type TriggerType => typeof(AccountPaymentApplied);

        public async Task ExecuteAsync(StoredEvent trigger, CommandDispatcher commands, CancellationToken cancellation)
        {
            // A bucket the payment left above zero means the account is not at
            // zero, so the current state need not be read. Only a payment that
            // emptied every bucket it touched is checked against current state.
            var touched = trigger.NewValue?["balance_buckets"]?.AsObject();
            if (touched is null || touched.Any(bucket => bucket.Value!.GetValue<long>() != 0))
            {
                return;
            }
            string? status = null;
            long total = -1;
            await using (var command = dataSource.CreateCommand(
                "SELECT a.status, COALESCE(sum(b.balance_minor_units), 0) FROM ar.account a " +
                "LEFT JOIN ar.account_balance_bucket b ON b.account_fk = a.account_pk " +
                "WHERE a.account_pk = $1 GROUP BY a.status"))
            {
                command.Parameters.AddWithValue(trigger.AggregatePkRef);
                await using var reader = await command.ExecuteReaderAsync(cancellation);
                if (await reader.ReadAsync(cancellation))
                {
                    status = reader.GetString(0);
                    total = reader.GetInt64(1);
                }
            }
            if (status != "active" || total != 0)
            {
                return;
            }
            await Act(commands, new ChangeAccountStatus { AccountPk = trigger.AggregatePkRef, Status = "paid_in_full", ReasonCode = "paid_in_full" }, trigger, cancellation);
        }
    }

    /// <summary>Schedule a future event some simulated days after the trigger occurred.</summary>
    sealed class Schedule<TTrigger>(string target, int days, double secondsPerDay) : IRule where TTrigger : WaxEvent
    {
        public Type TriggerType => typeof(TTrigger);

        public Task ExecuteAsync(StoredEvent trigger, CommandDispatcher commands, CancellationToken cancellation)
        {
            return Act(commands, new ScheduleFutureEvent
            {
                PendingEventPk = DerivedUuid.From("pending:" + trigger.EventPk),
                AccountPk = trigger.AggregatePkRef,
                TargetEventType = target,
                DueAt = trigger.OccurredAt.AddSeconds(days * secondsPerDay),
                ScheduledByEvent = trigger.EventPk,
            }, trigger, cancellation);
        }
    }

    /// <summary>When a dispute's review comes due, put the account in the review queue.</summary>
    sealed class ReviewQueue : IRule
    {
        public Type TriggerType => typeof(AccountDisputeReviewDue);

        public Task ExecuteAsync(StoredEvent trigger, CommandDispatcher commands, CancellationToken cancellation) =>
            Act(commands, new AssignToQueue { AccountPk = trigger.AggregatePkRef, QueueCode = "dispute_review" }, trigger, cancellation);
    }

    /// <summary>When first contact comes due, create the task for it.</summary>
    sealed class FirstContactTask : IRule
    {
        public Type TriggerType => typeof(AccountFirstContactDue);

        public Task ExecuteAsync(StoredEvent trigger, CommandDispatcher commands, CancellationToken cancellation)
        {
            var pendingEventPk = trigger.Metadata!["pending_event_pk"]!.GetValue<string>();
            return Act(commands, new CreateTask
            {
                TaskPk = DerivedUuid.From("task:" + pendingEventPk),
                AccountPk = trigger.AggregatePkRef,
                TaskType = "first_contact",
            }, trigger, cancellation);
        }
    }
}
