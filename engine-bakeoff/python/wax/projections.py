"""Asynchronous read model maintenance: the subscribe operation in WAX-0001.

Events are delivered in global_sequence_number order, which is also each
aggregate's own order, and never past the high-water mark: the highest
sequence number below which no event can still appear. A batch of events, the
read model writes they cause, and the checkpoint advance commit in one
transaction, so a handler sees each event exactly once.
"""

import ast
import hashlib
import inspect
import json
import logging
import textwrap
from collections import deque

from django.db import connection, transaction

from wax import release
from wax.models import Event

logger = logging.getLogger("wax.projections")
_registry = {}


class UnknownEventType(Exception):
    pass


class Projection:
    """One version of one read model.

    aggregate_types says which aggregates the read model covers. An event on
    a covered aggregate must have a handler or be listed in ignores; any
    other type stops the projection, because skipping an event that changed
    state would leave the read model wrong without a sign (SHARED-0010).
    Changing a handler or the table's shape means registering a new version,
    which builds beside the old one from the start of the stream.
    """

    def __init__(self, base_name, version, aggregate_types, ignores=()):
        if not aggregate_types:
            raise ValueError("a projection must say which aggregate types it covers")
        self.name = f"{base_name}.v{version}"
        self.aggregate_types = frozenset(aggregate_types)
        self.ignores = frozenset(ignores)
        self.handlers = {}

    def on(self, *event_types):
        def register(function):
            for event_type in event_types:
                self.handlers[event_type] = function
            return function

        return register

    def handler_for(self, event):
        if event.aggregate_type not in self.aggregate_types:
            return None
        handler = self.handlers.get(event.event_type)
        if handler is None and event.event_type not in self.ignores:
            raise UnknownEventType(event.event_type)
        return handler

    def digest(self) -> str:
        """Identifies this version's definition. Two releases that share a
        version name must agree on it, or neither may touch the other's rows.
        Comments and formatting do not change it; logic does."""
        def source(function):
            try:
                return ast.dump(ast.parse(textwrap.dedent(inspect.getsource(function))))
            except (OSError, TypeError, SyntaxError):
                return getattr(function, "__qualname__", repr(function))

        definition = {
            "aggregate_types": sorted(self.aggregate_types),
            "ignores": sorted(self.ignores),
            "handlers": {name: source(function) for name, function in sorted(self.handlers.items())},
        }
        return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def projection(base_name, version=1, *, aggregate_types, ignores=()) -> Projection:
    item = Projection(base_name, version, aggregate_types, ignores)
    if item.name in _registry:
        raise RuntimeError(f"Projection {item.name} is already registered")
    _registry[item.name] = item
    return item


def _row(sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


# The sequence position, and something that changes whenever the database
# underneath this process is not the one it was a moment ago.
_POSITION = (
    "SELECT CASE WHEN is_called THEN last_value ELSE 0 END, "
    "pg_postmaster_start_time()::text || '/' || current_database() "
    "FROM core.seq_event_global_sequence"
)


def sequence_position() -> int:
    return _row(_POSITION)[0]


class HighWaterMark:
    """The highest global_sequence_number that is safe to read up to.

    Sequence values are handed out at insert, not at commit, so a row with a
    lower number can become visible after a row with a higher one. Reading
    past it would skip it for good.

    A sample pairs the sequence position L with a transaction id N taken just
    after it. core.event's insert trigger forces a transaction id before it
    calls nextval(), so every event numbered L or lower belongs to a
    transaction older than N. Once the oldest transaction still running is N
    or newer, all of those have finished, and everything up to L is settled.

    The mark is this process's memory and nothing more. It is thrown away
    whenever the database restarts, is swapped, or shows a sequence position
    below one already seen, which is what a restore or a failover looks like.
    """

    def __init__(self, max_pending=64):
        self._max_pending = max_pending
        self._forget(None)

    def _forget(self, identity):
        self._identity = identity
        self._pending = deque()
        self._last_sampled = 0
        self.value = 0

    def advance(self) -> int:
        if connection.in_atomic_block:
            raise RuntimeError("the projection reader must not run inside another transaction")
        position, identity = _row(_POSITION)
        if identity != self._identity or position < self._last_sampled:
            self._forget(identity)
        if position > self._last_sampled:
            with transaction.atomic():
                position = _row(_POSITION)[0]
                boundary = _row("SELECT pg_current_xact_id()::text::bigint")[0]
            if len(self._pending) == self._max_pending:
                self._pending.pop()
            self._pending.append((boundary, position))
            self._last_sampled = position
        if self._pending:
            oldest_running = _row("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")[0]
            while self._pending and self._pending[0][0] <= oldest_running:
                self.value = max(self.value, self._pending.popleft()[1])
        return self.value


_high_water_mark = HighWaterMark()


def reset():
    """Forget the mark. Called after any error or reconnect."""
    global _high_water_mark
    _high_water_mark = HighWaterMark()


def register_checkpoints():
    with connection.cursor() as cursor:
        for item in _registry.values():
            cursor.execute(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest) "
                "VALUES (%s, %s) ON CONFLICT (projection_name) DO NOTHING",
                [item.name, item.digest()],
            )


