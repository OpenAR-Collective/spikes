"""The python-direct command path against the Django path. Foundation-written.

Each test places twin accounts, sends one twin's command through Andy
Troiano's dispatch (Django) and the other's through direct.commands.dispatch
(psycopg), and requires the same events and the same state, apart from the
keys that tell the twins apart and the values the store assigns. The direct path
runs on its own connection, as it does in the bench, so these tests commit.
"""

import json
import uuid
from datetime import UTC, date, datetime

import pytest
from django.db import connection

from bakeoff.keys import BATCH
from hivear import commands
from hivear.models import Account, AccountBalanceBucket, Payment
from tests.conftest import balances
from wax import store
from wax.commands import CommandRejected, dispatch

RECEIVED_AT = datetime(2026, 10, 1, 6, tzinfo=UTC)
WHO = {"actor_pk": BATCH, "actor_type": "batch", "source": "batch"}


@pytest.fixture(params=[True, False], ids=["pipeline", "sequential"])
def direct_session(request, monkeypatch):
    """A direct session on the test database, as a bench worker would hold one,
    with the payment handler's pipeline mode on and then off."""
    from direct import commands as direct_commands
    from direct import handlers
    from direct.db import connect

    monkeypatch.setattr(handlers, "PIPELINE", request.param)

    settings = connection.settings_dict
    conn = connect(dbname=settings["NAME"], user=settings["USER"], password=settings["PASSWORD"],
                   host=settings["HOST"], port=settings["PORT"])
    yield direct_commands.Session(conn)
    conn.close()


def direct_dispatch(session, command):
    from direct.commands import dispatch as dispatch_direct

    return dispatch_direct(session, command, **WHO)


def direct_rejected():
    from direct.commands import CommandRejected as DirectRejected

    return DirectRejected


def place(account_pk, number, listed=date(2024, 3, 1), opening=None):
    dispatch(
        commands.PlaceAccount(
            account_pk=account_pk, client_account_number=number, debt_type="personal_loan",
            contract_type="third_party_contingency", currency_code="USD", list_date=listed,
            itemization_date=date(2024, 1, 31), interest_accrued_through=date(2026, 9, 30),
            opening_balances=opening or {"principal": 1_000, "interest": 700, "fee": 1_500},
        ),
        **WHO,
    )


def twins(n=1, **kwargs):
    """n pairs of twin accounts: (django_pks, direct_pks), placed through Django.
    Each side's keys are sorted, so twins hold the same rank in key order,
    which decides lock order, allocation order, and the order of the request."""
    django_side, direct_side = [], []
    keys = {side: sorted(uuid.uuid4() for _ in range(n)) for side in ("D", "R")}
    for i in range(n):
        for side, label in ((django_side, "D"), (direct_side, "R")):
            pk = keys[label][i]
            listed = kwargs.get("listed", [date(2024, 3, 1 + j) for j in range(n)])[i]
            opening = kwargs.get("opening", [None] * n)[i]
            place(pk, f"TWIN-{label}-{pk.hex[:8]}", listed=listed, opening=opening)
            side.append(pk)
    return django_side, direct_side


def rename(text, names):
    """Replace each twin's key, and the client account number twins() gave
    it, with the name both twins share."""
    for key, name in names.items():
        text = text.replace(str(key), name)
        for label in ("D", "R"):
            text = text.replace(f"TWIN-{label}-{key.hex[:8]}", f"TWIN-{name}")
    return text


def events_text(aggregate_pks, names):
    """Every event on these aggregates as comparable text, with the twins'
    keys replaced by names and the store-assigned values left out."""
    rows = []
    for aggregate_pk in aggregate_pks:
        for event in store.load_stream(aggregate_pk):
            rows.append({
                field: getattr(event, field)
                for field in ("aggregate_sequence_number", "event_type", "schema_version", "aggregate_pk_ref",
                              "aggregate_type", "actor_pk_ref", "actor_type", "prior_value", "new_value",
                              "reason_code_fk", "source", "correlation_pk_ref", "correlation_type", "metadata")
            } | {"chained": event.prior_event_hash is not None})
    text = rename(json.dumps(rows, sort_keys=True, default=str), names)
    for row in rows:
        for value in (row["prior_value"], row["new_value"], row["metadata"]):
            if isinstance(value, str):
                raise AssertionError("jsonb read back as text")
    return text


def state(account_pks, payment_pks, names):
    accounts = []
    for pk in account_pks:
        account = Account.objects.get(pk=pk)
        accounts.append([account.aggregate_sequence_number, account.status, account.is_disputed,
                         account.interest_accrued_through, account.queue_code, balances(pk)])
    payments = []
    for pk in payment_pks:
        payment = Payment.objects.filter(pk=pk).first()
        payments.append(None if payment is None else [
            payment.aggregate_sequence_number, payment.amount_minor_units, payment.currency_code, payment.status,
            payment.received_at, payment.distribution, payment.unapplied_minor_units])
    return rename(json.dumps([accounts, payments], sort_keys=True, default=str), names)


