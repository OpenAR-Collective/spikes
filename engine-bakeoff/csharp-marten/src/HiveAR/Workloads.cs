using System.Text.Json.Nodes;
using Npgsql;
using Wax.Commands;
using Wax.Events;
using Wax.Hashing;

namespace HiveAR;

// The commands and handlers the bake-off's workloads add to the prototype's
// slice (WORKLOADS.md). Foundation-written. Each keeps every invariant: the
// row lock, the version check, the hash in the same transaction, and the
// event and state written together, and each is idempotent.

public sealed record AccrueInterest : ICommand
{
    public required Guid AccountPk { get; init; }
    public required DateOnly AccrualDate { get; init; }
    public required int AnnualRateBasisPoints { get; init; }

    public void Validate()
    {
        Rules.PlausibleDate(AccrualDate, "accrual_date");
        if (AnnualRateBasisPoints is < 0 or > 100_000)
        {
            throw Rules.Invalid("annual_rate_basis_points", "must be from 0 to 100,000");
        }
    }
}

public sealed record ChangeAccountStatus : ICommand
{
    public required Guid AccountPk { get; init; }
    public required string Status { get; init; }
    public required string ReasonCode { get; init; }

    public void Validate()
    {
        Rules.OneOf(Status, "status", "active", "paid_in_full", "closed");
        Rules.BucketCode(ReasonCode);
    }
}

public sealed record AssignToQueue : ICommand
{
    public required Guid AccountPk { get; init; }
    public required string QueueCode { get; init; }

    public void Validate() => Rules.BucketCode(QueueCode);
}

public sealed record CreateTask : ICommand
{
    public required Guid TaskPk { get; init; }
    public required Guid AccountPk { get; init; }
    public required string TaskType { get; init; }

    public void Validate() => Rules.BucketCode(TaskType);
}

public sealed record ScheduleFutureEvent : ICommand
{
    public required Guid PendingEventPk { get; init; }
    public required Guid AccountPk { get; init; }
    public required string TargetEventType { get; init; }
    public required DateTimeOffset DueAt { get; init; }
    public Guid? ScheduledByEvent { get; init; }

    public void Validate()
    {
        if (!WaxEventTypes.IsKnown(TargetEventType))
        {
            throw Rules.Invalid("target_event_type", $"{TargetEventType} is not a registered event type");
        }
    }
}

public sealed record FirePendingEvent : ICommand
{
    public required Guid PendingEventPk { get; init; }

    public void Validate()
    {
    }
}

public sealed class AccrueInterestHandler : ICommandHandler<AccrueInterest>
{
    public async Task HandleAsync(AccrueInterest command, CommandContext context, CancellationToken cancellation)
    {
        var (accounts, rows) = await Streams.LockAccountsAsync(context, [command.AccountPk], withBuckets: true, cancellation);
        var account = accounts[0];
        if (account.Status != "active" || command.AccrualDate <= account.InterestAccruedThrough)
        {
            throw new AlreadyAppliedException();
        }
        var days = command.AccrualDate.DayNumber - account.InterestAccruedThrough.DayNumber;
        var buckets = rows[account.Pk];
        var principal = buckets.TryGetValue("principal", out var principalRow) ? principalRow.Balance : 0;
        var amount = (long)((Int128)principal * command.AnnualRateBasisPoints * days / (10_000 * 365));
        buckets.TryGetValue("interest", out var interestRow);
        var priorInterest = interestRow?.Balance ?? 0;
        var newInterest = priorInterest + amount;
        if (interestRow is not null && amount != 0)
        {
            interestRow.Balance = newInterest;
            context.QueueSql("UPDATE ar.account_balance_bucket SET balance_minor_units = ? WHERE account_balance_bucket_pk = ?", newInterest, interestRow.Pk);
        }
        else if (interestRow is null && amount != 0)
        {
            context.QueueSql(
                "INSERT INTO ar.account_balance_bucket (account_balance_bucket_pk, balance_minor_units, account_fk, balance_bucket_definition_fk) " +
                "SELECT ?, ?, ?, balance_bucket_definition_pk FROM ar.balance_bucket_definition WHERE code = 'interest'",
                Guid.NewGuid(), newInterest, account.Pk);
        }

        var priorThrough = account.InterestAccruedThrough;
        account.InterestAccruedThrough = command.AccrualDate;
        await context.RecordAsync<AccountInterestAccrued>(
            account,
            prior: new JsonObject
            {
                ["balance_buckets"] = new JsonObject { ["interest"] = priorInterest },
                ["interest_accrued_through"] = IsoFormat.Date(priorThrough),
            },
            @new: new JsonObject
            {
                ["balance_buckets"] = new JsonObject { ["interest"] = newInterest },
                ["interest_accrued_through"] = IsoFormat.Date(command.AccrualDate),
            },
            reason: "interest_accrued",
            metadata: new JsonObject
            {
                ["days"] = days,
                ["annual_rate_basis_points"] = command.AnnualRateBasisPoints,
                ["amount_minor_units"] = amount,
            },
            cancellation: cancellation);
    }
}

