"""Payment distribution (HIVE-0007 Payment Application and Waterfall).

Pure functions over integers: no database, no ledger posting. Allocation
across accounts runs first, then each account's bucket waterfall.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID


@dataclass(frozen=True)
class AccountPosition:
    account_pk: UUID
    list_date: date
    is_disputed: bool
    balance_minor_units: int


def allocate_across_accounts(amount, positions, method, directed, fixed=frozenset()):
    """Split one payment across accounts. Returns ({account_pk: amount}, remainder).

    HIVE-0007 sets a floor that configuration may not override: the consumer's
    directions are honored first, and a disputed account receives nothing the
    consumer did not direct to it. An account in fixed receives exactly its
    directed amount and takes no part in the default spread.
    """
    shares = {position.account_pk: 0 for position in positions}
    room = {position.account_pk: position.balance_minor_units for position in positions}
    remaining = amount

    for account_pk, directed_amount in directed.items():
        applied = min(directed_amount, room[account_pk])
        shares[account_pk] += applied
        room[account_pk] -= applied
        remaining -= directed_amount
    # Directed money beyond an account's balance is not moved to another account.
    held_back = sum(directed.values()) - sum(shares.values())
    remaining_to_spread = remaining

    eligible = sorted(
        (p for p in positions if not p.is_disputed and p.account_pk not in fixed),
        key=lambda p: (p.list_date, str(p.account_pk)),
    )

    if method == "oldest_first":
        for position in eligible:
            applied = min(remaining_to_spread, room[position.account_pk])
            shares[position.account_pk] += applied
            room[position.account_pk] -= applied
            remaining_to_spread -= applied
    elif method == "even_split":
        # Split evenly, cap each share at the account's balance, and re-split
        # whatever the capped accounts could not absorb.
        while remaining_to_spread > 0:
            open_accounts = [p for p in eligible if room[p.account_pk] > 0]
            if not open_accounts:
                break
            base, extra = divmod(remaining_to_spread, len(open_accounts))
            for index, position in enumerate(open_accounts):
                applied = min(base + (1 if index < extra else 0), room[position.account_pk])
                shares[position.account_pk] += applied
                room[position.account_pk] -= applied
                remaining_to_spread -= applied
    else:
        raise ValueError(f"Unknown allocation method: {method}")

    return shares, remaining_to_spread + held_back


def run_waterfall(amount, buckets):
    """Fill buckets in priority order. buckets is [(code, priority, balance)].

    Returns ({code: paydown}, remainder). Buckets with no balance are skipped.
    """
    paydown = {}
    remaining = amount
    for code, _priority, balance in sorted(buckets, key=lambda bucket: (bucket[1], bucket[0])):
        applied = min(remaining, balance)
        if applied > 0:
            paydown[code] = applied
            remaining -= applied
    return paydown, remaining