def assert_twins_match(django_accounts, direct_accounts, django_payments=(), direct_payments=(), other_keys=()):
    """other_keys: (django_key, direct_key) pairs, such as dispute keys, that
    must also read the same."""
    d_names = {pk: f"ACCOUNT{i}" for i, pk in enumerate(django_accounts)}
    d_names |= {pk: f"PAYMENT{i}" for i, pk in enumerate(django_payments)}
    r_names = {pk: f"ACCOUNT{i}" for i, pk in enumerate(direct_accounts)}
    r_names |= {pk: f"PAYMENT{i}" for i, pk in enumerate(direct_payments)}
    for i, (d_key, r_key) in enumerate(other_keys):
        d_names[d_key] = r_names[r_key] = f"KEY{i}"
    assert events_text([*direct_accounts, *direct_payments], r_names) == events_text([*django_accounts, *django_payments], d_names)
    assert state(direct_accounts, direct_payments, r_names) == state(django_accounts, django_payments, d_names)
    for pk in (*direct_accounts, *direct_payments):
        assert store.verify_stream(pk) == []


def payment(account_pks, payment_pk, amount=2_500, **kwargs):
    return commands.PostPayment(payment_pk=payment_pk, account_pks=account_pks, amount_minor_units=amount,
                                currency_code="USD", received_at=RECEIVED_AT, **kwargs)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("amount", [2_500, 2_000_000, 1])
def test_a_one_account_payment_matches(buckets, direct_session, amount):
    (d,), (r,) = twins()
    dp, rp = uuid.uuid4(), uuid.uuid4()
    dispatch(payment([d], dp, amount), **WHO)
    position = direct_dispatch(direct_session, payment([r], rp, amount))
    assert position > 0
    assert_twins_match([d], [r], [dp], [rp])


@pytest.mark.django_db(transaction=True)
def test_a_multi_account_payment_with_directions_and_a_dispute_matches(buckets, direct_session):
    d_side, r_side = twins(3)
    disputes = (uuid.uuid4(), uuid.uuid4())
    dispatch(commands.OpenDispute(account_pk=d_side[2], dispute_pk=disputes[0]), **WHO)
    dispatch(commands.OpenDispute(account_pk=r_side[2], dispute_pk=disputes[1]), **WHO)
    dp, rp = uuid.uuid4(), uuid.uuid4()

    def command(side, key):
        return payment(
            side, key, 5_000, allocation_method="even_split",
            directed_allocations={side[0]: 800},
            bucket_amounts={side[2]: {"fee": 300}},
            consumer_directed_accounts=[side[2]],
        )

    dispatch(command(d_side, dp), **WHO)
    direct_dispatch(direct_session, command(r_side, rp))
    assert_twins_match(d_side, r_side, [dp], [rp], other_keys=[disputes])


@pytest.mark.django_db(transaction=True)
def test_a_repeated_payment_is_answered_once_and_a_reused_key_is_refused(buckets, direct_session):
    (d,), (r,) = twins()
    dp, rp = uuid.uuid4(), uuid.uuid4()
    for _ in range(2):
        dispatch(payment([d], dp), **WHO)
        direct_dispatch(direct_session, payment([r], rp))
    assert_twins_match([d], [r], [dp], [rp])
    with pytest.raises(CommandRejected):
        dispatch(payment([d], dp, 2_600), **WHO)
    with pytest.raises(direct_rejected()) as refused:
        direct_dispatch(direct_session, payment([r], rp, 2_600))
    assert refused.value.code == "key_reused"


@pytest.mark.django_db(transaction=True)
def test_refusals_match(buckets, direct_session):
    (d,), (r,) = twins()
    for pk in (d, r):
        dispatch(commands.ChangeAccountStatus(account_pk=pk, status="closed", reason_code="paid_in_full"), **WHO)
    with pytest.raises(CommandRejected) as django_refusal:
        dispatch(payment([d], uuid.uuid4()), **WHO)
    with pytest.raises(direct_rejected()) as direct_refusal:
        direct_dispatch(direct_session, payment([r], uuid.uuid4()))
    assert direct_refusal.value.code == django_refusal.value.code == "account_not_active"
    with pytest.raises(direct_rejected()) as missing:
        direct_dispatch(direct_session, payment([uuid.uuid4()], uuid.uuid4()))
    assert missing.value.code == "account_not_found"
    assert_twins_match([d], [r])


