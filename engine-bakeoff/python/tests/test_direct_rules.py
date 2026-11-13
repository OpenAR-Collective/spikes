"""The python-direct rule runner and register poller against the Django ones.
Foundation-written.

The first twin of each pair receives its commands through the Django path,
and the Django runner and poller act on it until nothing is left. Then the
second twins receive the same commands, and direct/rules.py and
direct/register.py act on them, from the checkpoint the Django runner left.
Each pair must end with the same events, the same state, and the same register
entries and tasks, apart from keys and times. The commands are the ones that
trigger every rule: a placement schedules first contact, which fires and
raises a task; a dispute schedules a review, which fires and queues the
account; and a payoff marks the account paid in full.
"""

import json
import time
import uuid
from datetime import UTC, datetime
from uuid import UUID

import pytest
from django.db import connection

from bakeoff import rules as bakeoff_rules
from bakeoff.keys import REGISTER, RULE_RUNNER, derived_uuid
from hivear import commands
from tests.test_direct_twin import WHO, place, rename, state
from wax import register, store
from wax.commands import dispatch
from wax.rules import RuleRunner

RECEIVED_AT = datetime(2026, 10, 1, 6, tzinfo=UTC)


@pytest.fixture
def fast_days():
    from direct import rules as direct_rules

    bakeoff_rules.SETTINGS["seconds_per_day"] = direct_rules.SETTINGS["seconds_per_day"] = 0.001
    yield
    bakeoff_rules.SETTINGS["seconds_per_day"] = direct_rules.SETTINGS["seconds_per_day"] = 2.0


def send_commands(accounts, label):
    """The same commands for each side's accounts, in the same order: a
    partial payment, a dispute, and a payoff."""
    for i, pk in enumerate(accounts):
        place(pk, f"TWIN-{label}-{pk.hex[:8]}", opening={"principal": 1_000 * (i + 1), "fee": 200})
    payments = [uuid.uuid4(), uuid.uuid4()]
    dispatch(commands.PostPayment(payment_pk=payments[0], account_pks=[accounts[0]], amount_minor_units=300,
                                  currency_code="USD", received_at=RECEIVED_AT), **WHO)
    dispatch(commands.OpenDispute(account_pk=accounts[1], dispute_pk=uuid.uuid4()), **WHO)
    dispatch(commands.PostPayment(payment_pk=payments[1], account_pks=[accounts[2]], amount_minor_units=2_000_000,
                                  currency_code="USD", received_at=RECEIVED_AT), **WHO)
    return payments


def settle(drain, poll):
    """Run the rules and the register in the same fixed rounds on each side,
    so every account's events land in the same order."""
    for _ in range(2):
        drain()
        time.sleep(0.05)
        poll()
        drain()


def streams(accounts, payments):
    """Every event on the accounts as text, with each key named after its
    account and version, the keys derived from them named after those, and
    due times left out, since they follow the clock."""
    names, rows = {pk: f"PAYMENT{i}" for i, pk in enumerate(payments)}, []
    for i, pk in enumerate(accounts):
        names[pk] = f"ACCOUNT{i}"
        for event in store.load_stream(pk):
            names[event.event_pk] = f"EVENT{i}.{event.aggregate_sequence_number}"
            if event.event_type == "dispute.opened":
                names[UUID(event.metadata["dispute_pk"])] = f"DISPUTE{i}"
            new = dict(event.new_value or {})
            if "due_at" in new:
                new["due_at"] = "DUE"
            rows.append([event.aggregate_pk_ref, event.aggregate_sequence_number, event.event_type, event.actor_pk_ref,
                         event.actor_type, event.source, event.correlation_pk_ref, event.correlation_type,
                         event.reason_code_fk, event.prior_value, new, event.metadata,
                         event.prior_event_hash is not None])
    for key, name in list(names.items()):
        pending = derived_uuid(f"pending:{key}")
        names[pending] = f"PENDING-{name}"
        names[derived_uuid(f"task:{pending}")] = f"TASK-{name}"
    return rename(json.dumps(rows, sort_keys=True, default=str), names), names


@pytest.mark.django_db(transaction=True)
def test_the_direct_runner_and_poller_leave_what_the_django_ones_leave(buckets, fast_days):
    from direct import handlers  # noqa: F401  (registers the handlers)
    from direct import register as direct_register
    from direct import rules as direct_rules
    from direct.commands import Session
    from direct.db import connect

    runner = RuleRunner(actor_pk=RULE_RUNNER)
    runner.position()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s WHERE projection_name = %s",
            [store.current_position(), runner.name],
        )

    def django_fire(key):
        dispatch(commands.FirePendingEvent(pending_event_pk=key), actor_pk=REGISTER, actor_type="system", source="register")

    django_side = sorted(uuid.uuid4() for _ in range(3))
    django_payments = send_commands(django_side, "D")

    def drain_django():
        while runner.run_once():
            pass

    def poll_django():
        while register.poll_once(django_fire):
            pass

    settle(drain_django, poll_django)

    settings = connection.settings_dict
    session = Session(connect(dbname=settings["NAME"], user=settings["USER"], password=settings["PASSWORD"],
                              host=settings["HOST"], port=settings["PORT"]))
    try:
        direct_runner = direct_rules.RuleRunner(session, actor_pk=RULE_RUNNER)
        assert direct_runner.name == runner.name
        direct_fire = direct_register.firing(session, REGISTER)
        direct_side = sorted(uuid.uuid4() for _ in range(3))
        direct_payments = send_commands(direct_side, "R")

        def drain_direct():
            while direct_runner.run_once():
                pass

        def poll_direct():
            while direct_register.poll_once(session.conn, direct_fire):
                pass

        settle(drain_direct, poll_direct)
    finally:
        session.conn.close()

    django_text, django_names = streams(django_side, django_payments)
    direct_text, direct_names = streams(direct_side, direct_payments)
    assert direct_text == django_text
    assert state(direct_side, [], direct_names) == state(django_side, [], django_names)
    for pk in direct_side:
        assert store.verify_stream(pk) == []
    # Every rule fired: three first-contact tasks, one queued review, one payoff.
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM ar.task WHERE account_fk = ANY(%s)", [direct_side])
        assert cursor.fetchone()[0] == 3
        cursor.execute(
            "SELECT queue_code, status FROM ar.account WHERE account_pk = ANY(%s) ORDER BY account_pk", [direct_side]
        )
        assert cursor.fetchall() == [(None, "active"), ("dispute_review", "active"), (None, "paid_in_full")]
