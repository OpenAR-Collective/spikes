"""The bake-off's rule set (WORKLOADS.md, "Rules"), the same on both sides.
Foundation-written.

Each rule returns the command its action sends, keyed so that a second
delivery of the same event is answered as success.
"""

from datetime import timedelta

from django.db import connection

from bakeoff.keys import derived_uuid
from hivear.commands import AssignToQueue, ChangeAccountStatus, CreateTask, ScheduleFutureEvent
from wax.rules import rule

# One simulated day, in real seconds; run-engine sets it from --seconds-per-day.
SETTINGS = {"seconds_per_day": 2.0}


def _days(n):
    return timedelta(seconds=n * SETTINGS["seconds_per_day"])


@rule("paid_in_full", "account.payment_applied")
def mark_paid_in_full(event):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT a.status, coalesce(sum(b.balance_minor_units), 0) FROM ar.account a"
            " LEFT JOIN ar.account_balance_bucket b ON b.account_fk = a.account_pk"
            " WHERE a.account_pk = %s GROUP BY a.status",
            [event.aggregate_pk_ref],
        )
        row = cursor.fetchone()
    if row is None:
        return []
    status, total = row
    if status == "active" and total == 0:
        return [ChangeAccountStatus(account_pk=event.aggregate_pk_ref, status="paid_in_full", reason_code="paid_in_full")]
    return []


@rule("schedule_dispute_review", "dispute.opened")
def schedule_dispute_review(event):
    return [
        ScheduleFutureEvent(
            pending_event_pk=derived_uuid(f"pending:{event.event_pk}"),
            account_pk=event.aggregate_pk_ref,
            target_event_type="account.dispute_review_due",
            due_at=event.occurred_at + _days(30),
            scheduled_by_event=event.event_pk,
        )
    ]


@rule("queue_dispute_review", "account.dispute_review_due")
def queue_dispute_review(event):
    return [AssignToQueue(account_pk=event.aggregate_pk_ref, queue_code="dispute_review")]


@rule("schedule_first_contact", "account.placed")
def schedule_first_contact(event):
    return [
        ScheduleFutureEvent(
            pending_event_pk=derived_uuid(f"pending:{event.event_pk}"),
            account_pk=event.aggregate_pk_ref,
            target_event_type="account.first_contact_due",
            due_at=event.occurred_at + _days(1),
            scheduled_by_event=event.event_pk,
        )
    ]


@rule("first_contact_task", "account.first_contact_due")
def create_first_contact_task(event):
    pending_event_pk = event.metadata["pending_event_pk"]
    return [
        CreateTask(
            task_pk=derived_uuid(f"task:{pending_event_pk}"),
            account_pk=event.aggregate_pk_ref,
            task_type="first_contact",
        )
    ]