@pytest.mark.django_db(transaction=True)
def test_a_moved_version_is_a_conflict_and_writes_nothing(buckets, direct_session):
    from direct.store import ConcurrencyError

    (_d,), (r,) = twins()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE ar.account SET aggregate_sequence_number = 7 WHERE account_pk = %s", [r])
    rp = uuid.uuid4()
    with pytest.raises(ConcurrencyError):
        direct_dispatch(direct_session, payment([r], rp))
    assert not Payment.objects.filter(pk=rp).exists()
    assert store.load_stream(r).count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("accrual_date,status", [
    (date(2026, 10, 1), "active"),
    (date(2026, 10, 31), "active"),
    (date(2026, 9, 30), "active"),
    (date(2026, 10, 1), "closed"),
])
def test_accrual_matches(buckets, direct_session, accrual_date, status):
    (d,), (r,) = twins()
    if status != "active":
        for pk in (d, r):
            dispatch(commands.ChangeAccountStatus(account_pk=pk, status=status, reason_code="paid_in_full"), **WHO)
    for pk, send in ((d, lambda c: dispatch(c, **WHO)), (r, lambda c: direct_dispatch(direct_session, c))):
        send(commands.AccrueInterest(account_pk=pk, accrual_date=accrual_date, annual_rate_basis_points=1_800))
    assert_twins_match([d], [r])


@pytest.mark.django_db(transaction=True)
def test_accrual_creates_a_missing_interest_bucket_like_the_django_path(buckets, direct_session):
    (d,), (r,) = twins(opening=[{"principal": 500_000}])
    for pk, send in ((d, lambda c: dispatch(c, **WHO)), (r, lambda c: direct_dispatch(direct_session, c))):
        send(commands.AccrueInterest(account_pk=pk, accrual_date=date(2026, 10, 31), annual_rate_basis_points=1_800))
    assert AccountBalanceBucket.objects.filter(account_id=r, balance_bucket_definition__code="interest").exists()
    assert_twins_match([d], [r])


@pytest.mark.django_db(transaction=True)
def test_a_placement_matches_and_a_repeat_is_answered_once(buckets, direct_session):
    d, r = uuid.uuid4(), uuid.uuid4()

    def placement(pk, number):
        return commands.PlaceAccount(
            account_pk=pk, client_account_number=number, debt_type="personal_loan",
            contract_type="third_party_contingency", currency_code="USD", list_date=date(2026, 9, 1),
            itemization_date=date(2026, 8, 2), opening_balances={"principal": 5_000, "fee": 250},
        )

    for _ in range(2):
        dispatch(placement(d, "PLACE-SAME"), **WHO)
        direct_dispatch(direct_session, placement(r, "PLACE-SAME"))
    assert_twins_match([d], [r])
    with pytest.raises(direct_rejected()) as refused:
        direct_dispatch(direct_session, placement(r, "PLACE-OTHER"))
    assert refused.value.code == "key_reused"


@pytest.mark.django_db(transaction=True)
def test_a_dispute_matches_and_a_second_is_refused(buckets, direct_session):
    (d,), (r,) = twins()
    dispute_d, dispute_r = uuid.uuid4(), uuid.uuid4()
    dispatch(commands.OpenDispute(account_pk=d, dispute_pk=dispute_d), **WHO)
    direct_dispatch(direct_session, commands.OpenDispute(account_pk=r, dispute_pk=dispute_r))
    d_names = {d: "ACCOUNT0", dispute_d: "DISPUTE"}
    r_names = {r: "ACCOUNT0", dispute_r: "DISPUTE"}
    assert events_text([r], r_names) == events_text([d], d_names)
    assert state([r], [], r_names) == state([d], [], d_names)
    with pytest.raises(direct_rejected()) as refused:
        direct_dispatch(direct_session, commands.OpenDispute(account_pk=r))
    assert refused.value.code == "already_disputed"


@pytest.mark.django_db(transaction=True)
def test_hashing_off_stores_the_placeholder_on_the_direct_path(buckets, direct_session, monkeypatch):
    from wax import hashing

    (d,), (r,) = twins()
    monkeypatch.setattr(hashing, "HASHING", False)
    dp, rp = uuid.uuid4(), uuid.uuid4()
    dispatch(payment([d], dp), **WHO)
    direct_dispatch(direct_session, payment([r], rp))
    for pk in (d, r):
        events = list(store.load_stream(pk))
        assert [event.event_hash for event in events][1:] == [hashing.PLACEHOLDER_HASH]
        assert events[1].prior_event_hash == hashing.PLACEHOLDER_HASH


@pytest.mark.django_db(transaction=True)
def test_with_hashing_off_a_stale_version_is_caught_by_the_unique_constraint(buckets, direct_session, monkeypatch):
    from direct.store import ConcurrencyError
    from wax import hashing

    (_d,), (r,) = twins()
    monkeypatch.setattr(hashing, "HASHING", False)
    with connection.cursor() as cursor:
        cursor.execute("UPDATE ar.account SET aggregate_sequence_number = 0 WHERE account_pk = %s", [r])
    rp = uuid.uuid4()
    with pytest.raises(ConcurrencyError):
        direct_dispatch(direct_session, payment([r], rp))
    assert not Payment.objects.filter(pk=rp).exists()
    assert store.load_stream(r).count() == 1
