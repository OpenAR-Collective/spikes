"""Bulk chunk writes (WORKLOADS.md, "Bulk workloads"). Foundation-written.

A chunk of aggregates is written in one transaction with set-based
statements, as the WAX-0024 change being drafted allows for designated bulk
operations. Each aggregate's state row is locked in key order and its
version checked against its stream head, every event is hashed here and
chained to its own aggregate's previous event, and the events and the state
they change commit together. Each item is decided by the same rules as its
command, and every stored event is the one the single-command path would
store, so the read models, the rules, the gates, and verify-chain cannot
tell them apart. tests/test_bakeoff_bulk.py holds the two paths to that.

Events are loaded with COPY in the order they were recorded, so the event
table's insert trigger numbers them item by item, as the single-command path
would. New rows are loaded with COPY too; changed rows are updated from
unnest() arrays.
"""

import uuid
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.utils import timezone
from psycopg.types.json import Jsonb

from hivear.allocation import AccountPosition, allocate_across_accounts, run_waterfall
from wax import hashing, release
from wax.commands import _reason_code_pk
from wax.hashing import compute_event_hash
from wax.store import ConcurrencyError

EVENT_COLUMNS = (
    "event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type,"
    " occurred_at, actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, source,"
    " correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash"
)
PAYMENT_COLUMNS = (
    "payment_pk, aggregate_sequence_number, amount_minor_units, currency_code, status, received_at,"
    " distribution, unapplied_minor_units"
)
ACCOUNT_COLUMNS = (
    "account_pk, aggregate_sequence_number, client_account_number, debt_type, contract_type, currency_code,"
    " status, is_disputed, list_date, itemization_date, interest_accrued_through, queue_code"
)
BUCKET_COLUMNS = "account_balance_bucket_pk, account_fk, balance_bucket_definition_fk, balance_minor_units"


@dataclass
class Outcome:
    """What a chunk did, item by item. A skipped item's key had already taken
    effect, which the single-command path answers as success."""

    succeeded: int = 0
    rejected: int = 0
    skipped: int = 0


@dataclass
class _Writes:
    """A chunk's writes, gathered in memory and sent as a few set-based statements."""

    actor_pk: uuid.UUID
    actor_type: str
    source: str
    occurred_at: object = field(default_factory=timezone.now)
    events: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    buckets: list = field(default_factory=list)
    balances: list = field(default_factory=list)
    versions: list = field(default_factory=list)

    def event(self, aggregate_type, aggregate_pk, version, prior_hash, event_type, prior, new, reason_pk, metadata=None):
        """Hash one event, chained to its own aggregate's previous one, and queue it."""
        event_hash = hashing.PLACEHOLDER_HASH if not hashing.HASHING else compute_event_hash(
            aggregate_type=aggregate_type,
            aggregate_pk_ref=aggregate_pk,
            event_type=event_type,
            schema_version=1,
            prior_value=prior,
            new_value=new,
            occurred_at=self.occurred_at,
            actor_pk_ref=self.actor_pk,
            actor_type=self.actor_type,
            reason_code_fk=reason_pk,
            prior_event_hash=prior_hash,
        )
        self.events.append((
            uuid.uuid4(), version, event_type, 1, aggregate_pk, aggregate_type, self.occurred_at,
            self.actor_pk, self.actor_type, _jsonb(prior), _jsonb(new), reason_pk, self.source,
            None, None, _jsonb(metadata), prior_hash, event_hash,
        ))
        return event_hash

    def flush(self, cursor):
        for table, columns, rows in (
            ("core.event", EVENT_COLUMNS, self.events),
            ("ar.payment", PAYMENT_COLUMNS, self.payments),
            ("ar.account", ACCOUNT_COLUMNS, self.accounts),
            ("ar.account_balance_bucket", BUCKET_COLUMNS, self.buckets),
        ):
            if rows:
                with cursor.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
        if self.balances:
            pks, amounts = zip(*self.balances)
            cursor.execute(
                "UPDATE ar.account_balance_bucket b SET balance_minor_units = v.balance"
                " FROM unnest(%s::uuid[], %s::bigint[]) AS v(pk, balance) WHERE b.account_balance_bucket_pk = v.pk",
                [list(pks), list(amounts)],
            )
        if self.versions:
            pks, versions = zip(*self.versions)
            cursor.execute(
                "UPDATE ar.account a SET aggregate_sequence_number = v.version"
                " FROM unnest(%s::uuid[], %s::integer[]) AS v(pk, version) WHERE a.account_pk = v.pk",
                [list(pks), list(versions)],
            )


