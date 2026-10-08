import uuid
from datetime import UTC, datetime

import pytest
from django.db import DatabaseError, connection, transaction

from hivear.models import Account, AccountBalanceBucket
from tests.conftest import ACTOR
from wax import store

pytestmark = pytest.mark.django_db


def test_placing_an_account_writes_event_and_state_together(place):
    account_pk = place()
    (event,) = store.load_stream(account_pk)
    assert event.event_type == "account.placed"
    assert event.aggregate_sequence_number == 1
    assert event.prior_value is None and event.prior_event_hash is None
    assert event.new_value["balance_buckets"] == {"principal": 100_000, "interest": 5_000}
    assert Account.objects.get(pk=account_pk).aggregate_sequence_number == 1


def test_state_cannot_be_written_outside_a_command(place):
    account = Account.objects.get(pk=place())
    account.status = "closed"
    with pytest.raises(RuntimeError, match="command handler"):
        account.save()
    with pytest.raises(RuntimeError, match="command handler"):
        AccountBalanceBucket.objects.filter(account=account).update(balance_minor_units=0)


@pytest.mark.parametrize("statement", ["UPDATE core.event SET source = 'x'", "DELETE FROM core.event"])
def test_events_are_immutable_in_the_database(place, statement):
    place()
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(statement)


def test_append_rejects_a_stale_expected_version(place):
    account_pk = place()
    attribution = store.Attribution(actor_pk=ACTOR, actor_type="system", occurred_at=datetime.now(UTC))
    event = store.NewEvent("account.noted", {"status": "active"}, {"status": "active"})
    with pytest.raises(store.ConcurrencyError), transaction.atomic():
        store.append("account", account_pk, 0, [event], attribution)


def test_hash_chain_verifies_and_detects_a_forged_link(place, run):
    from hivear import commands

    account_pk = place()
    run(commands.PostPayment(account_pks=[account_pk], amount_minor_units=2_500, currency_code="USD"))
    assert store.verify_stream(account_pk) == []

    # Forge a third event whose prior hash points nowhere.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version,"
            " aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type,"
            " prior_event_hash, event_hash) VALUES (%s, 3, 'account.forged', 1, %s, 'account',"
            " now(), %s, 'system', 'not-the-real-hash', 'made-up')",
            [uuid.uuid4(), account_pk, ACTOR],
        )
    problems = store.verify_stream(account_pk)
    assert any("prior_event_hash" in problem for problem in problems)
    assert any("event_hash" in problem for problem in problems)
