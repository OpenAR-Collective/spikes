"""Command handlers for the python-direct side. Foundation-written.

Each is hivear/handlers.py's handler of the same name, decided the same way
with the same checks, the same allocation and waterfall functions, and the
same event values, and with Django's object layer replaced by SQL: state rows
are read with the account lock in one statement per kind, new rows are
inserted without an update first, and only the columns that change are
updated. tests/test_direct_twin.py requires the events and state each writes to
match the Django handler's.
"""

import os
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from hivear import commands
from hivear.allocation import AccountPosition, allocate_across_accounts, run_waterfall
from direct import store
from direct.commands import AlreadyApplied, CommandRejected, handles
from direct.db import as_json
from wax import hashing

# Pipeline mode sends a payment's reads in one round trip and its writes in
# another, as the C# sides batch theirs. DIRECT_PIPELINE=0 sends each statement
# on its own; the twin tests run both ways.
PIPELINE = os.environ.get("DIRECT_PIPELINE", "1") != "0"


@dataclass
class Account:
    pk: uuid.UUID
    version: int
    status: str
    currency_code: str
    is_disputed: bool
    list_date: object
    interest_accrued_through: object
    queue_code: str | None


LOCK_SQL = (
    "SELECT account_pk, aggregate_sequence_number, status, currency_code, is_disputed, list_date,"
    " interest_accrued_through, queue_code FROM ar.account WHERE account_pk = ANY(%s)"
    " ORDER BY account_pk FOR UPDATE"
)
BUCKETS_SQL = (
    "SELECT b.account_fk, d.code, b.account_balance_bucket_pk, d.waterfall_priority, b.balance_minor_units"
    " FROM ar.account_balance_bucket b JOIN ar.balance_bucket_definition d"
    " ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk WHERE b.account_fk = ANY(%s)"
)


def _accounts_from(cur, wanted):
    accounts = [Account(*row) for row in cur.fetchall()]
    if len(accounts) != len(wanted):
        raise CommandRejected("One or more accounts do not exist", "account_not_found")
    return accounts


def _buckets_from(cur, accounts):
    by_account = {account.pk: {} for account in accounts}
    for account_fk, code, bucket_pk, priority, balance in cur.fetchall():
        by_account[account_fk][code] = [bucket_pk, priority, balance]
    return by_account


def lock_accounts(cur, account_pks):
    """Row-lock the account state rows in key order (WAX-0013), as
    hivear.handlers._lock_accounts does."""
    wanted = set(account_pks)
    cur.execute(LOCK_SQL, [list(wanted)])
    return _accounts_from(cur, wanted)


def bucket_rows(cur, accounts):
    """{account_pk: {code: [bucket_pk, waterfall_priority, balance]}}."""
    cur.execute(BUCKETS_SQL, [[account.pk for account in accounts]])
    return _buckets_from(cur, accounts)


def _payment_reads(context, command):
    """Whether the payment key is used, the locked accounts, their buckets,
    and the stream heads. In pipeline mode they travel in one round trip; the
    lock still precedes the head read, since the server runs them in order."""
    cur = context.cur
    wanted = set(command.account_pks)
    exists_sql = "SELECT 1 FROM ar.payment WHERE payment_pk = %s"
    if not PIPELINE:
        cur.execute(exists_sql, [command.payment_pk])
        if cur.fetchone():
            return True, None, None, None
        accounts = lock_accounts(cur, wanted)
        return False, accounts, bucket_rows(cur, accounts), None
    conn = cur.connection
    with conn.cursor() as c_lock, conn.cursor() as c_buckets, conn.cursor() as c_heads:
        with conn.pipeline():
            cur.execute(exists_sql, [command.payment_pk])
            c_lock.execute(LOCK_SQL, [list(wanted)])
            c_buckets.execute(BUCKETS_SQL, [list(wanted)])
            if hashing.HASHING:
                c_heads.execute(store.HEADS_SQL, [[command.payment_pk, *wanted]])
        if cur.fetchone():
            return True, None, None, None
        accounts = _accounts_from(c_lock, wanted)
        rows = _buckets_from(c_buckets, accounts)
        heads = store.heads_from(c_heads) if hashing.HASHING else None
    return False, accounts, rows, heads