def _jsonb(value):
    return None if value is None else Jsonb(value)


def _heads(cursor, aggregate_pks):
    """Each aggregate's stream head: its version and its last event's hash."""
    cursor.execute(
        "SELECT DISTINCT ON (aggregate_pk_ref) aggregate_pk_ref, aggregate_sequence_number, event_hash"
        " FROM core.event WHERE aggregate_pk_ref = ANY(%s)"
        " ORDER BY aggregate_pk_ref, aggregate_sequence_number DESC",
        [aggregate_pks],
    )
    return {pk: (version, event_hash) for pk, version, event_hash in cursor.fetchall()}


def _payment_request(command):
    """What was asked, exactly as hivear.handlers.post_payment records it."""
    return {
        "account_pks": sorted(str(pk) for pk in set(command.account_pks)),
        "allocation_method": command.allocation_method,
        "directed_allocations": {str(pk): amount for pk, amount in sorted(command.directed_allocations.items())},
        "bucket_amounts": {str(pk): dict(amounts) for pk, amounts in sorted(command.bucket_amounts.items())},
        "consumer_directed_accounts": sorted(str(pk) for pk in set(command.consumer_directed_accounts)),
        "received_at": command.received_at.isoformat() if command.received_at else None,
    }


def post_payments(commands, *, actor_pk, actor_type, source) -> Outcome:
    """Post a chunk of PostPayment commands, each to one account and with no
    directions, in one transaction, deciding each as post_payment would.

    A version that has moved raises ConcurrencyError, and any other failure
    raises too: the caller then runs the chunk again one command at a time.
    """
    by_account = {}
    for command in commands:
        if (len(command.account_pks) != 1 or command.directed_allocations or command.bucket_amounts
                or command.consumer_directed_accounts):
            raise ValueError("the bulk payment path takes one-account payments without directions")
        (account_pk,) = command.account_pks
        if account_pk in by_account:
            raise ValueError(f"account {account_pk} has two payments in one chunk")
        by_account[account_pk] = command
    # A release the schema has moved past must not write (SHARED-0010).
    release.assert_schema_supports_this_code(max_age_seconds=5)

    outcome = Outcome()
    keys = list(by_account)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT account_pk, aggregate_sequence_number, status, currency_code, is_disputed, list_date"
            " FROM ar.account WHERE account_pk = ANY(%s) ORDER BY account_pk FOR UPDATE",
            [keys],
        )
        accounts = cursor.fetchall()
        cursor.execute(
            "SELECT b.account_fk, d.code, b.account_balance_bucket_pk, d.waterfall_priority, b.balance_minor_units"
            " FROM ar.account_balance_bucket b JOIN ar.balance_bucket_definition d"
            " ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk WHERE b.account_fk = ANY(%s)",
            [keys],
        )
        buckets = {}
        for account_fk, code, bucket_pk, priority, balance in cursor.fetchall():
            buckets.setdefault(account_fk, {})[code] = [bucket_pk, priority, balance]
        # The hashing-off diagnostic skips the head read, as wax.store.append
        # does; the row lock and the unique constraint on each aggregate's
        # version still catch a conflict.
        heads = _heads(cursor, keys) if hashing.HASHING else None
        cursor.execute(
            "SELECT payment_pk FROM ar.payment WHERE payment_pk = ANY(%s)", [[c.payment_pk for c in commands]]
        )
        existing = {row[0] for row in cursor.fetchall()}
        reason_pk = _reason_code_pk("payment_received")
        writes = _Writes(actor_pk, actor_type, source)

        # The single-command path refuses a payment whose account does not exist.
        outcome.rejected += len(by_account) - len(accounts)
        for account_pk, version, status, currency_code, is_disputed, list_date in accounts:
            command = by_account[account_pk]
            if command.payment_pk in existing:
                outcome.skipped += 1
                continue
            if heads is None:
                head_version, head_hash = version, (hashing.PLACEHOLDER_HASH if version else None)
            else:
                head_version, head_hash = heads.get(account_pk, (0, None))
            if head_version != version:
                raise ConcurrencyError(f"account {account_pk}: expected version {version}, found {head_version}")
            # The refusals post_payment makes for a one-account payment with no directions.
            if status != "active" or currency_code != command.currency_code:
                outcome.rejected += 1
                continue

            rows = buckets.get(account_pk, {})
            position = AccountPosition(
                account_pk=account_pk,
                list_date=list_date,
                is_disputed=is_disputed,
                balance_minor_units=sum(row[2] for row in rows.values()),
            )
            shares, unapplied = allocate_across_accounts(
                command.amount_minor_units, [position], command.allocation_method, {}, fixed=frozenset()
            )
            paydown, _ = run_waterfall(shares[account_pk], [(code, row[1], row[2]) for code, row in rows.items()])
            distribution = {str(account_pk): paydown} if paydown else {}
            if sum(paydown.values()) + unapplied != command.amount_minor_units:
                raise RuntimeError(f"Payment {command.payment_pk} does not balance")

            received_at = command.received_at or writes.occurred_at
            writes.event(
                "payment", command.payment_pk, 1, None, "payment.posted", None,
                {
                    "status": "posted",
                    "amount_minor_units": command.amount_minor_units,
                    "currency_code": command.currency_code,
                    "received_at": received_at.isoformat(),
                    "distribution": distribution,
                    "unapplied_minor_units": unapplied,
                    "request": _payment_request(command),
                },
                reason_pk,
            )
            writes.payments.append((
                command.payment_pk, 1, command.amount_minor_units, command.currency_code, "posted",
                received_at, Jsonb(distribution), unapplied,
            ))
            if paydown:
                prior, new = {}, {}
                for code, amount in paydown.items():
                    row = rows[code]
                    prior[code] = row[2]
                    row[2] -= amount
                    new[code] = row[2]
                    writes.balances.append((row[0], row[2]))
                writes.event(
                    "account", account_pk, version + 1, head_hash, "account.payment_applied",
                    {"balance_buckets": prior}, {"balance_buckets": new}, reason_pk,
                    metadata={
                        "payment_pk": str(command.payment_pk),
                        "received_at": received_at.isoformat(),
                        "bucket_amounts": paydown,
                    },
                )
                writes.versions.append((account_pk, version + 1))
            outcome.succeeded += 1
        writes.flush(cursor)
    return outcome


