"""Andy Troiano's event store logic, wax/store.py's append, on a psycopg
cursor. Foundation-written for the python-direct side.

The logic is his: read the aggregate's head, refuse the write if the version
has moved, hash each event chained to the one before, and insert it in the
caller's transaction, with the unique constraint on the aggregate's version
as the backstop. Two things are re-expressed for plain SQL. The heads of
every aggregate a command touches are read in one statement, and a command's
events are inserted in one multi-row statement, in the order they were
recorded, so the insert trigger numbers them as his loop would.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from wax import hashing
from wax.hashing import compute_event_hash


class ConcurrencyError(Exception):
    """Another writer advanced the aggregate past the expected version."""


@dataclass(frozen=True)
class Attribution:
    actor_pk: uuid.UUID
    actor_type: str
    occurred_at: datetime
    source: str | None = None
    correlation_pk: uuid.UUID | None = None
    correlation_type: str | None = None


_INSERT_COLUMNS = (
    "event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type,"
    " occurred_at, actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, source,"
    " correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash"
)


def _jsonb(value):
    return None if value is None else Jsonb(value)


HEADS_SQL = (
    "SELECT DISTINCT ON (aggregate_pk_ref) aggregate_pk_ref, aggregate_sequence_number, event_hash"
    " FROM core.event WHERE aggregate_pk_ref = ANY(%s)"
    " ORDER BY aggregate_pk_ref, aggregate_sequence_number DESC"
)


def read_heads(cur, aggregate_pks):
    """{aggregate_pk: (version, last event_hash)} for every key that has events."""
    if not hashing.HASHING:
        return None
    cur.execute(HEADS_SQL, [list(aggregate_pks)])
    return heads_from(cur)


def heads_from(cur):
    return {pk: (version, event_hash) for pk, version, event_hash in cur.fetchall()}


@dataclass
class EventWriter:
    """A command's events, checked and hashed as they are recorded and
    inserted together at the end of the command."""

    attribution: Attribution
    heads: dict | None
    rows: list = field(default_factory=list)
    _tips: dict = field(default_factory=dict)

    def record(self, aggregate_type, aggregate_pk, expected_version, event_type, prior, new, reason_pk,
               metadata=None, schema_version=1):
        """Check the version, hash, and queue one event. Returns the new version."""
        if aggregate_pk in self._tips:
            version, prior_hash = self._tips[aggregate_pk]
        elif self.heads is None:
            # The hashing-off diagnostic skips the head read, as wax.store does;
            # the unique constraint still catches a conflicting version.
            version = expected_version
            prior_hash = hashing.PLACEHOLDER_HASH if version else None
        else:
            version, prior_hash = self.heads.get(aggregate_pk, (0, None))
        if version != expected_version:
            raise ConcurrencyError(
                f"{aggregate_type} {aggregate_pk}: expected version {expected_version}, found {version}"
            )
        version += 1
        a = self.attribution
        event_hash = hashing.PLACEHOLDER_HASH if not hashing.HASHING else compute_event_hash(
            aggregate_type=aggregate_type,
            aggregate_pk_ref=aggregate_pk,
            event_type=event_type,
            schema_version=schema_version,
            prior_value=prior,
            new_value=new,
            occurred_at=a.occurred_at,
            actor_pk_ref=a.actor_pk,
            actor_type=a.actor_type,
            reason_code_fk=reason_pk,
            prior_event_hash=prior_hash,
        )
        self.rows.append((
            uuid.uuid4(), version, event_type, schema_version, aggregate_pk, aggregate_type, a.occurred_at,
            a.actor_pk, a.actor_type, _jsonb(prior), _jsonb(new), reason_pk, a.source,
            a.correlation_pk, a.correlation_type, _jsonb(metadata), prior_hash, event_hash,
        ))
        self._tips[aggregate_pk] = (version, event_hash)
        return version

    def send_insert(self, cur):
        """Send the insert of the queued events, in recorded order, without
        reading its result; in pipeline mode it travels with other writes."""
        placeholders = ", ".join(["(" + ", ".join(["%s"] * 18) + ")"] * len(self.rows))
        params = [value for row in self.rows for value in row]
        cur.execute(
            f"INSERT INTO core.event ({_INSERT_COLUMNS}) VALUES {placeholders} RETURNING global_sequence_number",
            params,
        )

    @staticmethod
    def position(cur):
        """The global position of the last event inserted, the command's
        read-your-writes token."""
        return max(row[0] for row in cur.fetchall())

    def flush(self, cur):
        if not self.rows:
            return 0
        try:
            self.send_insert(cur)
            return self.position(cur)
        except psycopg.errors.UniqueViolation as exc:
            raise ConcurrencyError(str(exc)) from exc


def current_position(cur):
    cur.execute("SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.seq_event_global_sequence")
    return cur.fetchone()[0]