def first_new_value(cur, aggregate_pk):
    """The new_value of an aggregate's first event, as store.load_stream(pk).first() reads it."""
    cur.execute(
        "SELECT event_type, new_value FROM core.event WHERE aggregate_pk_ref = %s"
        " ORDER BY aggregate_sequence_number LIMIT 1",
        [aggregate_pk],
    )
    row = cur.fetchone()
    return (row[0], as_json(row[1])) if row else (None, None)


def _set_versions(cur, versions):
    if versions:
        pks, numbers = zip(*versions.items())
        cur.execute(
            "UPDATE ar.account a SET aggregate_sequence_number = v.version"
            " FROM unnest(%s::uuid[], %s::integer[]) AS v(pk, version) WHERE a.account_pk = v.pk",
            [list(pks), list(numbers)],
        )


def _set_balances(cur, balances):
    if balances:
        pks, amounts = zip(*balances.items())
        cur.execute(
            "UPDATE ar.account_balance_bucket b SET balance_minor_units = v.balance"
            " FROM unnest(%s::uuid[], %s::bigint[]) AS v(pk, balance) WHERE b.account_balance_bucket_pk = v.pk",
            [list(pks), list(amounts)],
        )


@handles(commands.PlaceAccount)
def place_account(command, context):
    """HIVE-0005: an account needs an itemization date and a balance made of
    registered buckets."""
    cur = context.cur
    accrued_through = command.interest_accrued_through or command.itemization_date
    placed = {
        "client_account_number": command.client_account_number,
        "debt_type": command.debt_type,
        "contract_type": command.contract_type,
        "currency_code": command.currency_code,
        "status": "active",
        "is_disputed": False,
        "list_date": command.list_date.isoformat(),
        "itemization_date": command.itemization_date.isoformat(),
        "balance_buckets": dict(command.opening_balances),
        "interest_accrued_through": accrued_through.isoformat(),
    }
    cur.execute("SELECT 1 FROM ar.account WHERE account_pk = %s", [command.account_pk])
    if cur.fetchone():
        event_type, first = first_new_value(cur, command.account_pk)
        if event_type == "account.placed" and first == placed:
            raise AlreadyApplied()
        raise CommandRejected("A different account already exists under this key", "key_reused")
    cur.execute(
        "SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition WHERE code = ANY(%s)",
        [list(command.opening_balances)],
    )
    definitions = dict(cur.fetchall())
    unknown = sorted(set(command.opening_balances) - set(definitions))
    if unknown:
        raise CommandRejected(f"Unregistered balance buckets: {', '.join(unknown)}", "bucket_not_registered")
    events = context.read_heads([command.account_pk])
    events.record("account", command.account_pk, 0, "account.placed", None, placed, context.reason("account_placed"))
    cur.execute(
        "INSERT INTO ar.account (account_pk, aggregate_sequence_number, client_account_number, debt_type, contract_type,"
        " currency_code, status, is_disputed, list_date, itemization_date, interest_accrued_through, queue_code)"
        " VALUES (%s, 1, %s, %s, %s, %s, 'active', false, %s, %s, %s, NULL)",
        [command.account_pk, command.client_account_number, command.debt_type, command.contract_type,
         command.currency_code, command.list_date, command.itemization_date, accrued_through],
    )
    rows = [(uuid.uuid4(), command.account_pk, definitions[code], amount)
            for code, amount in command.opening_balances.items()]
    cur.execute(
        "INSERT INTO ar.account_balance_bucket (account_balance_bucket_pk, account_fk, balance_bucket_definition_fk,"
        " balance_minor_units) SELECT * FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[], %s::bigint[])",
        [list(column) for column in zip(*rows)],
    )


@handles(commands.OpenDispute)
def open_dispute(command, context):
    cur = context.cur
    (account,) = lock_accounts(cur, [command.account_pk])
    if account.is_disputed:
        raise CommandRejected("Account is already disputed", "already_disputed")
    events = context.read_heads([account.pk])
    version = events.record(
        "account", account.pk, account.version, "dispute.opened", {"is_disputed": False}, {"is_disputed": True},
        context.reason("dispute_raised"), metadata={"dispute_pk": str(command.dispute_pk)},
    )
    cur.execute(
        "UPDATE ar.account SET aggregate_sequence_number = %s, is_disputed = true WHERE account_pk = %s",
        [version, account.pk],
    )