public sealed class ChangeAccountStatusHandler : ICommandHandler<ChangeAccountStatus>
{
    public async Task HandleAsync(ChangeAccountStatus command, CommandContext context, CancellationToken cancellation)
    {
        var account = (await Streams.LockAccountsAsync(context, [command.AccountPk], withBuckets: false, cancellation)).Accounts[0];
        if (account.Status == command.Status)
        {
            throw new AlreadyAppliedException();
        }
        var prior = account.Status;
        account.Status = command.Status;
        await context.RecordAsync<AccountStatusChanged>(
            account,
            prior: new JsonObject { ["status"] = prior },
            @new: new JsonObject { ["status"] = command.Status },
            reason: command.ReasonCode,
            cancellation: cancellation);
    }
}

public sealed class AssignToQueueHandler : ICommandHandler<AssignToQueue>
{
    public async Task HandleAsync(AssignToQueue command, CommandContext context, CancellationToken cancellation)
    {
        var account = (await Streams.LockAccountsAsync(context, [command.AccountPk], withBuckets: false, cancellation)).Accounts[0];
        if (account.QueueCode == command.QueueCode)
        {
            throw new AlreadyAppliedException();
        }
        var prior = account.QueueCode;
        account.QueueCode = command.QueueCode;
        await context.RecordAsync<AccountQueueAssigned>(
            account,
            prior: new JsonObject { ["queue_code"] = prior },
            @new: new JsonObject { ["queue_code"] = command.QueueCode },
            reason: "queue_assigned",
            cancellation: cancellation);
    }
}

