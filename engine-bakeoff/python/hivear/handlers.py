# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/hivear/handlers.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: place_account records interest_accrued_through, and six handlers
# are added for the engine bake-off (Foundation-written, WORKLOADS.md, "New
# commands"). Andy's other handlers are unchanged.
from datetime import UTC

from django.db import connection
from django.utils import timezone

from hivear import commands
from hivear.allocation import AccountPosition, allocate_across_accounts, run_waterfall
from hivear.models import Account, AccountBalanceBucket, BalanceBucketDefinition, Payment, Task
from wax import store
from wax.commands import AlreadyApplied, CommandRejected, handles
from wax.models import PendingEvent


def _lock_accounts(account_pks):
    """Row-lock the account state rows in a fixed order (WAX-0013)."""
    wanted = set(account_pks)
    accounts = list(Account.objects.select_for_update().filter(pk__in=wanted).order_by("pk"))
    if len(accounts) != len(wanted):
        raise CommandRejected("One or more accounts do not exist", "account_not_found")
    return accounts


def _bucket_rows(accounts):
    rows = AccountBalanceBucket.objects.filter(account__in=accounts).select_related(
        "balance_bucket_definition"
    )
    by_account = {account.pk: {} for account in accounts}
    for row in rows:
        by_account[row.account_id][row.balance_bucket_definition.code] = row
    return by_account


@handles(commands.PlaceAccount)
def place_account(command, context):
    """HIVE-0005: an account needs an itemization date and a balance made of
    registered buckets. The responsible party is not modeled in this draft.
    """
    accrued_through = command.interest_accrued_through or command.itemization_date
    placed = {
        "client_account_number": command.client_account_number,
        "debt_type": command.debt_type,
        "contract_type": command.contract_type,
        "currency_code": command.currency_code,
        "status": "active",
        "is_disputed": False,
        "list_date": command.list_date.isoformat(),
        "itemization_date": command.itemization_date.isoformat(),
        "balance_buckets": dict(command.opening_balances),
        "interest_accrued_through": accrued_through.isoformat(),
    }
    if Account.objects.filter(pk=command.account_pk).exists():
        first = store.load_stream(command.account_pk).first()
        if first is not None and first.event_type == "account.placed" and first.new_value == placed:
            raise AlreadyApplied()
        raise CommandRejected("A different account already exists under this key", "key_reused")
    definitions = {
        definition.code: definition
        for definition in BalanceBucketDefinition.objects.filter(code__in=command.opening_balances)
    }
    unknown = sorted(set(command.opening_balances) - set(definitions))
    if unknown:
        raise CommandRejected(f"Unregistered balance buckets: {', '.join(unknown)}", "bucket_not_registered")

    account = Account(
        account_pk=command.account_pk,
        client_account_number=command.client_account_number,
        debt_type=command.debt_type,
        contract_type=command.contract_type,
        currency_code=command.currency_code,
        list_date=command.list_date,
        itemization_date=command.itemization_date,
        interest_accrued_through=accrued_through,
    )
    context.record(account, "account.placed", prior=None, new=placed, reason="account_placed")
    AccountBalanceBucket.objects.bulk_create(
        AccountBalanceBucket(
            account=account,
            balance_bucket_definition=definitions[code],
            balance_minor_units=amount,
        )
        for code, amount in command.opening_balances.items()
    )


@handles(commands.OpenDispute)
def open_dispute(command, context):
    (account,) = _lock_accounts([command.account_pk])
    if account.is_disputed:
        raise CommandRejected("Account is already disputed", "already_disputed")
    account.is_disputed = True
    # A dispute is a member of the account aggregate, so the event belongs to
    # the account and carries the dispute's identity (WAX-0001).
    context.record(
        account,
        "dispute.opened",
        prior={"is_disputed": False},
        new={"is_disputed": True},
        reason="dispute_raised",
        metadata={"dispute_pk": str(command.dispute_pk)},
    )


