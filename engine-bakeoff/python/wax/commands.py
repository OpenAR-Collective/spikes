"""Command path (WAX-0002 CQRS Model).

A command is a named, immutable request. One handler per command type runs
inside one transaction, writes events and authoritative state together, and
returns nothing: the outcome is success or an exception.
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.utils import timezone

_in_command = ContextVar("wax_in_command", default=False)
_handlers = {}

MAX_ATTEMPTS = 3


class CommandRejected(Exception):
    """The command is invalid against current state. Nothing was written.

    code names the refusal for a client that shows its own wording in its
    own language; the message is the plain-English fallback.
    """

    def __init__(self, message, code="rejected"):
        super().__init__(message)
        self.code = code


class AlreadyApplied(Exception):
    """This exact command, under this key, took effect earlier. Nothing is
    written, and the caller is answered as if it had just succeeded: a
    request retried after a lost response must not be told it failed."""


def require_command():
    if not _in_command.get():
        raise RuntimeError(
            "Authoritative state may only be written by a command handler (WAX-0001)"
        )


def handles(command_type):
    def register(function):
        if command_type in _handlers:
            raise RuntimeError(f"{command_type.__name__} already has a handler")
        _handlers[command_type] = function
        return function

    return register


@dataclass
class CommandContext:
    actor_pk: uuid.UUID
    actor_type: str
    occurred_at: datetime
    source: str | None
    correlation_pk: uuid.UUID | None
    correlation_type: str | None
    position: int = 0

    def record(self, aggregate, event_type, *, prior, new, reason, metadata=None):
        """Write one event for an aggregate root and save its state row.

        The event insert and the state save share the handler's transaction,
        which is the atomicity invariant in WAX-0001.
        """
        from wax import store

        attribution = store.Attribution(
            actor_pk=self.actor_pk,
            actor_type=self.actor_type,
            occurred_at=self.occurred_at,
            reason_code_fk=_reason_code_pk(reason),
            source=self.source,
            correlation_pk=self.correlation_pk,
            correlation_type=self.correlation_type,
        )
        aggregate.aggregate_sequence_number, position = store.append(
            aggregate.aggregate_type,
            aggregate.pk,
            aggregate.aggregate_sequence_number,
            [store.NewEvent(event_type, prior, new, metadata)],
            attribution,
        )
        self.position = max(self.position, position)
        aggregate.save()


def _reason_code_pk(code):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT reference_value_pk FROM core.reference_value value "
            "JOIN core.reference_type type ON type.reference_type_pk = value.reference_type_fk "
            "WHERE type.code = 'reason_code' AND value.code = %s",
            [code],
        )
        row = cursor.fetchone()
    if row is None:
        raise CommandRejected(f"Unknown reason code: {code}", "unknown_reason_code")
    return row[0]


def dispatch(
    command,
    *,
    actor_pk,
    actor_type="user",
    source=None,
    correlation_pk=None,
    correlation_type=None,
):
    from wax import release, store
    from wax.store import ConcurrencyError

    # A release the schema has moved past must not write (SHARED-0010).
    release.assert_schema_supports_this_code(max_age_seconds=5)
    handler = _handlers[type(command)]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        context = CommandContext(
            actor_pk=uuid.UUID(str(actor_pk)),
            actor_type=actor_type,
            occurred_at=timezone.now(),
            source=source,
            correlation_pk=correlation_pk,
            correlation_type=correlation_type,
        )
        token = _in_command.set(True)
        try:
            with transaction.atomic():
                handler(command, context)
            # Not a result: a token. A caller that wants to read its own write
            # waits until the read models have passed this position.
            return context.position
        except AlreadyApplied:
            return store.current_position()
        except ConcurrencyError:
            if attempt == MAX_ATTEMPTS:
                raise
        finally:
            _in_command.reset(token)
