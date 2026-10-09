"""The seed loader, the batch coordinator, and the derived keys
(WORKLOADS.md, "Deterministic keys", "The seed", and "Batch workloads").
Foundation-written.
"""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.db import connection

from bakeoff.keys import BATCH, account_pk, balances, derived_uuid
from bakeoff.seed import seed
from bakeoff.verify import verify_chain
from hivear import commands
from hivear.models import Account, AccountSummaryReadModel, Payment
from tests.conftest import balances as bucket_balances
from wax import batch, projections, rules, store
from wax.commands import dispatch

UUID_VECTORS = Path(os.environ.get("UUID_VECTORS", Path(__file__).resolve().parents[2] / "shared" / "derived-uuids.json"))


def _uuid_vectors():
    if not UUID_VECTORS.exists():
        return [pytest.param(None, id="derived-uuids.json not found")]
    return [pytest.param(v, id=v["name"] or "empty name") for v in json.loads(UUID_VECTORS.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("vector", _uuid_vectors())
def test_derived_uuid_matches_shared_vector(vector):
    assert vector is not None, f"{UUID_VECTORS} is missing; the vectors pin both sides to one key derivation"
    assert str(derived_uuid(vector["name"])) == vector["uuid"]


@pytest.fixture
def empty_ar():
    """Django's flush between committing tests finds only tables in the
    default schema, so the prototype's schema-qualified ar tables keep rows
    from earlier tests. The seed loads only into an empty database."""
    with connection.cursor() as cursor:
        cursor.execute(
            "TRUNCATE ar.account, ar.account_balance_bucket, ar.task, ar.payment, ar.account_summary_read_model,"
            " ar.payment_read_model, ar.account_payment_read_model_v2"
        )


@pytest.mark.django_db(transaction=True)
def test_seed_writes_what_place_account_would_and_starts_readers_at_the_head(empty_ar, buckets):
    result = seed(30, chunk=7)
    assert result["accounts"] == 30
    for i in (0, 13, 29):
        account = Account.objects.get(pk=account_pk(i))
        assert account.client_account_number == f"BK-{i:09d}"
        assert account.interest_accrued_through == date(2026, 9, 30)
        assert account.aggregate_sequence_number == 1
        assert bucket_balances(account.pk) == balances(i)
        summary = AccountSummaryReadModel.objects.get(pk=account.pk)
        assert summary.balance_minor_units == sum(balances(i).values())
        placed = store.load_stream(account.pk).get()
        assert placed.new_value["balance_buckets"] == balances(i)
        assert placed.occurred_at == datetime(2026, 9, 30, tzinfo=UTC)
        assert (placed.source, placed.actor_type) == ("seed", "batch")
    assert verify_chain()["ok"]
    head = store.current_position()
    positions = {row["projection"]: row["position"] for row in projections.status()}
    assert positions and all(position == head for position in positions.values())
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s",
            [rules.BASE_POSITION],
        )
        assert cursor.fetchone()[0] == head

    # A batch over the seed: chunks cover every account once, in the
    # database's key order, and a second claim pass finds nothing left.
    run_pk, chunks = batch.create_run("batch-payments", "ar.account", "account_pk", chunk_size=7)
    assert chunks == 5
    seen = []
    while (claim := batch.claim_chunk(run_pk, "test")) is not None:
        chunk_pk, start, end = claim
        keys = batch.chunk_keys("ar.account", "account_pk", start, end)
        seen.extend(keys)
        for key in keys:
            dispatch(
                commands.PostPayment(
                    payment_pk=derived_uuid(f"bakeoff:payment:{run_pk}:{key}"), account_pks=[key],
                    amount_minor_units=2500, currency_code="USD", received_at=datetime(2026, 10, 1, 6, tzinfo=UTC),
                ),
                actor_pk=BATCH, actor_type="batch", source="batch",
            )
        batch.complete_chunk(chunk_pk)
    assert len(seen) == len(set(seen)) == 30
    with connection.cursor() as cursor:
        cursor.execute("SELECT account_pk FROM ar.account ORDER BY account_pk")
        assert seen == [row[0] for row in cursor.fetchall()]
    assert batch.finish_run(run_pk)
    assert Payment.objects.count() == 30
    assert verify_chain()["ok"]
