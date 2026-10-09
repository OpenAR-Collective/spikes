"""The bulk workloads (WORKLOADS.md, "Bulk workloads"). Foundation-written.

The bulk path must store exactly what the single-command path stores. Each
test sends one of a pair of twin aggregates through each path and requires
the same events and the same state, apart from the keys that tell the twins
apart and the values the store assigns.
"""

import json
import uuid
from datetime import UTC, date, datetime

import pytest
from django.db import connection

from bakeoff import bulk, drivers
from bakeoff.keys import BATCH
from hivear import commands
from hivear.models import Account, Payment
from tests.conftest import balances
from wax import store
from wax.commands import CommandRejected, dispatch
from wax.store import ConcurrencyError

RECEIVED_AT = datetime(2026, 10, 1, 6, tzinfo=UTC)
ATTRIBUTION = {"actor_pk": BATCH, "actor_type": "batch", "source": "batch"}


def _placement(account_pk, number):
    return commands.PlaceAccount(
        account_pk=account_pk,
        client_account_number=number,
        debt_type="personal_loan",
        contract_type="third_party_contingency",
        currency_code="USD",
        list_date=date(2024, 3, 1),
        itemization_date=date(2024, 1, 31),
        interest_accrued_through=date(2026, 9, 30),
        opening_balances={"principal": 1_000, "interest": 700, "fee": 1_500},
    )


def _payment(account_pk, payment_pk, amount=2_500):
    return commands.PostPayment(
        payment_pk=payment_pk, account_pks=[account_pk], amount_minor_units=amount,
        currency_code="USD", received_at=RECEIVED_AT,
    )


def _events(*aggregate_pks, names):
    """Every event on these aggregates, as comparable text: the twin's keys
    replaced by names, and only the values the store assigns left out."""
    rows = []
    for aggregate_pk in aggregate_pks:
        for event in store.load_stream(aggregate_pk):
            row = {
                field: getattr(event, field)
                for field in (
                    "aggregate_sequence_number", "event_type", "schema_version", "aggregate_pk_ref", "aggregate_type",
                    "actor_pk_ref", "actor_type", "prior_value", "new_value", "reason_code_fk", "source",
                    "correlation_pk_ref", "correlation_type", "metadata",
                )
            }
            row["prior_event_hash"] = event.prior_event_hash is not None
            rows.append(row)
    text = json.dumps(rows, sort_keys=True, default=str)
    for key, name in names.items():
        text = text.replace(str(key), name)
    return text


@pytest.mark.django_db
def test_bulk_payment_stores_what_post_payment_stores(buckets, new_keys):
    single_account, bulk_account = new_keys()
    single_payment, bulk_payment = new_keys()
    dispatch(_placement(single_account, "TWIN-1"), **ATTRIBUTION)
    dispatch(_placement(bulk_account, "TWIN-2"), **ATTRIBUTION)

    dispatch(_payment(single_account, single_payment), **ATTRIBUTION)
    outcome = bulk.post_payments([_payment(bulk_account, bulk_payment)], **ATTRIBUTION)

    assert (outcome.succeeded, outcome.rejected, outcome.skipped) == (1, 0, 0)
    single = _events(single_account, single_payment, names={single_account: "ACCOUNT", single_payment: "PAYMENT", "TWIN-1": "TWIN"})
    twin = _events(bulk_account, bulk_payment, names={bulk_account: "ACCOUNT", bulk_payment: "PAYMENT", "TWIN-2": "TWIN"})
    assert twin == single
    assert balances(bulk_account) == balances(single_account) == {"principal": 700, "interest": 0, "fee": 0}
    assert Account.objects.get(pk=bulk_account).aggregate_sequence_number == 2
    paid, twin_paid = Payment.objects.get(pk=single_payment), Payment.objects.get(pk=bulk_payment)
    assert (twin_paid.aggregate_sequence_number, twin_paid.amount_minor_units, twin_paid.status, twin_paid.received_at,
            twin_paid.unapplied_minor_units) == (
            paid.aggregate_sequence_number, paid.amount_minor_units, paid.status, paid.received_at,
            paid.unapplied_minor_units)
    assert twin_paid.distribution == {str(bulk_account): paid.distribution[str(single_account)]}
    assert store.verify_stream(bulk_account) == [] and store.verify_stream(bulk_payment) == []


@pytest.mark.django_db
def test_bulk_placement_stores_what_place_account_stores(buckets, new_keys):
    single_account, bulk_account = new_keys()
    dispatch(_placement(single_account, "TWIN-1"), **ATTRIBUTION)
    outcome = bulk.place_accounts([_placement(bulk_account, "TWIN-2")], **ATTRIBUTION)

    assert (outcome.succeeded, outcome.rejected, outcome.skipped) == (1, 0, 0)
    assert _events(bulk_account, names={bulk_account: "ACCOUNT", "TWIN-2": "TWIN"}) == _events(
        single_account, names={single_account: "ACCOUNT", "TWIN-1": "TWIN"}
    )
    columns = ("aggregate_sequence_number", "debt_type", "contract_type", "currency_code", "status", "is_disputed",
               "list_date", "itemization_date", "interest_accrued_through", "queue_code")
    single, twin = Account.objects.get(pk=single_account), Account.objects.get(pk=bulk_account)
    assert [getattr(twin, c) for c in columns] == [getattr(single, c) for c in columns]
    assert balances(bulk_account) == balances(single_account)
    assert store.verify_stream(bulk_account) == []


