import uuid
from datetime import date

import pytest

from hivear import commands
from hivear.models import BalanceBucketDefinition
from wax.commands import dispatch

ACTOR = uuid.UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def fresh_interlock():
    """The schema interlock caches its answer for a few seconds in production."""
    from wax import release

    release._cache["problems"] = None
    yield
    release._cache["problems"] = None


@pytest.fixture
def buckets(db):
    for code, priority in [("interest", 10), ("fee", 20), ("principal", 30)]:
        BalanceBucketDefinition.objects.get_or_create(
            code=code, defaults={"label": code.title(), "waterfall_priority": priority}
        )


@pytest.fixture
def run():
    def _run(command):
        dispatch(command, actor_pk=ACTOR, source="test")
        return command

    return _run


@pytest.fixture
def place(buckets, run):
    def _place(listed=date(2026, 1, 15), **opening_balances):
        return run(
            commands.PlaceAccount(
                client_account_number=f"A-{uuid.uuid4().hex[:8]}",
                debt_type="personal_loan",
                contract_type="third_party_contingency",
                currency_code="USD",
                list_date=listed,
                itemization_date=date(2025, 12, 31),
                opening_balances=opening_balances or {"principal": 100_000, "interest": 5_000},
            )
        ).account_pk

    return _place


def balances(account_pk):
    from hivear.models import AccountBalanceBucket

    return {
        row.balance_bucket_definition.code: row.balance_minor_units
        for row in AccountBalanceBucket.objects.filter(account_id=account_pk).select_related(
            "balance_bucket_definition"
        )
    }
