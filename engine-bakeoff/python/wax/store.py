"""Event store abstraction layer (WAX-0001 Enriched Event Sourcing).

The three operations WAX-0001 defines: append and load_stream here,
subscribe in wax.projections. Nothing else in the codebase writes core.event.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, connection
from psycopg.types.json import Jsonb

from wax.hashing import compute_event_hash
from wax.models import Event


class ConcurrencyError(Exception):
    """Another writer advanced the aggregate past expected_version."""


@dataclass(frozen=True)
class NewEvent:
    event_type: str
    prior_value: dict | None
    new_value: dict | None
    metadata: dict | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class Attribution:
    """Who, when, and why: the non-payload columns shared by a command's events."""

    actor_pk: uuid.UUID
    actor_type: str
    occurred_at: datetime
    reason_code_fk: uuid.UUID | None = None
    source: str | None = None
    correlation_pk: uuid.UUID | None = None
    correlation_type: str | None = None


_INSERT = """
INSERT INTO core.event (
    event_pk, aggregate_sequence_number, event_type, schema_version,
    aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type,
    prior_value, new_value, reason_code_fk, source, correlation_pk_ref,
    correlation_type, metadata, prior_event_hash, event_hash
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING global_sequence_number
"""


def _jsonb(value):
    return None if value is None else Jsonb(value)


def append(aggregate_type, aggregate_pk, expected_version, events, attribution) -> tuple[int, int]:
    """Append events to one aggregate's stream. Returns its new version and
    the global_sequence_number of the last event written.

    Must run inside the command transaction so the events commit or roll
    back with the state tables. The caller holds the row lock on the
    aggregate's state row (WAX-0013); the unique constraint on
    (aggregate_pk_ref, aggregate_sequence_number) is the backstop.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("append must run inside the command transaction")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT aggregate_sequence_number, event_hash FROM core.event "
            "WHERE aggregate_pk_ref = %s ORDER BY aggregate_sequence_number DESC LIMIT 1",
            [aggregate_pk],
        )
        row = cursor.fetchone()
        version, prior_hash = row if row else (0, None)
        position = 0
        if version != expected_version:
            raise ConcurrencyError(
                f"{aggregate_type} {aggregate_pk}: expected version {expected_version}, found {version}"
            )

        for event in events:
            version += 1
            event_hash = compute_event_hash(
                aggregate_type=aggregate_type,
                aggregate_pk_ref=aggregate_pk,
                event_type=event.event_type,
                schema_version=event.schema_version,
                prior_value=event.prior_value,
                new_value=event.new_value,
                occurred_at=attribution.occurred_at,
                actor_pk_ref=attribution.actor_pk,
                actor_type=attribution.actor_type,
                reason_code_fk=attribution.reason_code_fk,
                prior_event_hash=prior_hash,
            )
            try:
                cursor.execute(
                    _INSERT,
                    [
                        uuid.uuid4(), version, event.event_type, event.schema_version,
                        aggregate_pk, aggregate_type, attribution.occurred_at,
                        attribution.actor_pk, attribution.actor_type,
                        _jsonb(event.prior_value), _jsonb(event.new_value),
                        attribution.reason_code_fk, attribution.source,
                        attribution.correlation_pk, attribution.correlation_type,
                        _jsonb(event.metadata), prior_hash, event_hash,
                    ],
                )
                position = cursor.fetchone()[0]
            except IntegrityError as exc:
                raise ConcurrencyError(str(exc)) from exc
            prior_hash = event_hash
    return version, position


def load_stream(aggregate_pk, from_version=1):
    """Events for one aggregate in aggregate_sequence_number order."""
    return Event.objects.filter(
        aggregate_pk_ref=aggregate_pk, aggregate_sequence_number__gte=from_version
    ).order_by("aggregate_sequence_number")


def verify_stream(aggregate_pk) -> list[str]:
    """Recompute one aggregate's hash chain and return any problems found."""
    problems = []
    prior_hash = None
    expected_sequence = 0
    for event in load_stream(aggregate_pk):
        expected_sequence += 1
        if event.aggregate_sequence_number != expected_sequence:
            problems.append(f"gap before sequence {event.aggregate_sequence_number}")
            expected_sequence = event.aggregate_sequence_number
        if event.prior_event_hash != prior_hash:
            problems.append(f"sequence {expected_sequence}: prior_event_hash does not match predecessor")
        recomputed = compute_event_hash(
            aggregate_type=event.aggregate_type,
            aggregate_pk_ref=event.aggregate_pk_ref,
            event_type=event.event_type,
            schema_version=event.schema_version,
            prior_value=event.prior_value,
            new_value=event.new_value,
            occurred_at=event.occurred_at,
            actor_pk_ref=event.actor_pk_ref,
            actor_type=event.actor_type,
            reason_code_fk=event.reason_code_fk,
            prior_event_hash=event.prior_event_hash,
        )
        if recomputed != event.event_hash:
            problems.append(f"sequence {expected_sequence}: event_hash does not match content")
        prior_hash = event.event_hash
    return problems


def reason_labels(reference_value_pks):
    """(reference_value_pk, label) for the reason codes on a page of events."""
    if not reference_value_pks:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT reference_value_pk, label FROM core.reference_value WHERE reference_value_pk = ANY(%s)",
            [list(reference_value_pks)],
        )
        return cursor.fetchall()


def current_position() -> int:
    """The sequence position now. At or past every event already written."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.seq_event_global_sequence")
        return cursor.fetchone()[0]