@pytest.mark.django_db
def test_bulk_payment_skips_paid_keys_and_refuses_what_post_payment_refuses(buckets, new_keys):
    paid, closed, fresh = new_keys(3)
    keys = dict(zip((paid, closed, fresh), new_keys(3)))
    for i, account in enumerate((paid, closed, fresh)):
        dispatch(_placement(account, f"SKIP-{i}"), **ATTRIBUTION)
    dispatch(_payment(paid, keys[paid]), **ATTRIBUTION)
    dispatch(commands.ChangeAccountStatus(account_pk=closed, status="closed", reason_code="paid_in_full"), **ATTRIBUTION)
    with pytest.raises(CommandRejected):
        dispatch(_payment(closed, keys[closed]), **ATTRIBUTION)
    events_before = {pk: store.load_stream(pk).count() for pk in (paid, closed)}

    outcome = bulk.post_payments([_payment(pk, keys[pk]) for pk in (paid, closed, fresh)], **ATTRIBUTION)

    assert (outcome.succeeded, outcome.rejected, outcome.skipped) == (1, 1, 1)
    assert {pk: store.load_stream(pk).count() for pk in (paid, closed)} == events_before
    assert Payment.objects.filter(pk__in=keys.values()).count() == 2
    assert store.verify_stream(fresh) == []


@pytest.mark.django_db
def test_bulk_payment_writes_nothing_when_a_version_has_moved(buckets, new_keys):
    steady, moved = new_keys()
    for i, account in enumerate((steady, moved)):
        dispatch(_placement(account, f"MOVED-{i}"), **ATTRIBUTION)
    with connection.cursor() as cursor:
        cursor.execute("UPDATE ar.account SET aggregate_sequence_number = 7 WHERE account_pk = %s", [moved])

    with pytest.raises(ConcurrencyError):
        bulk.post_payments([_payment(pk, key) for pk, key in zip((steady, moved), new_keys())], **ATTRIBUTION)

    assert Payment.objects.count() == 0
    assert store.load_stream(steady).count() == 1


@pytest.mark.django_db
def test_a_failed_chunk_runs_again_one_command_at_a_time(buckets, new_keys):
    accounts = new_keys(3)
    for i, account in enumerate(accounts):
        dispatch(_placement(account, f"FALLBACK-{i}"), **ATTRIBUTION)

    def failing(*args, **kwargs):
        raise RuntimeError("chunk failed")

    tally = drivers.Tally()
    payments = [_payment(pk, key) for pk, key in zip(accounts, new_keys(3))]
    drivers.send_chunk(failing, payments, BATCH, "batch", "batch", tally)

    assert tally.counts["fallback_chunks"] == 1
    assert (tally.counts["sent"], tally.counts["succeeded"], tally.counts["transactions"]) == (3, 3, 3)
    assert len(tally.latencies) == 3
    assert Payment.objects.count() == 3

    # The same chunk again, in bulk: every key already took effect.
    drivers.send_chunk(bulk.post_payments, payments, BATCH, "batch", "batch", tally)
    assert (tally.counts["sent"], tally.counts["succeeded"], tally.counts["transactions"]) == (6, 6, 4)
    assert len(tally.latencies) == 4 and Payment.objects.count() == 3




@pytest.mark.django_db
def test_hashing_off_stores_the_placeholder_and_still_catches_a_conflict(buckets, new_keys, monkeypatch):
    from django.db import transaction

    from wax import hashing

    monkeypatch.setattr(hashing, "HASHING", False)
    single_account, bulk_account = new_keys()
    single_payment, bulk_payment = new_keys()
    dispatch(_placement(single_account, "OFF-1"), **ATTRIBUTION)
    dispatch(_placement(bulk_account, "OFF-2"), **ATTRIBUTION)
    dispatch(_payment(single_account, single_payment), **ATTRIBUTION)
    bulk.post_payments([_payment(bulk_account, bulk_payment)], **ATTRIBUTION)

    for account in (single_account, bulk_account):
        events = list(store.load_stream(account))
        assert [event.event_hash for event in events] == [hashing.PLACEHOLDER_HASH] * 2
        assert [event.prior_event_hash for event in events] == [None, hashing.PLACEHOLDER_HASH]
    assert balances(bulk_account) == balances(single_account)

    # A stale expected version still fails, on the unique constraint.
    attribution = store.Attribution(actor_pk=BATCH, actor_type="batch", occurred_at=RECEIVED_AT)
    with pytest.raises(ConcurrencyError), transaction.atomic():
        store.append("account", single_account, 1, [store.NewEvent("account.status_changed", None, {"status": "x"})], attribution)

@pytest.fixture
def new_keys():
    def _keys(n=2):
        return [uuid.uuid4() for _ in range(n)]

    return _keys
