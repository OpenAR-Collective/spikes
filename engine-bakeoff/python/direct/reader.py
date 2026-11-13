"""The projection reader for the python-direct side. Foundation-written.

It keeps Andy Troiano's reader logic from wax/projections.py: the high-water
mark sampled from the sequence and a fresh transaction id and advanced once no
older transaction is running; one projection at a time, its checkpoint row
claimed with FOR UPDATE SKIP LOCKED so several workers can share it; a batch
applied in the same transaction as the checkpoint advance, so each event is
handled exactly once; a batch that fails replayed one event at a time; and a
projection that meets an event type it neither handles nor ignores stopped at
that event, with the reason recorded, while the others continue.

Two things differ. A projection reads only the aggregate types it covers, as
the C# port of his reader does, instead of every event and passing over the
rest. Events are read as plain rows, and the three read models are written
with SQL upserts in place of Django's object layer. The read models, their
tables, and the projection names are the Python side's, so the harness's
progress view and gates read them unchanged. The definition digest that
wax/projections.py compares is computed from the Django handlers' source, so
this reader leaves the stored digest as it is and does not compare it.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from direct.db import as_json

logger = logging.getLogger("direct.reader")
EVENT_COLUMNS = "global_sequence_number, event_type, aggregate_pk_ref, aggregate_type, new_value, metadata"


@dataclass(frozen=True)
class Event:
    global_sequence_number: int
    event_type: str
    aggregate_pk_ref: object
    aggregate_type: str
    new_value: dict | None
    metadata: dict | None


class UnknownEventType(Exception):
    pass


class HighWaterMark:
    """wax.projections.HighWaterMark on a psycopg connection."""

    def __init__(self, max_pending=64):
        self._max_pending = max_pending
        self._forget(None)

    def _forget(self, identity):
        self._identity = identity
        self._pending = deque()
        self._last_sampled = 0
        self.value = 0

    def advance(self, conn) -> int:
        position_sql = (
            "SELECT CASE WHEN is_called THEN last_value ELSE 0 END, "
            "pg_postmaster_start_time()::text || '/' || current_database() FROM core.seq_event_global_sequence"
        )
        with conn.cursor() as cur:
            cur.execute(position_sql)
            position, identity = cur.fetchone()
            if identity != self._identity or position < self._last_sampled:
                self._forget(identity)
            if position > self._last_sampled:
                with conn.transaction():
                    cur.execute(position_sql)
                    position = cur.fetchone()[0]
                    cur.execute("SELECT pg_current_xact_id()::text::bigint")
                    boundary = cur.fetchone()[0]
                if len(self._pending) == self._max_pending:
                    self._pending.pop()
                self._pending.append((boundary, position))
                self._last_sampled = position
            if self._pending:
                cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::bigint")
                oldest_running = cur.fetchone()[0]
                while self._pending and self._pending[0][0] <= oldest_running:
                    self.value = max(self.value, self._pending.popleft()[1])
        return self.value


class Projection:
    def __init__(self, name, aggregate_types, ignores=()):
        self.name = name
        self.aggregate_types = list(aggregate_types)
        self.ignores = frozenset(ignores)
        self.handlers = {}

    def on(self, *event_types):
        def register(function):
            for event_type in event_types:
                self.handlers[event_type] = function
            return function

        return register

    def handler_for(self, event):
        handler = self.handlers.get(event.event_type)
        if handler is None and event.event_type not in self.ignores:
            raise UnknownEventType(event.event_type)
        return handler


# The three read models of hivear/projections.py, by the same names.

account_summary = Projection(
    "ar.account_summary_read_model.v1",
    ["account"],
    ignores=["pending_event.scheduled", "account.queue_assigned", "task.created",
             "account.dispute_review_due", "account.first_contact_due"],
)


@account_summary.on("account.placed")
def _placed(cur, event):
    new = event.new_value
    cur.execute(
        "INSERT INTO ar.account_summary_read_model (account_summary_read_model_pk, client_account_number, status,"
        " is_disputed, currency_code, debt_type, contract_type, list_date, balance_buckets, balance_minor_units,"
        " last_global_sequence_number) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s)"
        " ON CONFLICT (account_summary_read_model_pk) DO UPDATE SET client_account_number = EXCLUDED.client_account_number,"
        " status = EXCLUDED.status, is_disputed = EXCLUDED.is_disputed, currency_code = EXCLUDED.currency_code,"
        " debt_type = EXCLUDED.debt_type, contract_type = EXCLUDED.contract_type, list_date = EXCLUDED.list_date,"
        " balance_buckets = EXCLUDED.balance_buckets, balance_minor_units = EXCLUDED.balance_minor_units,"
        " last_global_sequence_number = EXCLUDED.last_global_sequence_number",
        [event.aggregate_pk_ref, new["client_account_number"], new["status"], new["is_disputed"],
         new["currency_code"], new["debt_type"], new["contract_type"], new["list_date"],
         Jsonb(new["balance_buckets"]), sum(new["balance_buckets"].values()), event.global_sequence_number],
    )


def _update_summary(cur, event, assignments, params):
    cur.execute(
        f"UPDATE ar.account_summary_read_model SET {assignments}, last_global_sequence_number = %s"
        " WHERE account_summary_read_model_pk = %s",
        [*params, event.global_sequence_number, event.aggregate_pk_ref],
    )
    if cur.rowcount != 1:
        raise LookupError(f"no account summary for {event.aggregate_pk_ref}")


@account_summary.on("dispute.opened")
def _disputed(cur, event):
    _update_summary(cur, event, "is_disputed = true", [])


@account_summary.on("account.status_changed")
def _status_changed(cur, event):
    _update_summary(cur, event, "status = %s", [event.new_value["status"]])


@account_summary.on("account.payment_applied", "account.payment_reversed", "account.interest_accrued")
def _balance_moved(cur, event):
    moved = Jsonb(event.new_value["balance_buckets"])
    assignments = ("balance_buckets = balance_buckets || %s::jsonb, balance_minor_units ="
                   " (SELECT sum(value::bigint) FROM jsonb_each_text(balance_buckets || %s::jsonb))")
    params = [moved, moved]
    if event.event_type == "account.payment_applied":
        assignments += ", last_payment_at = %s, last_payment_minor_units = %s"
        params += [datetime.fromisoformat(event.metadata["received_at"]),
                   sum(event.metadata["bucket_amounts"].values())]
    _update_summary(cur, event, assignments, params)


account_payment = Projection("ar.account_payment_read_model.v2", ["payment"])


@account_payment.on("payment.posted")
def _payment_lines(cur, event):
    import uuid

    new = event.new_value
    distribution = new["distribution"]
    named = (new.get("request") or {}).get("account_pks") or list(distribution)
    for account_pk in named:
        buckets = distribution.get(account_pk, {})
        applied = sum(buckets.values())
        cur.execute(
            "INSERT INTO ar.account_payment_read_model_v2 (account_payment_line_pk, payment_pk, account_pk,"
            " applied_minor_units, bucket_amounts, payment_amount_minor_units, payment_unapplied_minor_units,"
            " currency_code, received_at, status, last_global_sequence_number)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (payment_pk, account_pk) DO UPDATE SET applied_minor_units = EXCLUDED.applied_minor_units,"
            " bucket_amounts = EXCLUDED.bucket_amounts, payment_amount_minor_units = EXCLUDED.payment_amount_minor_units,"
            " payment_unapplied_minor_units = EXCLUDED.payment_unapplied_minor_units,"
            " currency_code = EXCLUDED.currency_code, received_at = EXCLUDED.received_at, status = EXCLUDED.status,"
            " last_global_sequence_number = EXCLUDED.last_global_sequence_number",
            [uuid.uuid4(), event.aggregate_pk_ref, account_pk, applied, Jsonb(buckets), new["amount_minor_units"],
             new["unapplied_minor_units"], new["currency_code"], datetime.fromisoformat(new["received_at"]),
             "applied" if applied else "held", event.global_sequence_number],
        )


@account_payment.on("payment.reversed")
def _payment_lines_reversed(cur, event):
    cur.execute(
        "UPDATE ar.account_payment_read_model_v2 SET status = 'reversed', last_global_sequence_number = %s"
        " WHERE payment_pk = %s",
        [event.global_sequence_number, event.aggregate_pk_ref],
    )


payment = Projection("ar.payment_read_model.v1", ["payment"])


@payment.on("payment.posted")
def _payment_posted(cur, event):
    new = event.new_value
    cur.execute(
        "INSERT INTO ar.payment_read_model (payment_read_model_pk, amount_minor_units, currency_code, status,"
        " received_at, distribution, unapplied_minor_units, last_global_sequence_number)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (payment_read_model_pk) DO UPDATE SET amount_minor_units = EXCLUDED.amount_minor_units,"
        " currency_code = EXCLUDED.currency_code, status = EXCLUDED.status, received_at = EXCLUDED.received_at,"
        " distribution = EXCLUDED.distribution, unapplied_minor_units = EXCLUDED.unapplied_minor_units,"
        " last_global_sequence_number = EXCLUDED.last_global_sequence_number",
        [event.aggregate_pk_ref, new["amount_minor_units"], new["currency_code"], new["status"],
         datetime.fromisoformat(new["received_at"]), Jsonb(new["distribution"]), new["unapplied_minor_units"],
         event.global_sequence_number],
    )


@payment.on("payment.reversed")
def _payment_was_reversed(cur, event):
    cur.execute(
        "UPDATE ar.payment_read_model SET status = %s, last_global_sequence_number = %s WHERE payment_read_model_pk = %s",
        [event.new_value["status"], event.global_sequence_number, event.aggregate_pk_ref],
    )


PROJECTIONS = [account_summary, account_payment, payment]


class Reader:
    def __init__(self, conn, projections=PROJECTIONS, batch_size=500):
        self.conn = conn
        self.projections = projections
        self.batch_size = batch_size
        self.mark = HighWaterMark()

    def _handle(self, cur, item, event):
        handler = item.handler_for(event)
        if handler:
            handler(cur, event)
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def _apply(self, cur, item, events):
        try:
            with self.conn.transaction():
                for event in events:
                    self._handle(cur, item, event)
            return events[-1].global_sequence_number, None
        except Exception:
            logger.warning("%s: batch failed, replaying one event at a time", item.name)
        applied = None
        for event in events:
            try:
                with self.conn.transaction():
                    self._handle(cur, item, event)
                applied = event.global_sequence_number
            except UnknownEventType as exc:
                logger.error("%s does not know event type %s; stopped at %s", item.name, exc, event.global_sequence_number)
                return applied, ("unknown_event_type", event.global_sequence_number, str(exc))
            except Exception as exc:
                logger.exception("%s stopped at global_sequence_number %s", item.name, event.global_sequence_number)
                return applied, ("handler_error", event.global_sequence_number, f"{type(exc).__name__}: {exc}")
        return applied, None

    def _advance(self, item, safe) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT last_global_sequence_number, failed_at FROM core.projection_checkpoint WHERE projection_name = %s",
                [item.name],
            )
            row = cur.fetchone()
            if row is None or row[1] is not None or row[0] >= safe:
                return 0
            with self.conn.transaction():
                cur.execute(
                    "SELECT last_global_sequence_number FROM core.projection_checkpoint"
                    " WHERE projection_name = %s AND failed_at IS NULL FOR UPDATE SKIP LOCKED",
                    [item.name],
                )
                locked = cur.fetchone()
                if locked is None:
                    return 0
                cur.execute(
                    f"SELECT {EVENT_COLUMNS} FROM core.event WHERE global_sequence_number > %s"
                    " AND global_sequence_number <= %s AND aggregate_type = ANY(%s)"
                    " ORDER BY global_sequence_number LIMIT %s",
                    [locked[0], safe, item.aggregate_types, self.batch_size],
                )
                events = [
                    Event(seq, event_type, pk, aggregate_type, as_json(new_value), as_json(metadata))
                    for seq, event_type, pk, aggregate_type, new_value, metadata in cur.fetchall()
                ]
                applied, failure = self._apply(cur, item, events) if events else (None, None)
                if failure is None and len(events) < self.batch_size:
                    position = safe
                else:
                    position = applied if applied is not None else locked[0]
                cur.execute(
                    "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, updated_at = now()"
                    " WHERE projection_name = %s",
                    [position, item.name],
                )
                if failure:
                    kind, sequence, detail = failure
                    cur.execute(
                        "UPDATE core.projection_checkpoint SET failed_at = now(), failure_kind = %s,"
                        " failed_global_sequence_number = %s, failure_detail = %s, updated_at = now()"
                        " WHERE projection_name = %s",
                        [kind, sequence, str(detail).replace("\x00", "")[:2000], item.name],
                    )
            return sum(1 for event in events if applied is not None and event.global_sequence_number <= applied)

    def run_once(self) -> int:
        safe = self.mark.advance(self.conn)
        handled = 0
        for item in self.projections:
            try:
                handled += self._advance(item, safe)
            except Exception:
                logger.exception("%s: pass failed", item.name)
        return handled


def run_forever(poll_seconds=0.25):
    """The projection worker of the python-direct engine."""
    from direct.commands import IncompatibleSchema, Session
    from direct.db import connect

    session = None
    reader = None
    while True:
        try:
            if session is None:
                session = Session(connect())
                reader = Reader(session.conn)
            session.assert_schema_supports_this_code(max_age_seconds=5)
            handled = reader.run_once()
        except IncompatibleSchema as exc:
            logger.error("Not processing: %s", exc)
            handled = 0
            time.sleep(5)
        except Exception:
            logger.exception("Projection pass failed; reconnecting")
            try:
                session.conn.close()
            except Exception:
                pass
            session = reader = None
            handled = 0
            time.sleep(2)
        if handled == 0:
            time.sleep(poll_seconds)