@handles(commands.PostPayment)
def post_payment(command, context):
    """HIVE-0007: allocate across accounts, then run each account's waterfall."""
    # What was asked, kept in the event so the trail shows whether money
    # reached an account by the usual order or by the payer's direction.
    request = {
        "account_pks": sorted(str(pk) for pk in set(command.account_pks)),
        "allocation_method": command.allocation_method,
        "directed_allocations": {str(pk): amount for pk, amount in sorted(command.directed_allocations.items())},
        "bucket_amounts": {str(pk): dict(amounts) for pk, amounts in sorted(command.bucket_amounts.items())},
        "consumer_directed_accounts": sorted(str(pk) for pk in set(command.consumer_directed_accounts)),
        "received_at": command.received_at.isoformat() if command.received_at else None,
    }
    if Payment.objects.filter(pk=command.payment_pk).exists():
        first = store.load_stream(command.payment_pk).first()
        recorded = first.new_value if first is not None else {}
        # A payment recorded before a field was added to the request is the
        # same payment if it matches with that field at its default.
        asked = {"consumer_directed_accounts": [], **(recorded.get("request") or {})}
        same = (
            asked == request
            and recorded.get("amount_minor_units") == command.amount_minor_units
            and recorded.get("currency_code") == command.currency_code
        )
        if same:
            raise AlreadyApplied()
        raise CommandRejected("A different payment already exists under this key", "key_reused")
    accounts = _lock_accounts(command.account_pks)
    rows = _bucket_rows(accounts)
    known = {account.pk for account in accounts}

    if set(command.directed_allocations) & set(command.bucket_amounts):
        raise CommandRejected("An account may carry a directed amount or bucket amounts, not both", "direction_conflict")
    if not (set(command.directed_allocations) | set(command.bucket_amounts)) <= known:
        raise CommandRejected("Directions name an account that is not on this payment", "direction_off_payment")
    for account in accounts:
        if account.status != "active":
            raise CommandRejected(f"Account {account.pk} is not active", "account_not_active")
        if account.currency_code != command.currency_code:
            raise CommandRejected(f"Account {account.pk} is not held in {command.currency_code}", "currency_mismatch")

    # HIVE-0007's floor: a disputed account receives only what the consumer
    # directed to it. Explicit bucket amounts land on the account, so on a
    # disputed account the command must say the consumer directed them. The
    # form asks; this makes every other caller answer the same question.
    stated = set(command.consumer_directed_accounts)
    if not stated <= set(command.bucket_amounts):
        # The statement goes into the permanent record, so it may only be
        # made about an account whose bucket amounts this payment carries.
        raise CommandRejected(
            "consumer_directed_accounts names an account with no bucket amounts on this payment",
            "direction_off_payment",
        )
    for account in accounts:
        if account.is_disputed and account.pk in command.bucket_amounts and account.pk not in stated:
            raise CommandRejected(
                f"Account {account.pk} is disputed; bucket amounts need the consumer's direction",
                "dispute_needs_direction",
            )

    # Explicit bucket amounts are a direction to that account for their total.
    for account_pk, amounts in command.bucket_amounts.items():
        for code, amount in amounts.items():
            row = rows[account_pk].get(code)
            if row is None or amount > row.balance_minor_units:
                raise CommandRejected(f"Bucket {code} on account {account_pk} cannot take {amount}", "bucket_cannot_take")
    directed = dict(command.directed_allocations)
    directed.update({pk: sum(amounts.values()) for pk, amounts in command.bucket_amounts.items()})
    if sum(directed.values()) > command.amount_minor_units:
        raise CommandRejected("Directed amounts exceed the payment amount", "directed_exceeds_amount")

    positions = [
        AccountPosition(
            account_pk=account.pk,
            list_date=account.list_date,
            is_disputed=account.is_disputed,
            balance_minor_units=sum(row.balance_minor_units for row in rows[account.pk].values()),
        )
        for account in accounts
    ]
    # The fourth edge case HIVE-0007 proposes: an account with explicit bucket
    # amounts receives exactly those amounts. The rest of the payment spreads
    # over the other accounts and is otherwise held as unapplied.
    shares, unapplied = allocate_across_accounts(
        command.amount_minor_units,
        positions,
        command.allocation_method,
        directed,
        fixed=frozenset(command.bucket_amounts),
    )

    distribution = {}
    for account in accounts:
        if account.pk in command.bucket_amounts:
            paydown = dict(command.bucket_amounts[account.pk])
        else:
            paydown, _ = run_waterfall(
                shares[account.pk],
                [
                    (code, row.balance_bucket_definition.waterfall_priority, row.balance_minor_units)
                    for code, row in rows[account.pk].items()
                ],
            )
        if paydown:
            distribution[str(account.pk)] = paydown

    applied = sum(sum(paydown.values()) for paydown in distribution.values())
    if applied + unapplied != command.amount_minor_units:
        raise RuntimeError(
            f"Payment {command.payment_pk} does not balance: {applied} applied plus "
            f"{unapplied} unapplied is not {command.amount_minor_units}"
        )

    received_at = command.received_at or context.occurred_at
    payment = Payment(
        payment_pk=command.payment_pk,
        amount_minor_units=command.amount_minor_units,
        currency_code=command.currency_code,
        received_at=received_at,
        distribution=distribution,
        unapplied_minor_units=unapplied,
    )
    context.record(
        payment,
        "payment.posted",
        prior=None,
        new={
            "status": "posted",
            "amount_minor_units": payment.amount_minor_units,
            "currency_code": payment.currency_code,
            "received_at": received_at.isoformat(),
            "distribution": distribution,
            "unapplied_minor_units": unapplied,
            "request": request,
        },
        reason="payment_received",
    )
    for account in accounts:
        paydown = distribution.get(str(account.pk))
        if paydown:
            _post_to_buckets(
                context, account, rows[account.pk], paydown, -1,
                "account.payment_applied", "payment_received", payment,
            )