def place_accounts(commands, *, actor_pk, actor_type, source) -> Outcome:
    """Place a chunk of new accounts in one transaction, deciding each as
    place_account would, after passing over any key that already exists."""
    release.assert_schema_supports_this_code(max_age_seconds=5)
    outcome = Outcome()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT account_pk FROM ar.account WHERE account_pk = ANY(%s)", [[c.account_pk for c in commands]]
        )
        existing = {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition")
        definitions = dict(cursor.fetchall())
        reason_pk = _reason_code_pk("account_placed")
        writes = _Writes(actor_pk, actor_type, source)

        for command in commands:
            if command.account_pk in existing:
                outcome.skipped += 1
                continue
            if set(command.opening_balances) - set(definitions):
                outcome.rejected += 1
                continue
            existing.add(command.account_pk)
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
            writes.event("account", command.account_pk, 1, None, "account.placed", None, placed, reason_pk)
            writes.accounts.append((
                command.account_pk, 1, command.client_account_number, command.debt_type, command.contract_type,
                command.currency_code, "active", False, command.list_date, command.itemization_date,
                accrued_through, None,
            ))
            for code, amount in command.opening_balances.items():
                writes.buckets.append((uuid.uuid4(), command.account_pk, definitions[code], amount))
            outcome.succeeded += 1
        writes.flush(cursor)
    return outcome
