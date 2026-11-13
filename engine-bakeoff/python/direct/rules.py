"""The rule runner of the python-direct engine. Foundation-written.

It is wax/rules.py's runner and bakeoff/rules.py's five rules on a psycopg
connection, with no Django. Delivery is the same: a runner reads the events
its rules care about, in global order, past its position and up to the
reader's high-water mark; sends each matching rule's actions through the
direct command path, each in its own transaction; and moves its position only
after they have committed, so a crash means a second delivery, never a lost
one. The actions are keyed commands, so a second delivery changes nothing.
Partitions take the aggregates whose key hashes to them, which keeps every
aggregate's events in order. The checkpoint names are wax/rules.py's, so the
harness reads this runner's progress unchanged. tests/test_direct_rules.py
requires the events and state this runner leaves to match the Django
runner's.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from bakeoff.keys import derived_uuid
from direct.commands import CommandRejected, dispatch
from direct.db import as_json
from direct.reader import HighWaterMark
from hivear.commands import AssignToQueue, ChangeAccountStatus, CreateTask, ScheduleFutureEvent

logger = logging.getLogger("direct.rules")
_rules = {}

# wax/rules.py's: the position a seed loader sets, so that every runner
# partition created later starts at the head of the seed.
BASE_POSITION = "wax.rules:base"

# One simulated day, in real seconds; run-engine sets it from --seconds-per-day.
SETTINGS = {"seconds_per_day": 2.0}


@dataclass(frozen=True)
class RuleEvent:
    event_pk: UUID
    global_sequence_number: int
    event_type: str
    aggregate_pk_ref: UUID
    aggregate_type: str
    occurred_at: datetime
    new_value: dict | None
    metadata: dict | None


def rule(name, *event_types):
    """Register a rule. The function receives a cursor and a RuleEvent and
    returns the commands to send, which may be none."""

    def register(function):
        for event_type in event_types:
            _rules.setdefault(event_type, []).append((name, function))
        return function

    return register


def _days(n):
    return timedelta(seconds=n * SETTINGS["seconds_per_day"])


# bakeoff/rules.py's rules, each keyed so that a second delivery of the same
# event is answered as success.

@rule("paid_in_full", "account.payment_applied")
def mark_paid_in_full(cur, event):
    cur.execute(
        "SELECT a.status, coalesce(sum(b.balance_minor_units), 0) FROM ar.account a"
        " LEFT JOIN ar.account_balance_bucket b ON b.account_fk = a.account_pk"
        " WHERE a.account_pk = %s GROUP BY a.status",
        [event.aggregate_pk_ref],
    )
    row = cur.fetchone()
    if row is None:
        return []
    status, total = row
    if status == "active" and total == 0:
        return [ChangeAccountStatus(account_pk=event.aggregate_pk_ref, status="paid_in_full", reason_code="paid_in_full")]
    return []


@rule("schedule_dispute_review", "dispute.opened")
def schedule_dispute_review(cur, event):
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
def queue_dispute_review(cur, event):
    return [AssignToQueue(account_pk=event.aggregate_pk_ref, queue_code="dispute_review")]


@rule("schedule_first_contact", "account.placed")
def schedule_first_contact(cur, event):
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
def create_first_contact_task(cur, event):
    pending_event_pk = event.metadata["pending_event_pk"]
    return [
        CreateTask(
            task_pk=derived_uuid(f"task:{pending_event_pk}"),
            account_pk=event.aggregate_pk_ref,
            task_type="first_contact",
        )
    ]


class RuleRunner:
    def __init__(self, session, *, actor_pk, partition=0, partitions=1, batch_size=500):
        self.session = session
        self.actor_pk = actor_pk
        self.partition = partition
        self.partitions = partitions
        self.batch_size = batch_size
        self.name = f"wax.rules:{partition}/{partitions}"
        self.mark = HighWaterMark()
        self.counts = {"delivered": 0, "actions": 0, "rejected": 0}

    def position(self):
        with self.session.conn.cursor() as cur:
            cur.execute(
                "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s", [self.name]
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s",
                [BASE_POSITION],
            )
            base = cur.fetchone()
            cur.execute(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest, last_global_sequence_number)"
                " VALUES (%s, 'rules', %s) ON CONFLICT (projection_name) DO NOTHING",
                [self.name, base[0] if base else 0],
            )
        return self.position()

    def run_once(self) -> int:
        """Deliver at most one batch. Returns how many events it delivered."""
        conn = self.session.conn
        safe = self.mark.advance(conn)
        position = self.position()
        if position >= safe:
            return 0
        sql = (
            "SELECT event_pk, global_sequence_number, event_type, aggregate_pk_ref, aggregate_type, occurred_at,"
            " new_value, metadata FROM core.event"
            " WHERE global_sequence_number > %s AND global_sequence_number <= %s AND event_type = ANY(%s)"
        )
        params = [position, safe, sorted(_rules)]
        if self.partitions > 1:
            # Masked rather than abs(), which overflows for a hash of -2**31.
            sql += " AND (hashtext(aggregate_pk_ref::text) & 2147483647) %% %s = %s"
            params += [self.partitions, self.partition]
        sql += " ORDER BY global_sequence_number LIMIT %s"
        params.append(self.batch_size)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            events = [RuleEvent(*row[:6], as_json(row[6]), as_json(row[7])) for row in cur.fetchall()]
            for event in events:
                for name, function in _rules.get(event.event_type, []):
                    for command in function(cur, event) or ():
                        self._act(name, command, event)
                self.counts["delivered"] += 1
            # Nothing at or below the mark can still appear, so a short batch
            # may carry the position to the mark itself.
            new_position = events[-1].global_sequence_number if len(events) == self.batch_size else safe
            cur.execute(
                "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, updated_at = now()"
                " WHERE projection_name = %s",
                [new_position, self.name],
            )
        return len(events)

    def _act(self, name, command, event):
        try:
            dispatch(
                self.session,
                command,
                actor_pk=self.actor_pk,
                actor_type="system",
                source="rule",
                correlation_pk=event.event_pk,
                correlation_type="rule",
            )
            self.counts["actions"] += 1
        except CommandRejected as exc:
            # A refusal is an answer, not a fault: the rule's condition no
            # longer held when its action ran. It is not retried.
            self.counts["rejected"] += 1
            logger.debug("rule %s: %s refused for event %s: %s", name, type(command).__name__, event.event_pk, exc)


def run_forever(actor_pk, partition, partitions, idle_seconds):
    """One rule runner partition of the python-direct engine."""
    from direct import handlers  # noqa: F401  (registers the handlers)
    from direct.commands import Session
    from direct.db import connect

    runner = None
    while True:
        try:
            if runner is None:
                runner = RuleRunner(Session(connect()), actor_pk=actor_pk, partition=partition, partitions=partitions)
                logger.info("Rule runner %s started", runner.name)
            delivered = runner.run_once()
        except Exception:
            # The position did not move, so the batch is delivered again.
            logger.exception("Rule runner %s/%s: pass failed; reconnecting", partition, partitions)
            try:
                runner.session.conn.close()
            except Exception:
                pass
            runner = None
            delivered = 0
            time.sleep(1)
        if delivered == 0:
            time.sleep(idle_seconds)