@handles(commands.ReversePayment)
def reverse_payment(command, context):
    """HIVE-0007: back out the recorded distribution, not a recomputed one."""
    payment = Payment.objects.select_for_update().filter(pk=command.payment_pk).first()
    if payment is None:
        raise CommandRejected("Payment does not exist", "payment_not_found")
    if payment.status != "posted":
        raise CommandRejected("Payment is not in a reversible state", "payment_not_reversible")
    accounts = _lock_accounts(payment.distribution)
    rows = _bucket_rows(accounts)

    payment.status = "reversed"
    context.record(
        payment,
        "payment.reversed",
        prior={"status": "posted"},
        new={"status": "reversed"},
        reason="payment_returned",
    )
    for account in accounts:
        _post_to_buckets(
            context, account, rows[account.pk], payment.distribution[str(account.pk)], 1,
            "account.payment_reversed", "payment_returned", payment,
        )


def _post_to_buckets(context, account, bucket_rows, amounts, sign, event_type, reason, payment):
    """Move bucket balances and write the account's fan-out event together."""
    prior, new = {}, {}
    for code, amount in amounts.items():
        row = bucket_rows[code]
        prior[code] = row.balance_minor_units
        row.balance_minor_units += sign * amount
        new[code] = row.balance_minor_units
        row.save()
    context.record(
        account,
        event_type,
        prior={"balance_buckets": prior},
        new={"balance_buckets": new},
        reason=reason,
        metadata={
            "payment_pk": str(payment.pk),
            "received_at": payment.received_at.isoformat(),
            "bucket_amounts": amounts,
        },
    )


# Foundation-written for the engine bake-off (WORKLOADS.md, "New commands").
# Each handler takes the account's row lock first, decides under that lock,
# and writes its event and state in the command's one transaction. A command
# that would change nothing returns without writing, which dispatch answers
# as success: that is how a retried or redelivered command stays harmless.


