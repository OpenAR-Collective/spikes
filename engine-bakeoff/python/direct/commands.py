"""Command dispatch for the python-direct side: wax/commands.py's dispatch on a
psycopg connection. Foundation-written.

The rules are the prototype's: one handler per command type, one transaction
per command, nothing returned but a position token, a command whose key took
effect earlier answered as success, and a version conflict retried. The
schema interlock of wax/release.py is checked at most every five seconds, as
dispatch does, and the reason-code registry is read once per connection,
since it is reference data.
"""

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from direct import store

MAX_ATTEMPTS = 3
_handlers = {}

# wax/release.py's identity, without importing Django: the same environment
# variables and the same rule that a schema supports the previous minor.
RELEASE = os.environ.get("HIVEAR_RELEASE", "0.2.0")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.]+)?$")
COMPONENTS = ("wax", "hivear")


class CommandRejected(Exception):
    def __init__(self, message, code="rejected"):
        super().__init__(message)
        self.code = code


class AlreadyApplied(Exception):
    """This exact command, under this key, took effect earlier."""


class IncompatibleSchema(Exception):
    pass


def handles(command_type):
    def register(function):
        if command_type in _handlers:
            raise RuntimeError(f"{command_type.__name__} already has a handler")
        _handlers[command_type] = function
        return function

    return register


def _parse(version):
    match = _VERSION.match(str(version))
    if not match:
        raise ValueError(f"{version!r} is not a MAJOR.MINOR.PATCH release")
    return tuple(int(part) for part in match.groups())


@dataclass
class Session:
    """What a connection keeps between commands: the interlock's last answer
    and the reason-code registry."""

    conn: object
    checked_at: float = -1e9
    problems: list = field(default_factory=list)
    reasons: dict | None = None

    def assert_schema_supports_this_code(self, max_age_seconds=5):
        now = time.monotonic()
        if now - self.checked_at >= max_age_seconds:
            problems = []
            with self.conn.cursor() as cur:
                cur.execute("SELECT component, schema_release, minimum_code_release FROM core.component_release")
                for name, schema, minimum in cur.fetchall():
                    if name not in COMPONENTS:
                        problems.append(f"{name} is installed in this database and missing from this image")
                    elif _parse(RELEASE) < _parse(minimum):
                        problems.append(f"{name} here is {RELEASE}; the schema at {schema} supports code from {minimum}")
            self.checked_at, self.problems = now, problems
        if self.problems:
            raise IncompatibleSchema("; ".join(self.problems))

    def reason(self, cur, code):
        if self.reasons is None:
            cur.execute(
                "SELECT value.code, value.reference_value_pk FROM core.reference_value value"
                " JOIN core.reference_type type ON type.reference_type_pk = value.reference_type_fk"
                " WHERE type.code = 'reason_code'"
            )
            self.reasons = dict(cur.fetchall())
        if code not in self.reasons:
            raise CommandRejected(f"Unknown reason code: {code}", "unknown_reason_code")
        return self.reasons[code]


@dataclass
class Context:
    session: Session
    cur: object
    attribution: store.Attribution
    events: store.EventWriter = None
    # Set by a handler that inserted its own events, in pipeline mode.
    position: int | None = None

    @property
    def occurred_at(self):
        return self.attribution.occurred_at

    def reason(self, code):
        return self.session.reason(self.cur, code)

    def read_heads(self, aggregate_pks):
        self.events = store.EventWriter(self.attribution, store.read_heads(self.cur, aggregate_pks))
        return self.events


def dispatch(session, command, *, actor_pk, actor_type="user", source=None, correlation_pk=None,
             correlation_type=None):
    """Run one command in one transaction. Returns the position token."""
    session.assert_schema_supports_this_code(max_age_seconds=5)
    handler = _handlers[type(command)]
    conn = session.conn
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attribution = store.Attribution(
            actor_pk=uuid.UUID(str(actor_pk)), actor_type=actor_type, occurred_at=datetime.now(UTC),
            source=source, correlation_pk=correlation_pk, correlation_type=correlation_type,
        )
        try:
            with conn.transaction(), conn.cursor() as cur:
                context = Context(session, cur, attribution)
                handler(command, context)
                if context.position is not None:
                    return context.position
                return context.events.flush(cur) if context.events else 0
        except AlreadyApplied:
            with conn.cursor() as cur:
                return store.current_position(cur)
        except store.ConcurrencyError:
            if attempt == MAX_ATTEMPTS:
                raise