def accept_definition(name):
    """Adopt this release's definition for a version. Only for a version no
    release has shipped yet: afterward, a changed definition is a new version."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET definition_digest = %s, updated_at = now() "
            "WHERE projection_name = %s",
            [_registry[name].digest(), name],
        )


def status() -> list[dict]:
    """Position and health of every projection this code owns."""
    position = sequence_position()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT projection_name, last_global_sequence_number, definition_digest, failed_at, "
            "failure_kind, failed_global_sequence_number, failure_detail "
            "FROM core.projection_checkpoint WHERE projection_name = ANY(%s) ORDER BY projection_name",
            [list(_registry)],
        )
        rows = cursor.fetchall()
    return [
        {
            "projection": name,
            "position": last,
            "sequence_position": position,
            "definition_matches": digest == _registry[name].digest(),
            "failed_at": failed_at,
            "failure_kind": kind,
            "failed_global_sequence_number": failed_sequence,
            "failure_detail": detail,
        }
        for name, last, digest, failed_at, kind, failed_sequence, detail in rows
    ]


def retry(name):
    """Clear a recorded failure so the projection resumes at the same event."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET failed_at = NULL, failure_kind = NULL, "
            "failed_global_sequence_number = NULL, failure_detail = NULL, updated_at = now() "
            "WHERE projection_name = %s",
            [name],
        )


def beat():
    """This color's worker is alive (SHARED-0010)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.worker_heartbeat (color, release) VALUES (%s, %s) "
            "ON CONFLICT (color) DO UPDATE SET release = EXCLUDED.release, last_seen_at = now()",
            [release.COLOR, release.RELEASE],
        )


def heartbeat_age_seconds(color):
    row = _row(
        "SELECT extract(epoch FROM now() - last_seen_at) FROM core.worker_heartbeat WHERE color = %s",
        [color],
    )
    return None if row is None else float(row[0])


def _handle(item, event):
    handler = item.handler_for(event)
    if handler:
        handler(event)
    # A deferred constraint would otherwise fail at commit, attributed to nothing.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def _apply(item, events):
    """Apply events in order. Returns (last applied sequence, failure), where
    failure is (kind, sequence, detail) for the event the projection stopped at."""
    try:
        with transaction.atomic():
            for event in events:
                _handle(item, event)
        return events[-1].global_sequence_number, None
    except Exception:
        logger.warning("%s: batch failed, replaying one event at a time", item.name)

    applied = None
    for event in events:
        try:
            with transaction.atomic():
                _handle(item, event)
            applied = event.global_sequence_number
        except UnknownEventType as exc:
            logger.error("%s does not know event type %s; stopped at %s", item.name, exc, event.global_sequence_number)
            return applied, ("unknown_event_type", event.global_sequence_number, str(exc))
        except Exception as exc:
            logger.exception("%s stopped at global_sequence_number %s", item.name, event.global_sequence_number)
            return applied, ("handler_error", event.global_sequence_number, f"{type(exc).__name__}: {exc}")
    return applied, None


def _record_failure(name, kind, sequence, detail):
    detail = str(detail).replace("\x00", "")[:2000]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET failed_at = now(), failure_kind = %s, "
            "failed_global_sequence_number = %s, failure_detail = %s, updated_at = now() "
            "WHERE projection_name = %s",
            [kind, sequence, detail, name],
        )


def _advance(item, safe, batch_size) -> int:
    row = _row(
        "SELECT last_global_sequence_number, definition_digest, failed_at, failure_kind, "
        "failed_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s",
        [item.name],
    )
    if row is None:
        register_checkpoints()
        row = (0, item.digest(), None, None, None)
    last, digest, failed_at, failure_kind, failed_sequence = row
    if digest != item.digest():
        # Another release defines this version differently. Leave it alone.
        return 0
    if failed_at is not None:
        # A newer release may know a type an older one stopped on.
        if failure_kind != "unknown_event_type":
            return 0
        blocker = Event.objects.filter(global_sequence_number=failed_sequence).first()
        try:
            item.handler_for(blocker)
        except UnknownEventType:
            return 0
        retry(item.name)
    if last >= safe:
        return 0

    with transaction.atomic():
        # SKIP LOCKED lets several workers, of either color, share a version.
        locked = _row(
            "SELECT last_global_sequence_number FROM core.projection_checkpoint "
            "WHERE projection_name = %s AND failed_at IS NULL FOR UPDATE SKIP LOCKED",
            [item.name],
        )
        if locked is None:
            return 0
        events = list(
            Event.objects.filter(
                global_sequence_number__gt=locked[0], global_sequence_number__lte=safe
            ).order_by("global_sequence_number")[:batch_size]
        )
        applied, failure = _apply(item, events) if events else (None, None)
        if failure is None and len(events) < batch_size:
            # Nothing at or below the mark can still appear, so the checkpoint
            # may pass sequence numbers that no event will ever carry.
            position = safe
        else:
            position = applied if applied is not None else locked[0]
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, "
                "updated_at = now() WHERE projection_name = %s",
                [position, item.name],
            )
        if failure:
            _record_failure(item.name, *failure)
    return sum(1 for event in events if applied is not None and event.global_sequence_number <= applied)


def run_once(batch_size=500) -> int:
    """Advance every healthy projection by at most one batch.

    A projection that cannot handle an event stops at that event and records
    why. It never skips, and it does not hold the others back.
    """
    safe = _high_water_mark.advance()
    handled = 0
    for item in list(_registry.values()):
        try:
            handled += _advance(item, safe, batch_size)
        except Exception as exc:
            # A failure outside any one event, such as at commit. The pass for
            # this projection rolled back; record it and move on to the next.
            logger.exception("%s: pass failed", item.name)
            try:
                _record_failure(item.name, "handler_error", None, f"{type(exc).__name__}: {exc}")
            except Exception:
                logger.exception("%s: could not record the failure", item.name)
    return handled
