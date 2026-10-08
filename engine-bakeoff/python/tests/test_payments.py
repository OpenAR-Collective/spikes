from datetime import date

import pytest

from hivear import commands
from hivear.models import Payment
from tests.conftest import balances
from wax import store
from wax.commands import CommandRejected

pytestmark = pytest.mark.django_db


def post(run, account_pks, amount, **options):
    return run(
        commands.PostPayment(
            account_pks=account_pks, amount_minor_units=amount, currency_code="USD", **options
        )
    ).payment_pk


def test_waterfall_pays_buckets_in_priority_order(place, run):
    account_pk = place(principal=100_000, interest=5_000, fee=2_000)
    payment_pk = post(run, [account_pk], 8_000)
    assert balances(account_pk) == {"interest": 0, "fee": 0, "principal": 99_000}

    applied = store.load_stream(account_pk).last()
    assert applied.event_type == "account.payment_applied"
    assert applied.prior_value == {"balance_buckets": {"interest": 5_000, "fee": 2_000, "principal": 100_000}}
    assert applied.new_value == {"balance_buckets": {"interest": 0, "fee": 0, "principal": 99_000}}
    assert applied.metadata["payment_pk"] == str(payment_pk)
    # One logical operation, two aggregates, one event each.
    assert [e.event_type for e in store.load_stream(payment_pk)] == ["payment.posted"]


def test_per_payment_bucket_override_beats_the_waterfall(place, run):
    account_pk = place(principal=100_000, interest=5_000)
    post(run, [account_pk], 3_000, bucket_amounts={account_pk: {"principal": 3_000}})
    assert balances(account_pk) == {"principal": 97_000, "interest": 5_000}


def test_disputed_account_gets_nothing_the_consumer_did_not_direct(place, run):
    older = place(listed=date(2025, 1, 1), principal=10_000)
    newer = place(listed=date(2026, 1, 1), principal=10_000)
    run(commands.OpenDispute(account_pk=older))

    post(run, [older, newer], 4_000)
    assert balances(older) == {"principal": 10_000}
    assert balances(newer) == {"principal": 6_000}

    post(run, [older, newer], 1_000, directed_allocations={older: 1_000})
    assert balances(older) == {"principal": 9_000}
    assert balances(newer) == {"principal": 6_000}


def test_oldest_first_and_even_split_allocation(place, run):
    older = place(listed=date(2025, 1, 1), principal=1_000)
    newer = place(listed=date(2026, 1, 1), principal=10_000)
    post(run, [older, newer], 3_000, allocation_method="oldest_first")
    assert (balances(older), balances(newer)) == ({"principal": 0}, {"principal": 8_000})

    first = place(listed=date(2025, 1, 1), principal=10_000)
    second = place(listed=date(2026, 1, 1), principal=10_000)
    post(run, [first, second], 1_001, allocation_method="even_split")
    assert (balances(first), balances(second)) == ({"principal": 9_499}, {"principal": 9_500})


def test_overpayment_is_held_as_unapplied_not_lost(place, run):
    account_pk = place(principal=1_000)
    payment = Payment.objects.get(pk=post(run, [account_pk], 1_500))
    assert balances(account_pk) == {"principal": 0}
    assert payment.unapplied_minor_units == 500
    assert sum(payment.distribution[str(account_pk)].values()) + 500 == payment.amount_minor_units


def test_reversal_unwinds_the_recorded_distribution(place, run):
    account_pk = place(principal=100_000, interest=5_000)
    before = balances(account_pk)
    payment_pk = post(run, [account_pk], 7_000)
    run(commands.ReversePayment(payment_pk=payment_pk))

    assert balances(account_pk) == before
    assert Payment.objects.get(pk=payment_pk).status == "reversed"
    assert store.verify_stream(account_pk) == [] and store.verify_stream(payment_pk) == []
    with pytest.raises(CommandRejected):
        run(commands.ReversePayment(payment_pk=payment_pk))


def test_rejected_command_writes_nothing(place, run):
    account_pk = place(principal=1_000)
    with pytest.raises(CommandRejected):
        post(run, [account_pk], 500, bucket_amounts={account_pk: {"principal": 5_000}})
    assert store.load_stream(account_pk).count() == 1
    assert balances(account_pk) == {"principal": 1_000}


def test_bucket_state_equals_replay_of_the_event_stream(place, run):
    """WAX-0001: state tables are rebuildable from core.event."""
    account_pk = place(principal=100_000, interest=5_000, fee=1_000)
    first = post(run, [account_pk], 5_500)
    post(run, [account_pk], 20_000)
    run(commands.ReversePayment(payment_pk=first))

    replayed = {}
    for event in store.load_stream(account_pk):
        replayed.update((event.new_value or {}).get("balance_buckets", {}))
    assert replayed == balances(account_pk)


def test_bucket_override_smaller_than_the_payment_loses_no_money(place, run):
    """A bucket override smaller than the payment once lost the remainder."""
    account_pk = place(principal=100_000, interest=5_000)
    payment = Payment.objects.get(
        pk=post(run, [account_pk], 10_000, bucket_amounts={account_pk: {"principal": 3_000}})
    )
    assert balances(account_pk) == {"principal": 97_000, "interest": 5_000}
    assert payment.unapplied_minor_units == 7_000

    older = place(listed=date(2025, 1, 1), principal=50_000)
    newer = place(listed=date(2026, 1, 1), principal=50_000)
    payment = Payment.objects.get(
        pk=post(run, [older, newer], 10_000, bucket_amounts={older: {"principal": 1_000}})
    )
    assert (balances(older), balances(newer)) == ({"principal": 49_000}, {"principal": 41_000})
    applied = sum(sum(amounts.values()) for amounts in payment.distribution.values())
    assert applied + payment.unapplied_minor_units == payment.amount_minor_units == 10_000
