"""Bench drivers for the python-direct side (WORKLOADS.md, "Batch workloads").
Foundation-written.

They mirror bakeoff/drivers.py: the same commands, keys, chunks, worker
processes, start barrier, counts, and JSON line, so the two Python sides
report alike. The differences are that each worker holds one psycopg
connection and sends commands through direct.commands.dispatch, and that no
worker imports Django. Chunks are claimed with wax/batch.py's SQL.
"""

import logging
import multiprocessing as mp
import os
import socket
import time

from bakeoff.drivers import COUNTS, MAX_CONFLICT_RETRIES, Tally, _batch_command, merge, percentiles
from bakeoff.keys import BATCH

logger = logging.getLogger("direct.drivers")


def setup():
    """Import the handlers so they register, and make dispatch try once per
    call so the driver counts every retry, as bakeoff.drivers.setup does."""
    from direct import commands, handlers  # noqa: F401

    commands.MAX_ATTEMPTS = 1


def send(session, command, actor_pk, actor_type, source, tally):
    from direct.commands import CommandRejected, dispatch
    from direct.store import ConcurrencyError

    tally.counts["sent"] += 1
    started = time.perf_counter()
    attempts = 0
    while True:
        try:
            dispatch(session, command, actor_pk=actor_pk, actor_type=actor_type, source=source)
            outcome = "succeeded"
        except CommandRejected:
            outcome = "rejected"
        except ConcurrencyError:
            if attempts < MAX_CONFLICT_RETRIES:
                attempts += 1
                tally.counts["retried"] += 1
                continue
            outcome = "failed"
        except Exception:
            logger.exception("%s failed", type(command).__name__)
            outcome = "failed"
        break
    tally.counts[outcome] += 1
    if outcome == "succeeded":
        tally.counts["transactions"] += 1
    tally.latencies.append(time.perf_counter() - started)


# wax/batch.py's claiming, on a psycopg connection.

def create_run(conn, job_name, chunk_size=1000):
    import uuid

    run_pk = uuid.uuid4()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO core.batch_job_run (batch_job_run_pk, job_name) VALUES (%s, %s)", [run_pk, job_name])
        cur.execute(
            """
            WITH numbered AS (
                SELECT account_pk AS k, (row_number() OVER (ORDER BY account_pk) - 1) / %s AS n FROM ar.account
            )
            INSERT INTO core.batch_job_chunk (batch_job_run_fk, chunk_sequence_number, range_start, range_end, status)
            SELECT %s, first.n, first.k, last.k, 'pending'
            FROM (SELECT DISTINCT ON (n) n, k FROM numbered ORDER BY n, k) first
            JOIN (SELECT DISTINCT ON (n) n, k FROM numbered ORDER BY n, k DESC) last USING (n)
            """,
            [chunk_size, run_pk],
        )
        cur.execute("SELECT count(*) FROM core.batch_job_chunk WHERE batch_job_run_fk = %s", [run_pk])
        return run_pk, cur.fetchone()[0]


def claim_chunk(conn, run_pk, instance, lease_seconds=600):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT batch_job_chunk_pk, range_start, range_end FROM core.batch_job_chunk"
            " WHERE batch_job_run_fk = %s"
            " AND (status = 'pending' OR (status = 'claimed' AND claim_expires_at < clock_timestamp()))"
            " ORDER BY chunk_sequence_number LIMIT 1 FOR UPDATE SKIP LOCKED",
            [run_pk],
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE core.batch_job_chunk SET status = 'claimed', claimed_by_instance = %s,"
            " claim_expires_at = clock_timestamp() + make_interval(secs => %s) WHERE batch_job_chunk_pk = %s",
            [instance, lease_seconds, row[0]],
        )
    return row


def chunk_keys(conn, range_start, range_end):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT account_pk FROM ar.account WHERE account_pk BETWEEN %s AND %s ORDER BY account_pk",
            [range_start, range_end],
        )
        return [row[0] for row in cur.fetchall()]


def end_chunk(conn, chunk_pk, failed_detail=None):
    with conn.cursor() as cur:
        if failed_detail is None:
            cur.execute(
                "UPDATE core.batch_job_chunk SET status = 'complete', completed_at = clock_timestamp()"
                " WHERE batch_job_chunk_pk = %s",
                [chunk_pk],
            )
        else:
            cur.execute(
                "UPDATE core.batch_job_chunk SET status = 'failed', error_detail = %s WHERE batch_job_chunk_pk = %s",
                [str(failed_detail)[:2000], chunk_pk],
            )


def finish_run(conn, run_pk):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.batch_job_run SET finished_at = clock_timestamp() WHERE batch_job_run_pk = %s"
            " AND NOT EXISTS (SELECT 1 FROM core.batch_job_chunk WHERE batch_job_run_fk = %s AND status <> 'complete')",
            [run_pk, run_pk],
        )
        return cur.rowcount == 1


def _batch_worker(workload, run_pk, barrier, results):
    setup()
    from direct.commands import Session
    from direct.db import connect

    session = Session(connect())
    instance = f"{socket.gethostname()}:{os.getpid()}"
    tally = Tally()
    chunks = failed_chunks = 0
    barrier.wait()
    while (claim := claim_chunk(session.conn, run_pk, instance)) is not None:
        chunk_pk, range_start, range_end = claim
        failed_before = tally.counts["failed"]
        for key in chunk_keys(session.conn, range_start, range_end):
            send(session, _batch_command(workload, run_pk, key), BATCH, "batch", "batch", tally)
        if tally.counts["failed"] > failed_before:
            end_chunk(session.conn, chunk_pk, f"{tally.counts['failed'] - failed_before} commands failed")
            failed_chunks += 1
        else:
            end_chunk(session.conn, chunk_pk)
        chunks += 1
    results.put({**tally.as_dict(), "chunks": chunks, "failed_chunks": failed_chunks})


