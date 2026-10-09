"""Post-event rules (WAX-0023): "when X happens, do Y", run from the event stream.

Foundation-written for the engine bake-off (WORKLOADS.md, "Rules"); not part
of Andy Troiano's prototype. It reuses his projection reader's high-water
mark, so it never skips an event whose transaction committed late.

Delivery is at least once and in global order. A runner reads the events its
rules care about, past its position and up to the high-water mark, runs each
matching rule, and sends each action through the normal command path in its
own transaction. Only after the actions have committed does it advance its
position, so a crash means a second delivery, never a lost one. The actions
are keyed commands, so a second delivery is answered as success and changes
nothing. A runner may be one of several partitions; each takes the
aggregates whose key hashes to its partition, which keeps every aggregate's
events in order.
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import close_old_connections, connection

from wax.commands import CommandRejected, dispatch
from wax.projections import HighWaterMark

logger = logging.getLogger("wax.rules")
_rules = {}

# The position a seed loader sets, so that every runner partition created
# later starts at the head of the seed instead of replaying it.
BASE_POSITION = "wax.rules:base"


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
    """Register a rule. The function receives a RuleEvent and returns the
    commands to send, which may be none."""

    def register(function):
        for event_type in event_types:
            _rules.setdefault(event_type, []).append((name, function))
        return function

    return register


def registered_event_types():
    return sorted(_rules)


class RuleRunner:
    def __init__(self, *, actor_pk, partition=0, partitions=1, batch_size=500):
        self.actor_pk = actor_pk
        self.partition = partition
        self.partitions = partitions
        self.batch_size = batch_size
        self.name = f"wax.rules:{partition}/{partitions}"
        self.mark = HighWaterMark()
        self.counts = {"delivered": 0, "actions": 0, "rejected": 0}

    def position(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s", [self.name]
            )
            row = cursor.fetchone()
            if row:
                return row[0]
            cursor.execute(
                "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s",
                [BASE_POSITION],
            )
            base = cursor.fetchone()
            cursor.execute(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest, last_global_sequence_number)"
                " VALUES (%s, 'rules', %s) ON CONFLICT (projection_name) DO NOTHING",
                [self.name, base[0] if base else 0],
            )
        return self.position()

    def run_once(self) -> int:
        """Deliver at most one batch. Returns how many events it delivered."""
        safe = self.mark.advance()
        position = self.position()
        if position >= safe:
            return 0
        sql = (
            "SELECT event_pk, global_sequence_number, event_type, aggregate_pk_ref, aggregate_type, occurred_at,"
            " new_value, metadata FROM core.event"
            " WHERE global_sequence_number > %s AND global_sequence_number <= %s AND event_type = ANY(%s)"
        )
        params = [position, safe, registered_event_types()]
        if self.partitions > 1:
            # Masked rather than abs(), which overflows for a hash of -2**31.
            sql += " AND (hashtext(aggregate_pk_ref::text) & 2147483647) %% %s = %s"
            params += [self.partitions, self.partition]
        sql += " ORDER BY global_sequence_number LIMIT %s"
        params.append(self.batch_size)
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            # Django loads jsonb as text on its connections; decode it here.
            events = [
                RuleEvent(*row[:6], *(json.loads(v) if isinstance(v, str) else v for v in row[6:]))
                for row in cursor.fetchall()
            ]
        for event in events:
            for name, function in _rules.get(event.event_type, []):
                for command in function(event) or ():
                    self._act(name, command, event)
            self.counts["delivered"] += 1
        # Nothing at or below the mark can still appear, so a short batch may
        # carry the position to the mark itself.
        new_position = events[-1].global_sequence_number if len(events) == self.batch_size else safe
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, updated_at = now()"
                " WHERE projection_name = %s",
                [new_position, self.name],
            )
        return len(events)

    def _act(self, name, command, event):
        try:
            dispatch(
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

    def loop(self, idle_seconds, stop=None):
        logger.info("Rule runner %s started", self.name)
        while stop is None or not stop.is_set():
            close_old_connections()
            try:
                delivered = self.run_once()
            except Exception:
                # The position did not move, so the batch is delivered again.
                logger.exception("Rule runner %s: pass failed; retrying", self.name)
                self.mark = HighWaterMark()
                delivered = 0
                time.sleep(1)
            if delivered == 0:
                time.sleep(idle_seconds)