@handles(commands.PostPayment)
def post_payment(command, context):
    """HIVE-0007: allocate across accounts, then run each account's waterfall."""
    cur = context.cur
    request = {
        "account_pks": sorted(str(pk) for pk in set(command.account_pks)),
        "allocation_method": command.allocation_method,
        "directed_allocations": {str(pk): amount for pk, amount in sorted(command.directed_allocations.items())},
        "bucket_amounts": {str(pk): dict(amounts) for pk, amounts in sorted(command.bucket_amounts.items())},
        "consumer_directed_accounts": sorted(str(pk) for pk in set(command.consumer_directed_accounts)),
        "received_at": command.received_at.isoformat() if command.received_at else None,
    }
    used, accounts, rows, heads = _payment_reads(context, command)
    if used:
        _event_type, recorded = first_new_value(cur, command.payment_pk)
        recorded = recorded or {}
        asked = {"consumer_directed_accounts": [], **(recorded.get("request") or {})}
        same = (
            asked == request
            and recorded.get("amount_minor_units") == command.amount_minor_units
            and recorded.get("currency_code") == command.currency_code
        )
        if same:
            raise AlreadyApplied()
        raise CommandRejected("A different payment already exists under this key", "key_reused")
    known = {account.pk for account in accounts}

    if set(command.directed_allocations) & set(command.bucket_amounts):
        raise CommandRejected("An account may carry a directed amount or bucket amounts, not both", "direction_conflict")
    if not (set(command.directed_allocations) | set(command.bucket_amounts)) <= known:
        raise CommandRejected("Directions name an account that is not on this payment", "direction_off_payment")
    for account in accounts:
        if account.status != "active":
            raise CommandRejected(f"Account {account.pk} is not active", "account_not_active")
        if account.currency_code != command.currency_code:
            raise CommandRejected(f"Account {account.pk} is not held in {command.currency_code}", "currency_mismatch")
    stated = set(command.consumer_directed_accounts)
    if not stated <= set(command.bucket_amounts):
        raise CommandRejected(
            "consumer_directed_accounts names an account with no bucket amounts on this payment",
            "direction_off_payment",
        )
    for account in accounts:
        if account.is_disputed and account.pk in command.bucket_amounts and account.pk not in stated:
            raise CommandRejected(
                f"Account {account.pk} is disputed; bucket amounts need the consumer's direction",
                "dispute_needs_direction",
            )
    for account_pk, amounts in command.bucket_amounts.items():
        for code, amount in amounts.items():
            row = rows[account_pk].get(code)
            if row is None or amount > row[2]:
                raise CommandRejected(f"Bucket {code} on account {account_pk} cannot take {amount}", "bucket_cannot_take")
    directed = dict(command.directed_allocations)
    directed.update({pk: sum(amounts.values()) for pk, amounts in command.bucket_amounts.items()})
    if sum(directed.values()) > command.amount_minor_units:
        raise CommandRejected("Directed amounts exceed the payment amount", "directed_exceeds_amount")

    positions = [
        AccountPosition(
            account_pk=account.pk,
            list_date=account.list_date,
            is_disputed=account.is_disputed,
            balance_minor_units=sum(row[2] for row in rows[account.pk].values()),
        )
        for account in accounts
    ]
    shares, unapplied = allocate_across_accounts(
        command.amount_minor_units, positions, command.allocation_method, directed,
        fixed=frozenset(command.bucket_amounts),
    )
    distribution = {}
    for account in accounts:
        if account.pk in command.bucket_amounts:
            paydown = dict(command.bucket_amounts[account.pk])
        else:
            paydown, _ = run_waterfall(
                shares[account.pk], [(code, row[1], row[2]) for code, row in rows[account.pk].items()]
            )
        if paydown:
            distribution[str(account.pk)] = paydown
    applied = sum(sum(paydown.values()) for paydown in distribution.values())
    if applied + unapplied != command.amount_minor_units:
        raise RuntimeError(
            f"Payment {command.payment_pk} does not balance: {applied} applied plus "
            f"{unapplied} unapplied is not {command.amount_minor_units}"
        )

    received_at = command.received_at or context.occurred_at
    if PIPELINE:
        events = context.events = store.EventWriter(context.attribution, heads)
    else:
        events = context.read_heads([command.payment_pk, *known])
    reason = context.reason("payment_received")
    events.record(
        "payment", command.payment_pk, 0, "payment.posted", None,
        {
            "status": "posted",
            "amount_minor_units": command.amount_minor_units,
            "currency_code": command.currency_code,
            "received_at": received_at.isoformat(),
            "distribution": distribution,
            "unapplied_minor_units": unapplied,
            "request": request,
        },
        reason,
    )
    payment_row = [command.payment_pk, command.amount_minor_units, command.currency_code, received_at,
                   Jsonb(distribution), unapplied]
    balances, versions = {}, {}
    for account in accounts:
        paydown = distribution.get(str(account.pk))
        if paydown:
            prior, new = {}, {}
            for code, amount in paydown.items():
                row = rows[account.pk][code]
                prior[code] = row[2]
                row[2] -= amount
                new[code] = row[2]
                balances[row[0]] = row[2]
            versions[account.pk] = events.record(
                "account", account.pk, account.version, "account.payment_applied",
                {"balance_buckets": prior}, {"balance_buckets": new}, reason,
                metadata={
                    "payment_pk": str(command.payment_pk),
                    "received_at": received_at.isoformat(),
                    "bucket_amounts": paydown,
                },
            )
    insert_payment = (
        "INSERT INTO ar.payment (payment_pk, aggregate_sequence_number, amount_minor_units, currency_code, status,"
        " received_at, distribution, unapplied_minor_units) VALUES (%s, 1, %s, %s, 'posted', %s, %s, %s)"
    )
    if not PIPELINE:
        cur.execute(insert_payment, payment_row)
        _set_balances(cur, balances)
        _set_versions(cur, versions)
        return
    conn = cur.connection
    with conn.cursor() as c_events:
        try:
            with conn.pipeline():
                events.send_insert(c_events)
                cur.execute(insert_payment, payment_row)
                _set_balances(cur, balances)
                _set_versions(cur, versions)
        except psycopg.errors.UniqueViolation as exc:
            raise store.ConcurrencyError(str(exc)) from exc
        context.position = events.position(c_events)