def _instant(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@handles(commands.AccrueInterest)
def accrue_interest(command, context):
    (account,) = _lock_accounts([command.account_pk])
    if account.status != "active" or command.accrual_date <= account.interest_accrued_through:
        return
    rows = _bucket_rows([account])[account.pk]
    principal = rows["principal"].balance_minor_units if "principal" in rows else 0
    days = (command.accrual_date - account.interest_accrued_through).days
    amount = principal * command.annual_rate_basis_points * days // (10000 * 365)
    interest = rows.get("interest")
    if interest is None:
        interest = AccountBalanceBucket(
            account=account,
            balance_bucket_definition=BalanceBucketDefinition.objects.get(code="interest"),
            balance_minor_units=0,
        )
    prior = {
        "balance_buckets": {"interest": interest.balance_minor_units},
        "interest_accrued_through": account.interest_accrued_through.isoformat(),
    }
    interest.balance_minor_units += amount
    interest.save()
    account.interest_accrued_through = command.accrual_date
    context.record(
        account,
        "account.interest_accrued",
        prior=prior,
        new={
            "balance_buckets": {"interest": interest.balance_minor_units},
            "interest_accrued_through": command.accrual_date.isoformat(),
        },
        reason="interest_accrued",
        metadata={
            "days": days,
            "annual_rate_basis_points": command.annual_rate_basis_points,
            "amount_minor_units": amount,
        },
    )


@handles(commands.ChangeAccountStatus)
def change_account_status(command, context):
    (account,) = _lock_accounts([command.account_pk])
    if account.status == command.status:
        return
    prior = {"status": account.status}
    account.status = command.status
    context.record(account, "account.status_changed", prior=prior, new={"status": command.status}, reason=command.reason_code)


@handles(commands.AssignToQueue)
def assign_to_queue(command, context):
    (account,) = _lock_accounts([command.account_pk])
    if account.queue_code == command.queue_code:
        return
    prior = {"queue_code": account.queue_code}
    account.queue_code = command.queue_code
    context.record(account, "account.queue_assigned", prior=prior, new={"queue_code": command.queue_code}, reason="queue_assigned")


@handles(commands.ScheduleFutureEvent)
def schedule_future_event(command, context):
    """WAX-0027: write the scheduling event and the register row together."""
    (account,) = _lock_accounts([command.account_pk])
    existing = PendingEvent.objects.filter(pk=command.pending_event_pk).first()
    if existing is not None:
        same = (
            existing.aggregate_pk_ref == account.pk
            and existing.target_event_type == command.target_event_type
            and existing.scheduled_by_event == command.scheduled_by_event
            and existing.due_at == command.due_at
        )
        if same:
            raise AlreadyApplied()
        raise CommandRejected("A different pending event already exists under this key", "key_reused")
    context.record(
        account,
        "pending_event.scheduled",
        prior=None,
        new={
            "pending_event_pk": str(command.pending_event_pk),
            "target_event_type": command.target_event_type,
            "due_at": _instant(command.due_at),
            "scheduled_by_event": str(command.scheduled_by_event),
        },
        reason="pending_event_scheduled",
    )
    PendingEvent(
        pending_event_pk=command.pending_event_pk,
        aggregate_pk_ref=account.pk,
        aggregate_type=account.aggregate_type,
        target_event_type=command.target_event_type,
        due_at=command.due_at,
        status="pending",
        scheduled_by_event=command.scheduled_by_event,
    ).save(force_insert=True)


@handles(commands.FirePendingEvent)
def fire_pending_event(command, context):
    """WAX-0027: claim the register row, write its target event on the account,
    and mark the row fired, all in one transaction. A row another poller is
    firing is locked and passed over; a row already fired is left alone."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT aggregate_pk_ref, target_event_type, due_at, status FROM core.pending_event"
            " WHERE pending_event_pk = %s FOR UPDATE SKIP LOCKED",
            [command.pending_event_pk],
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute("SELECT 1 FROM core.pending_event WHERE pending_event_pk = %s", [command.pending_event_pk])
            if cursor.fetchone() is None:
                raise CommandRejected("Pending event does not exist", "pending_event_not_found")
            return
    aggregate_pk, target_event_type, due_at, status = row
    if status != "pending":
        return
    (account,) = _lock_accounts([aggregate_pk])
    context.record(
        account,
        target_event_type,
        prior=None,
        new={"due_at": _instant(due_at)},
        reason="pending_event_fired",
        metadata={"pending_event_pk": str(command.pending_event_pk)},
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.pending_event SET status = 'fired', fired_at = clock_timestamp() WHERE pending_event_pk = %s",
            [command.pending_event_pk],
        )


@handles(commands.CreateTask)
def create_task(command, context):
    (account,) = _lock_accounts([command.account_pk])
    existing = Task.objects.filter(pk=command.task_pk).first()
    if existing is not None:
        if existing.account_id == account.pk and existing.task_type == command.task_type:
            raise AlreadyApplied()
        raise CommandRejected("A different task already exists under this key", "key_reused")
    Task(
        task_pk=command.task_pk, account=account, task_type=command.task_type, status="open", created_at=context.occurred_at
    ).save(force_insert=True)
    context.record(
        account,
        "task.created",
        prior=None,
        new={"task_pk": str(command.task_pk), "task_type": command.task_type, "status": "open"},
        reason="task_created",
    )
