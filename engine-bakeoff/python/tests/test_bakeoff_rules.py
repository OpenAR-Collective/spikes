"""The rule runner, the register poller, and the projections over the
bake-off's events (WORKLOADS.md, "Rules"). Foundation-written.

These tests commit for real, because the readers see only committed
transactions. core.event and the reader positions outlive each test, so each
test first moves every reader to the head of the stream.
"""

import time
import uuid
from datetime import date

import pytest
from django.db import connection

from bakeoff import rules as bakeoff_rules
from bakeoff.keys import REGISTER, RULE_RUNNER, derived_uuid
from hivear import commands
from hivear.models import Account, AccountSummaryReadModel, Task
from tests.conftest import balances
from wax import projections, register, store
from wax.commands import dispatch
from wax.models import Event, PendingEvent
from wax.rules import RuleRunner


@pytest.fixture
def at_head():
    projections.register_checkpoints()
    head = store.current_position()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, failed_at = NULL,"
            " failure_kind = NULL, failed_global_sequence_number = NULL, failure_detail = NULL",
            [head],
        )
    runner = RuleRunner(actor_pk=RULE_RUNNER)
    runner.position()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s WHERE projection_name = %s",
            [head, runner.name],
        )
    bakeoff_rules.SETTINGS["seconds_per_day"] = 0.001
    yield runner
    bakeoff_rules.SETTINGS["seconds_per_day"] = 2.0


def drain(runner):
    while runner.run_once():
        pass


def fire(key):
    dispatch(commands.FirePendingEvent(pending_event_pk=key), actor_pk=REGISTER, actor_type="system", source="register")


def poll_until_quiet():
    time.sleep(0.05)
    while register.poll_once(fire):
        pass


@pytest.mark.django_db(transaction=True)
def test_a_new_account_gets_one_first_contact_task(at_head, place):
    account_pk = place()
    drain(at_head)
    placed = store.load_stream(account_pk).first()
    entry = PendingEvent.objects.get(pk=derived_uuid(f"pending:{placed.event_pk}"))
    assert entry.target_event_type == "account.first_contact_due"
    scheduled = Event.objects.get(aggregate_pk_ref=account_pk, event_type="pending_event.scheduled")
    assert (scheduled.correlation_pk_ref, scheduled.correlation_type) == (placed.event_pk, "rule")
    assert (scheduled.actor_pk_ref, scheduled.actor_type, scheduled.source) == (RULE_RUNNER, "system", "rule")

    poll_until_quiet()
    entry.refresh_from_db()
    assert entry.status == "fired" and entry.fired_at >= entry.due_at
    drain(at_head)
    task = Task.objects.get(account_id=account_pk)
    assert task.pk == derived_uuid(f"task:{entry.pk}") and task.task_type == "first_contact"


@pytest.mark.django_db(transaction=True)
def test_a_dispute_is_queued_for_review_when_its_review_comes_due(at_head, place, run):
    account_pk = place()
    run(commands.OpenDispute(account_pk=account_pk))
    drain(at_head)
    poll_until_quiet()
    drain(at_head)
    assert Account.objects.get(pk=account_pk).queue_code == "dispute_review"
    due = Event.objects.get(aggregate_pk_ref=account_pk, event_type="account.dispute_review_due")
    queued = Event.objects.get(aggregate_pk_ref=account_pk, event_type="account.queue_assigned")
    assert queued.correlation_pk_ref == due.event_pk


@pytest.mark.django_db(transaction=True)
def test_a_payoff_marks_the_account_paid_in_full(at_head, place, run):
    account_pk = place(principal=10_000, interest=500)
    run(commands.PostPayment(account_pks=[account_pk], amount_minor_units=2_000_000, currency_code="USD"))
    drain(at_head)
    assert Account.objects.get(pk=account_pk).status == "paid_in_full"
    assert sum(balances(account_pk).values()) == 0


@pytest.mark.django_db(transaction=True)
def test_a_partial_payment_leaves_the_account_active(at_head, place, run):
    account_pk = place(principal=10_000)
    run(commands.PostPayment(account_pks=[account_pk], amount_minor_units=500, currency_code="USD"))
    drain(at_head)
    assert Account.objects.get(pk=account_pk).status == "active"


@pytest.mark.django_db(transaction=True)
def test_a_second_delivery_changes_nothing(at_head, place, run):
    start = store.current_position()
    account_pk = place(principal=1_000)
    run(commands.OpenDispute(account_pk=account_pk))
    run(commands.PostPayment(account_pks=[account_pk], amount_minor_units=2_000_000, currency_code="USD"))
    drain(at_head)
    poll_until_quiet()
    drain(at_head)
    count = Event.objects.filter(aggregate_pk_ref=account_pk).count()
    tasks = Task.objects.count()
    # Deliver everything again, as a crash before the position moved would.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s WHERE projection_name = %s",
            [start, at_head.name],
        )
    drain(at_head)
    poll_until_quiet()
    drain(at_head)
    assert Event.objects.filter(aggregate_pk_ref=account_pk).count() == count
    assert Task.objects.count() == tasks


@pytest.mark.django_db(transaction=True)
def test_the_account_summary_follows_the_new_events(at_head, place, run):
    account_pk = place(principal=365_000, interest=1_000)
    run(commands.AccrueInterest(account_pk=account_pk, accrual_date=date(2026, 1, 30), annual_rate_basis_points=1800))
    run(commands.AssignToQueue(account_pk=account_pk, queue_code="dispute_review"))
    run(commands.CreateTask(task_pk=uuid.uuid4(), account_pk=account_pk, task_type="first_contact"))
    run(commands.ChangeAccountStatus(account_pk=account_pk, status="paid_in_full", reason_code="paid_in_full"))
    while projections.run_once():
        pass
    assert all(row["failed_at"] is None for row in projections.status())
    summary = AccountSummaryReadModel.objects.get(pk=account_pk)
    assert summary.balance_minor_units == sum(balances(account_pk).values())
    assert summary.balance_buckets["interest"] == 6_400
    assert summary.status == "paid_in_full"