def _realtime_worker(w, run_pk, seed_accounts, random_seed, warmup, duration, barrier, results):
    """bakeoff.drivers._realtime_worker's mix, generator, and keys exactly,
    sent through the direct path. Kept as a copy so the Django side's driver is
    unchanged."""
    import random

    setup()
    from bakeoff.drivers import LIVE_INTEREST_ACCRUED_THROUGH, LIVE_ITEMIZATION_DATE, LIVE_LIST_DATE
    from bakeoff.keys import INBOUND, account_pk, balances, derived_uuid
    from hivear.commands import OpenDispute, PlaceAccount, PostPayment
    from direct.commands import Session
    from direct.db import connect

    session = Session(connect())
    rng = random.Random(f"{random_seed}:{w}")
    counters = {"payment": 0, "dispute": 0, "account": 0}
    measured = Tally()
    warmup_sent = 0
    barrier.wait()
    begin = time.monotonic()
    warm_end, end = begin + warmup, begin + warmup + duration
    while (now := time.monotonic()) < end:
        if now >= warm_end:
            tally = measured
        else:
            tally = Tally()
            warmup_sent += 1
        draw = rng.random()
        if draw < 0.85:
            i = rng.randrange(seed_accounts)
            payoff = rng.randrange(30) == 0
            n = counters["payment"]
            counters["payment"] += 1
            command = PostPayment(
                payment_pk=derived_uuid(f"bakeoff:live-payment:{run_pk}:{w}:{n}"),
                account_pks=[account_pk(i)],
                amount_minor_units=2_000_000 if payoff else rng.randint(500, 5000),
                currency_code="USD",
            )
        elif draw < 0.95:
            i = rng.randrange(seed_accounts)
            n = counters["dispute"]
            counters["dispute"] += 1
            command = OpenDispute(account_pk=account_pk(i), dispute_pk=derived_uuid(f"bakeoff:live-dispute:{run_pk}:{w}:{n}"))
        else:
            n = counters["account"]
            counters["account"] += 1
            command = PlaceAccount(
                account_pk=derived_uuid(f"bakeoff:live-account:{run_pk}:{w}:{n}"),
                client_account_number=f"LV-{w}-{n}",
                debt_type="personal_loan",
                contract_type="third_party_contingency",
                currency_code="USD",
                list_date=LIVE_LIST_DATE,
                itemization_date=LIVE_ITEMIZATION_DATE,
                interest_accrued_through=LIVE_INTEREST_ACCRUED_THROUGH,
                opening_balances=balances(n),
            )
        send(session, command, INBOUND, "integration", "inbound", tally)
    results.put({**measured.as_dict(), "warmup_sent": warmup_sent})


def run_realtime(workers, warmup, duration, random_seed):
    from datetime import UTC, datetime

    from bakeoff.keys import derived_uuid
    from direct.db import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ar.account WHERE client_account_number LIKE %s", ["BK-%"])
        seed_accounts = cur.fetchone()[0]
    if not seed_accounts:
        raise SystemExit("no seed accounts; run seed first")
    start = datetime.now(UTC).isoformat()
    run_pk = derived_uuid(f"bakeoff:realtime:{random_seed}:{start}")
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(workers + 1)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_realtime_worker, args=(w, run_pk, seed_accounts, random_seed, warmup, duration, barrier, results))
        for w in range(workers)
    ]
    for proc in procs:
        proc.start()
    barrier.wait()
    measured_from = time.perf_counter() + warmup
    outputs = [results.get() for _ in procs]
    elapsed = time.perf_counter() - measured_from
    for proc in procs:
        proc.join()
    totals, latencies = merge(outputs)
    return {
        "workload": "realtime",
        "workers": workers,
        "run_pk": str(run_pk),
        **totals,
        "sent_total": totals["sent"] + sum(out["warmup_sent"] for out in outputs),
        "elapsed_s": round(elapsed, 3),
        "latency_ms": percentiles(latencies),
        "warmup_s": warmup,
        "duration_s": duration,
        "random_seed": random_seed,
        "seed_accounts": seed_accounts,
        "started_at": start,
        "path": "direct",
    }


def run_batch(workload, workers):
    from direct.db import connect

    conn = connect()
    coordinator_started = time.perf_counter()
    run_pk, chunk_count = create_run(conn, workload)
    coordinator_s = time.perf_counter() - coordinator_started
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(workers + 1)
    results = ctx.Queue()
    procs = [ctx.Process(target=_batch_worker, args=(workload, run_pk, barrier, results)) for _ in range(workers)]
    for proc in procs:
        proc.start()
    barrier.wait()
    started = time.perf_counter()
    outputs = [results.get() for _ in procs]
    elapsed = time.perf_counter() - started
    for proc in procs:
        proc.join()
    totals, latencies = merge(outputs)
    assert set(totals) == set(COUNTS)
    return {
        "workload": workload,
        "workers": workers,
        "run_pk": str(run_pk),
        **totals,
        "sent_total": totals["sent"],
        "elapsed_s": round(elapsed, 3),
        "latency_ms": percentiles(latencies),
        "chunks": chunk_count,
        "failed_chunks": sum(out["failed_chunks"] for out in outputs),
        "coordinator_s": round(coordinator_s, 3),
        "run_complete": finish_run(conn, run_pk),
        "path": "direct",
    }