public sealed class CreateTaskHandler : ICommandHandler<CreateTask>
{
    public async Task HandleAsync(CreateTask command, CommandContext context, CancellationToken cancellation)
    {
        var (account, existing) = await Streams.LockAccountWithAsync(
            context,
            command.AccountPk,
            new NpgsqlBatchCommand("SELECT account_fk, task_type FROM ar.task WHERE task_pk = $1")
            {
                Parameters = { new NpgsqlParameter { Value = command.TaskPk } },
            },
            cancellation);
        if (existing is not null)
        {
            if ((Guid)existing[0] == command.AccountPk && (string)existing[1] == command.TaskType)
            {
                throw new AlreadyAppliedException();
            }
            throw new CommandRejectedException("A different task already exists under this key", "key_reused");
        }
        context.QueueSql(
            "INSERT INTO ar.task (task_pk, account_fk, task_type, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            command.TaskPk, command.AccountPk, command.TaskType, context.OccurredAt);
        await context.RecordAsync<TaskCreated>(
            account,
            prior: null,
            @new: new JsonObject
            {
                ["task_pk"] = command.TaskPk.ToString(),
                ["task_type"] = command.TaskType,
                ["status"] = "open",
            },
            reason: "task_created",
            cancellation: cancellation);
    }
}

public sealed class ScheduleFutureEventHandler : ICommandHandler<ScheduleFutureEvent>
{
    /// <summary>The scheduling decision is an event on the account (WAX-0027),
    /// and the register entry is its projection, written in the same transaction.</summary>
    public async Task HandleAsync(ScheduleFutureEvent command, CommandContext context, CancellationToken cancellation)
    {
        var (account, existing) = await Streams.LockAccountWithAsync(
            context,
            command.AccountPk,
            new NpgsqlBatchCommand("SELECT aggregate_pk_ref, target_event_type FROM core.pending_event WHERE pending_event_pk = $1")
            {
                Parameters = { new NpgsqlParameter { Value = command.PendingEventPk } },
            },
            cancellation);
        if (existing is not null)
        {
            if ((Guid)existing[0] == command.AccountPk && (string)existing[1] == command.TargetEventType)
            {
                throw new AlreadyAppliedException();
            }
            throw new CommandRejectedException("A different scheduled event already exists under this key", "key_reused");
        }
        var dueAt = IsoFormat.TruncateToMicroseconds(command.DueAt);
        context.QueueSql(
            "INSERT INTO core.pending_event (pending_event_pk, aggregate_pk_ref, aggregate_type, target_event_type, due_at, " +
            "status, scheduled_by_event) VALUES (?, ?, 'account', ?, ?, 'pending', ?::uuid)",
            command.PendingEventPk, command.AccountPk, command.TargetEventType, dueAt,
            (object?)command.ScheduledByEvent ?? DBNull.Value);
        await context.RecordAsync<PendingEventScheduled>(
            account,
            prior: null,
            @new: new JsonObject
            {
                ["pending_event_pk"] = command.PendingEventPk.ToString(),
                ["target_event_type"] = command.TargetEventType,
                ["due_at"] = IsoFormat.Microseconds(dueAt),
                ["scheduled_by_event"] = command.ScheduledByEvent?.ToString(),
            },
            reason: "pending_event_scheduled",
            cancellation: cancellation);
    }
}

public sealed class FirePendingEventHandler : ICommandHandler<FirePendingEvent>
{
    /// <summary>Fire a fresh, independent domain event on the entry's account
    /// and mark the entry fired (WAX-0027). An entry another poller holds is
    /// passed over, and one already fired is answered as success.</summary>
    public async Task HandleAsync(FirePendingEvent command, CommandContext context, CancellationToken cancellation)
    {
        Guid accountPk;
        string targetEventType;
        DateTimeOffset dueAt;
        string status;
        await using (var claim = context.CreateCommand(
            "SELECT aggregate_pk_ref, target_event_type, due_at, status FROM core.pending_event " +
            "WHERE pending_event_pk = $1 FOR UPDATE SKIP LOCKED"))
        {
            claim.Parameters.AddWithValue(command.PendingEventPk);
            await using var reader = await claim.ExecuteReaderAsync(cancellation);
            if (!await reader.ReadAsync(cancellation))
            {
                // Locked by another poller that is firing it, or never scheduled.
                throw new AlreadyAppliedException();
            }
            accountPk = reader.GetGuid(0);
            targetEventType = reader.GetString(1);
            dueAt = reader.GetFieldValue<DateTimeOffset>(2);
            status = reader.GetString(3);
        }
        if (status != "pending")
        {
            throw new AlreadyAppliedException();
        }
        var account = (await Streams.LockAccountsAsync(context, [accountPk], withBuckets: false, cancellation)).Accounts[0];
        context.QueueSql(
            "UPDATE core.pending_event SET status = 'fired', fired_at = clock_timestamp() WHERE pending_event_pk = ?",
            command.PendingEventPk);
        await context.RecordAsync(
            WaxEventTypes.TypeOf(targetEventType),
            account,
            prior: null,
            @new: new JsonObject { ["due_at"] = IsoFormat.Microseconds(dueAt) },
            reason: "pending_event_fired",
            metadata: new JsonObject { ["pending_event_pk"] = command.PendingEventPk.ToString() },
            cancellation: cancellation);
    }
}