@handles(commands.AccrueInterest)
def accrue_interest(command, context):
    cur = context.cur
    (account,) = lock_accounts(cur, [command.account_pk])
    if account.status != "active" or command.accrual_date <= account.interest_accrued_through:
        return
    rows = bucket_rows(cur, [account])[account.pk]
    principal = rows["principal"][2] if "principal" in rows else 0
    days = (command.accrual_date - account.interest_accrued_through).days
    amount = principal * command.annual_rate_basis_points * days // (10000 * 365)
    interest = rows.get("interest")
    before = interest[2] if interest else 0
    if interest is None:
        cur.execute(
            "INSERT INTO ar.account_balance_bucket (account_balance_bucket_pk, account_fk, balance_bucket_definition_fk,"
            " balance_minor_units) SELECT %s, %s, balance_bucket_definition_pk, %s"
            " FROM ar.balance_bucket_definition WHERE code = 'interest'",
            [uuid.uuid4(), account.pk, before + amount],
        )
    else:
        _set_balances(cur, {interest[0]: before + amount})
    events = context.read_heads([account.pk])
    version = events.record(
        "account", account.pk, account.version, "account.interest_accrued",
        {"balance_buckets": {"interest": before}, "interest_accrued_through": account.interest_accrued_through.isoformat()},
        {"balance_buckets": {"interest": before + amount}, "interest_accrued_through": command.accrual_date.isoformat()},
        context.reason("interest_accrued"),
        metadata={"days": days, "annual_rate_basis_points": command.annual_rate_basis_points, "amount_minor_units": amount},
    )
    cur.execute(
        "UPDATE ar.account SET aggregate_sequence_number = %s, interest_accrued_through = %s WHERE account_pk = %s",
        [version, command.accrual_date, account.pk],
    )
