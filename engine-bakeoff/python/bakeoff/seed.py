"""The bulk seed loader (WORKLOADS.md, "The seed"). Foundation-written.

Writes, for each account, exactly what PlaceAccount would have written: the
state row, three bucket rows, one account.placed event with a valid hash,
and the account summary read model row. Then it sets every projection's and
the rule runner's position to the head of the stream, so nothing reprocesses
the seed. Rows are loaded with COPY in chunks; the event table's own insert
trigger still assigns each event's sequence number and recorded time.
"""

import sys
import time
from datetime import timedelta

from django.db import connection
from psycopg.types.json import Jsonb

from bakeoff.keys import INTEREST_ACCRUED_THROUGH, SEED_OCCURRED_AT, SEEDER, account_pk, balances, derived_uuid, seed_list_date
from wax import projections, rules, store
from wax.hashing import compute_event_hash

ACCOUNT_COLUMNS = (
    "account_pk, aggregate_sequence_number, client_account_number, debt_type, contract_type, currency_code,"
    " status, is_disputed, list_date, itemization_date, interest_accrued_through, queue_code"
)
BUCKET_COLUMNS = "account_balance_bucket_pk, account_fk, balance_bucket_definition_fk, balance_minor_units"
EVENT_COLUMNS = (
    "event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type,"
    " occurred_at, actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, source,"
    " correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash"
)

READ_MODEL = """
INSERT INTO ar.account_summary_read_model (
    account_summary_read_model_pk, client_account_number, status, is_disputed, currency_code, debt_type,
    contract_type, list_date, balance_minor_units, balance_buckets, last_global_sequence_number)
SELECT e.aggregate_pk_ref, e.new_value->>'client_account_number', e.new_value->>'status',
       (e.new_value->>'is_disputed')::boolean, e.new_value->>'currency_code', e.new_value->>'debt_type',
       e.new_value->>'contract_type', (e.new_value->>'list_date')::date,
       (SELECT sum(value::bigint) FROM jsonb_each_text(e.new_value->'balance_buckets')),
       e.new_value->'balance_buckets', e.global_sequence_number
FROM core.event e
JOIN ar.account a ON a.account_pk = e.aggregate_pk_ref
WHERE e.source = 'seed' AND e.event_type = 'account.placed'
  AND NOT EXISTS (SELECT 1 FROM ar.account_summary_read_model r WHERE r.account_summary_read_model_pk = e.aggregate_pk_ref)
"""


def seed(accounts: int, chunk: int = 50_000) -> dict:
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM ar.account")
        if cursor.fetchone()[0]:
            raise SystemExit("ar.account is not empty; the seed loads only into a freshly migrated database")
        cursor.execute(
            "SELECT v.reference_value_pk FROM core.reference_value v JOIN core.reference_type t"
            " ON t.reference_type_pk = v.reference_type_fk WHERE t.code = 'reason_code' AND v.code = 'account_placed'"
        )
        reason_pk = cursor.fetchone()[0]
        cursor.execute("SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition")
        definitions = dict(cursor.fetchall())
    missing = {"principal", "interest", "fee"} - set(definitions)
    if missing:
        raise SystemExit(f"the bucket registry lacks {sorted(missing)}; run the migrate service first")

    raw = connection.connection
    for start in range(0, accounts, chunk):
        stop = min(start + chunk, accounts)
        accounts_rows, bucket_rows, event_rows = [], [], []
        for i in range(start, stop):
            pk = account_pk(i)
            listed = seed_list_date(i)
            itemized = listed - timedelta(days=30)
            opening = balances(i)
            placed = {
                "client_account_number": f"BK-{i:09d}",
                "debt_type": "personal_loan",
                "contract_type": "third_party_contingency",
                "currency_code": "USD",
                "status": "active",
                "is_disputed": False,
                "list_date": listed.isoformat(),
                "itemization_date": itemized.isoformat(),
                "balance_buckets": opening,
                "interest_accrued_through": INTEREST_ACCRUED_THROUGH.isoformat(),
            }
            event_hash = compute_event_hash(
                aggregate_type="account",
                aggregate_pk_ref=pk,
                event_type="account.placed",
                schema_version=1,
                prior_value=None,
                new_value=placed,
                occurred_at=SEED_OCCURRED_AT,
                actor_pk_ref=SEEDER,
                actor_type="batch",
                reason_code_fk=reason_pk,
                prior_event_hash=None,
            )
            accounts_rows.append((pk, 1, placed["client_account_number"], "personal_loan", "third_party_contingency",
                                  "USD", "active", False, listed, itemized, INTEREST_ACCRUED_THROUGH, None))
            for code, amount in opening.items():
                bucket_rows.append((derived_uuid(f"bakeoff:bucket:{i}:{code}"), pk, definitions[code], amount))
            event_rows.append((derived_uuid(f"bakeoff:seed-event:{i}"), 1, "account.placed", 1, pk, "account",
                               SEED_OCCURRED_AT, SEEDER, "batch", None, Jsonb(placed), reason_pk, "seed",
                               None, None, None, None, event_hash))
        with raw.transaction(), raw.cursor() as cursor:
            for table, columns, rows in (
                ("ar.account", ACCOUNT_COLUMNS, accounts_rows),
                ("ar.account_balance_bucket", BUCKET_COLUMNS, bucket_rows),
                ("core.event", EVENT_COLUMNS, event_rows),
            ):
                with cursor.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
        print(f"[seed] {stop:,} of {accounts:,} accounts", file=sys.stderr, flush=True)

    with raw.transaction(), raw.cursor() as cursor:
        cursor.execute(READ_MODEL)
    head = store.current_position()
    projections.register_checkpoints()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, updated_at = now()"
            " WHERE projection_name = ANY(%s)",
            [head, list(projections._registry)],
        )
        cursor.execute(
            "INSERT INTO core.projection_checkpoint (projection_name, definition_digest, last_global_sequence_number)"
            " VALUES (%s, 'rules', %s) ON CONFLICT (projection_name)"
            " DO UPDATE SET last_global_sequence_number = EXCLUDED.last_global_sequence_number",
            [rules.BASE_POSITION, head],
        )
    return {"workload": "seed", "accounts": accounts, "head": head, "elapsed_s": round(time.perf_counter() - started, 3)}
