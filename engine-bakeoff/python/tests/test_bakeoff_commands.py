"""The six commands the engine bake-off adds (WORKLOADS.md, "New commands").
Foundation-written.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from hivear import commands
from hivear.models import Account, Task
from tests.conftest import balances
from wax import store
from wax.commands import CommandRejected
from wax.models import Event, PendingEvent


def events(account_pk):
    return list(store.load_stream(account_pk).values_list("event_type", flat=True))


@pytest.mark.django_db
def test_placing_records_interest_accrued_through_defaulting_to_itemization(place):
    account_pk = place()
    account = Account.objects.get(pk=account_pk)
    assert account.interest_accrued_through == account.itemization_date
    placed = store.load_stream(account_pk).first()
    assert placed.new_value["interest_accrued_through"] == account.itemization_date.isoformat()


@pytest.mark.django_db
def test_accrual_adds_the_specified_amount_once(place, run):
    account_pk = place(principal=365_000, interest=1_000)
    # Itemized 2025-12-31, accrued to 2026-01-30: 30 days at 1,800 basis points.
    command = commands.AccrueInterest(account_pk=account_pk, accrual_date=date(2026, 1, 30), annual_rate_basis_points=1800)
    run(command)
    assert balances(account_pk)["interest"] == 1_000 + 365_000 * 1800 * 30 // 3_650_000
    assert Account.objects.get(pk=account_pk).interest_accrued_through == date(2026, 1, 30)
    accrued = store.load_stream(account_pk).last()
    assert accrued.event_type == "account.interest_accrued"
    assert accrued.prior_value == {"balance_buckets": {"interest": 1_000}, "interest_accrued_through": "2025-12-31"}
    assert accrued.new_value == {"balance_buckets": {"interest": 6_400}, "interest_accrued_through": "2026-01-30"}
    assert accrued.metadata == {"days": 30, "annual_rate_basis_points": 1800, "amount_minor_units": 5_400}

    run(command)
    assert events(account_pk).count("account.interest_accrued") == 1


@pytest.mark.django_db
def test_accrual_of_zero_still_advances_the_date_and_adds_the_bucket(place, run):
    # No interest bucket yet; an accrual of zero still writes the event.
    account_pk = place(principal=10)
    run(commands.AccrueInterest(account_pk=account_pk, accrual_date=date(2026, 1, 1), annual_rate_basis_points=1800))
    assert balances(account_pk)["interest"] == 0
    assert Account.objects.get(pk=account_pk).interest_accrued_through == date(2026, 1, 1)
    assert events(account_pk)[-1] == "account.interest_accrued"


@pytest.mark.django_db
def test_accrual_on_an_inactive_account_writes_nothing(place, run):
    account_pk = place()
    run(commands.ChangeAccountStatus(account_pk=account_pk, status="paid_in_full", reason_code="paid_in_full"))
    before = events(account_pk)
    run(commands.AccrueInterest(account_pk=account_pk, accrual_date=date(2026, 6, 1), annual_rate_basis_points=1800))
    assert events(account_pk) == before


@pytest.mark.django_db
def test_status_and_queue_changes_are_idempotent(place, run):
    account_pk = place()
    for _ in range(2):
        run(commands.ChangeAccountStatus(account_pk=account_pk, status="paid_in_full", reason_code="paid_in_full"))
        run(commands.AssignToQueue(account_pk=account_pk, queue_code="dispute_review"))
    account = Account.objects.get(pk=account_pk)
    assert (account.status, account.queue_code) == ("paid_in_full", "dispute_review")
    assert events(account_pk).count("account.status_changed") == 1
    assert events(account_pk).count("account.queue_assigned") == 1
    changed = Event.objects.get(aggregate_pk_ref=account_pk, event_type="account.queue_assigned")
    assert (changed.prior_value, changed.new_value) == ({"queue_code": None}, {"queue_code": "dispute_review"})


@pytest.mark.django_db
def test_an_unknown_reason_code_is_refused(place, run):
    account_pk = place()
    with pytest.raises(CommandRejected):
        run(commands.ChangeAccountStatus(account_pk=account_pk, status="closed", reason_code="no_such_reason"))


@pytest.mark.django_db
def test_schedule_then_fire_writes_the_target_event_once(place, run):
    account_pk = place()
    trigger = store.load_stream(account_pk).first().event_pk
    key = uuid.uuid4()
    due = datetime.now(UTC) - timedelta(seconds=1)
    schedule = commands.ScheduleFutureEvent(
        pending_event_pk=key, account_pk=account_pk, target_event_type="account.first_contact_due",
        due_at=due, scheduled_by_event=trigger,
    )
    run(schedule)
    run(schedule)
    assert events(account_pk).count("pending_event.scheduled") == 1
    scheduled = Event.objects.get(aggregate_pk_ref=account_pk, event_type="pending_event.scheduled")
    assert scheduled.new_value == {
        "pending_event_pk": str(key),
        "target_event_type": "account.first_contact_due",
        "due_at": due.isoformat(timespec="microseconds"),
        "scheduled_by_event": str(trigger),
    }
    assert PendingEvent.objects.get(pk=key).status == "pending"

    with pytest.raises(CommandRejected):
        run(schedule.model_copy(update={"target_event_type": "account.dispute_review_due"}))

    run(commands.FirePendingEvent(pending_event_pk=key))
    run(commands.FirePendingEvent(pending_event_pk=key))
    fired = Event.objects.get(aggregate_pk_ref=account_pk, event_type="account.first_contact_due")
    assert fired.metadata == {"pending_event_pk": str(key)}
    assert fired.new_value == {"due_at": due.isoformat(timespec="microseconds")}
    entry = PendingEvent.objects.get(pk=key)
    assert entry.status == "fired" and entry.fired_at is not None


@pytest.mark.django_db
def test_firing_an_unknown_entry_is_refused(run):
    with pytest.raises(CommandRejected):
        run(commands.FirePendingEvent(pending_event_pk=uuid.uuid4()))


@pytest.mark.django_db
def test_a_task_is_created_once(place, run):
    account_pk = place()
    key = uuid.uuid4()
    run(commands.CreateTask(task_pk=key, account_pk=account_pk, task_type="first_contact"))
    run(commands.CreateTask(task_pk=key, account_pk=account_pk, task_type="first_contact"))
    assert Task.objects.filter(account_id=account_pk).count() == 1
    assert events(account_pk).count("task.created") == 1
    created = Event.objects.get(aggregate_pk_ref=account_pk, event_type="task.created")
    assert created.new_value == {"task_pk": str(key), "task_type": "first_contact", "status": "open"}
