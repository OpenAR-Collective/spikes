"""Deterministic keys, actors, and seed values, exactly as WORKLOADS.md
defines them, so both sides of the bake-off seed the same accounts and a
retried command reuses its key. Foundation-written.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime, timedelta


def derived_uuid(name: str) -> uuid.UUID:
    """The first 16 bytes of SHA-256 of the UTF-8 name, as a version 4 UUID."""
    return uuid.UUID(bytes=hashlib.sha256(name.encode()).digest()[:16], version=4)


# Actors, by source.
SEEDER = derived_uuid("bakeoff:seeder")
RULE_RUNNER = derived_uuid("bakeoff:rule-runner")
REGISTER = derived_uuid("bakeoff:register")
BATCH = derived_uuid("bakeoff:batch")
INBOUND = derived_uuid("bakeoff:inbound")

SEED_OCCURRED_AT = datetime(2026, 9, 30, tzinfo=UTC)
INTEREST_ACCRUED_THROUGH = date(2026, 9, 30)


def account_pk(i: int) -> uuid.UUID:
    return derived_uuid(f"bakeoff:account:{i}")


def balances(i: int) -> dict:
    return {
        "principal": 50000 + (i * 7919) % 950000,
        "interest": 100 + (i * 104729) % 20000,
        "fee": 500 + (i % 3) * 1000,
    }


def seed_list_date(i: int) -> date:
    return date(2020, 1, 1) + timedelta(days=i % 2190)
