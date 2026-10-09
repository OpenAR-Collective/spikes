"""The bench drivers (WORKLOADS.md, "Batch workloads" and "Real-time
workload"). Foundation-written.

Concurrency uses worker processes, the usual way for Python to use more than
one core. Each worker sets up Django and connects before a start barrier, so
start-up is outside the timed region. Commands go through the prototype's
own command path, wax.commands.dispatch. The driver makes dispatch try once
per call and retries a version conflict itself, so that every retry is
counted, as the specification's "retried" key requires; the behavior is the
same as dispatch's own retry loop.
"""

import logging
import multiprocessing as mp
import os
import random
import socket
import time
from datetime import UTC, date, datetime

from bakeoff.keys import BATCH, INBOUND, account_pk, balances, derived_uuid

logger = logging.getLogger("bakeoff.drivers")
MAX_CONFLICT_RETRIES = 10


def setup(count_retries=True):
    import django

    django.setup()
    if count_retries:
        from wax import commands as wax_commands

        wax_commands.MAX_ATTEMPTS = 1


class Tally:
    def __init__(self):
        self.counts = {"sent": 0, "succeeded": 0, "rejected": 0, "failed": 0, "retried": 0}
        self.latencies = []

    def as_dict(self):
        return {**self.counts, "latencies": self.latencies}


def send(command, actor_pk, actor_type, source, tally):
    from wax.commands import CommandRejected, dispatch
    from wax.store import ConcurrencyError

    tally.counts["sent"] += 1
    started = time.perf_counter()
    attempts = 0
    while True:
        try:
            dispatch(command, actor_pk=actor_pk, actor_type=actor_type, source=source)
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
    tally.latencies.append(time.perf_counter() - started)


def merge(outputs):
    totals = {"sent": 0, "succeeded": 0, "rejected": 0, "failed": 0, "retried": 0}
    latencies = []
    for out in outputs:
        for key in totals:
            totals[key] += out[key]
        latencies.extend(out["latencies"])
    return totals, latencies


def percentiles(values):
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def pick(q):
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000, 2)

    return {"p50": pick(0.50), "p95": pick(0.95), "p99": pick(0.99), "max": round(ordered[-1] * 1000, 2)}


# Batch workloads.

PAYMENT_RECEIVED_AT = datetime(2026, 10, 1, 6, tzinfo=UTC)
ACCRUAL_DATE = date(2026, 10, 1)


def _batch_command(workload, run_pk, key):
    from hivear.commands import AccrueInterest, PostPayment

    if workload == "batch-payments":
        return PostPayment(
            payment_pk=derived_uuid(f"bakeoff:payment:{run_pk}:{key}"),
            account_pks=[key],
            amount_minor_units=2500,
            currency_code="USD",
            received_at=PAYMENT_RECEIVED_AT,
            allocation_method="oldest_first",
        )
    return AccrueInterest(account_pk=key, accrual_date=ACCRUAL_DATE, annual_rate_basis_points=1800)


def _batch_worker(workload, run_pk, barrier, results):
    setup()
    from django.db import connection

    from wax import batch

    connection.ensure_connection()
    instance = f"{socket.gethostname()}:{os.getpid()}"
    tally = Tally()
    chunks = failed_chunks = 0
    barrier.wait()
    while (claim := batch.claim_chunk(run_pk, instance)) is not None:
        chunk_pk, range_start, range_end = claim
        failed_before = tally.counts["failed"]
        for key in batch.chunk_keys("ar.account", "account_pk", range_start, range_end):
            send(_batch_command(workload, run_pk, key), BATCH, "batch", "batch", tally)
        if tally.counts["failed"] > failed_before:
            batch.fail_chunk(chunk_pk, f"{tally.counts['failed'] - failed_before} commands failed")
            failed_chunks += 1
        else:
            batch.complete_chunk(chunk_pk)
        chunks += 1
    results.put({**tally.as_dict(), "chunks": chunks, "failed_chunks": failed_chunks})


def run_batch(workload, workers):
    setup()
    from wax import batch

    coordinator_started = time.perf_counter()
    run_pk, chunk_count = batch.create_run(workload, "ar.account", "account_pk")
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
    return {
        "workload": workload,
        "workers": workers,
        "run_pk": str(run_pk),
        **totals,
        "elapsed_s": round(elapsed, 3),
        "latency_ms": percentiles(latencies),
        "chunks": chunk_count,
        "failed_chunks": sum(out["failed_chunks"] for out in outputs),
        "coordinator_s": round(coordinator_s, 3),
        "run_complete": batch.finish_run(run_pk),
    }


# Real-time workload.

LIVE_LIST_DATE = date(2026, 9, 1)
LIVE_ITEMIZATION_DATE = date(2026, 8, 2)
LIVE_INTEREST_ACCRUED_THROUGH = date(2026, 9, 30)


def _realtime_worker(w, run_pk, seed_accounts, random_seed, warmup, duration, barrier, results):
    setup()
    from django.db import connection

    from hivear.commands import OpenDispute, PlaceAccount, PostPayment

    connection.ensure_connection()
    rng = random.Random(f"{random_seed}:{w}")
    counters = {"payment": 0, "dispute": 0, "account": 0}
    measured = Tally()
    barrier.wait()
    begin = time.monotonic()
    warm_end, end = begin + warmup, begin + warmup + duration
    while (now := time.monotonic()) < end:
        tally = measured if now >= warm_end else Tally()
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
        send(command, INBOUND, "integration", "inbound", tally)
    results.put(measured.as_dict())


def run_realtime(workers, warmup, duration, random_seed):
    setup()
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM ar.account WHERE client_account_number LIKE %s", ["BK-%"])
        seed_accounts = cursor.fetchone()[0]
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
        "elapsed_s": round(elapsed, 3),
        "latency_ms": percentiles(latencies),
        "warmup_s": warmup,
        "duration_s": duration,
        "random_seed": random_seed,
        "seed_accounts": seed_accounts,
        "started_at": start,
    }
