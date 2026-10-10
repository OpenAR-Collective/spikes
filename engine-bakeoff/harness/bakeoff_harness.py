"""The engine bake-off harness. Standard library only; runs on the host.

  python bakeoff_harness.py selftest --side python
  python bakeoff_harness.py prepare  --side python --accounts 500000
  python bakeoff_harness.py run      --side python --workload batch-payments --workers 1,2,4,8 --accounts 500000
  python bakeoff_harness.py run      --side csharp --workload realtime --workers 8 --accounts 500000 \
                                     --warmup 15 --duration 120 --random-seed 1 --engine-args "--rule-workers 4"
  python bakeoff_harness.py run      --side python --workload placement-bulk --workers 4 --accounts 500000 \
                                     --placements 100000
  python bakeoff_harness.py teardown --side python

It drives either side only through the commands in WORKLOADS.md, so it knows
nothing about either engine beyond sides.json. Everything it reports is
measured from outside the process under test:

- CPU from each container's cgroup counter (cpu.stat usage_usec), read
  before and after the bench command, for the database, the engine, and the
  bench driver, and again for the engine once it has caught up. Per-command
  figures divide by the bench JSON's sent_total, every command sent with
  the warm-up's included, and a run without it fails.
- Statements and commits from PostgreSQL's pg_stat_statements and
  pg_stat_database, for the run's database only.
- Read-model and rule lag from core.bakeoff_progress, sampled every second,
  and catch-up: the time until every reader reaches the head recorded when
  the bench command ended.
- Rule latency from core.bakeoff_event_log and register lateness from
  core.pending_event.
- The correctness gates, in SQL over the tables both sides share, plus the
  side's own verify-chain.

Three diagnostics change what a run does, and run.md says so: --no-engine
runs the bench without the engine, so nothing reads the stream, and the
read-model and rule gates are skipped; --hash off sets WAX_HASH=off on the
engine and bench containers, so events carry a placeholder hash, and
verify-chain and the chain gate are skipped; and every run records the
pg_stat_statements breakdown of its top statements by total time.

Each run gets a fresh copy of the seeded template database, made with CREATE
DATABASE ... TEMPLATE. Development output goes to harness/out/; --record
writes to ../results/ and is for runs Rob has approved.
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
BAKEOFF = HARNESS.parent
OVERRIDE = HARNESS / "compose.override.yml"
# The caps profiles. Each is the Compose file applied on top of a side's own,
# setting the PostgreSQL settings and every service's CPU and memory limits.
# The scaled profile is a complete file used in place of the standard one, not
# layered on it.
CAPS = {"standard": OVERRIDE, "scaled": HARNESS / "compose.scale.yml"}
PGBOUNCER = HARNESS / "compose.pgbouncer.yml"
SIDES = json.loads((HARNESS / "sides.json").read_text(encoding="utf-8"))
WORKLOADS = ["batch-payments", "batch-accrual", "batch-payments-bulk", "placement", "placement-bulk", "realtime"]
PLACEMENTS = ("placement", "placement-bulk")
OWNER_ENV = ["-e", "DATABASE_USER=hivear", "-e", "DATABASE_PASSWORD=local-evaluation-only",
             "-e", "DATABASE_HOST=db", "-e", "DATABASE_PORT=5432", "-e", "DATABASE_POOLED=0"]

# WORKLOADS.md's derived_uuid, in SQL, so the gates can recompute keys. It is
# declared IMMUTABLE, which it is, so the planner computes it once per row
# and can use the key's index; a volatile function would be recomputed for
# every row it is compared against.
DERIVED_UUID_SQL = (
    "CREATE FUNCTION pg_temp.derived_uuid(name text) RETURNS uuid LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$"
    " SELECT encode(set_byte(set_byte(b, 6, (get_byte(b, 6) & 15) | 64), 8, (get_byte(b, 8) & 63) | 128), 'hex')::uuid"
    " FROM (SELECT substring(sha256(convert_to(name, 'UTF8')) FROM 1 FOR 16) AS b) x $$;"
)
# Seed formulas from WORKLOADS.md, keyed by i.
OPENING_SQL = ("(50000 + ({i} * 7919) % 950000) + (100 + ({i} * 104729) % 20000) + (500 + ({i} % 3) * 1000)")
INDEX_SQL = "substring(a.client_account_number FROM '([0-9]+)$')::bigint"


def log(message):
    print(f"[harness {datetime.now():%H:%M:%S}] {message}", flush=True)


def sh(cmd, *, env=None, check=True, timeout=None):
    result = subprocess.run(cmd, env={**os.environ, **(env or {})}, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr[-3000:]}")
    return result


def last_json(text):
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


class Side:
    def __init__(self, name, pgbouncer=False, caps="standard"):
        spec = SIDES[name]
        if caps not in CAPS:
            raise ValueError(f"unknown caps profile {caps!r}; the profiles are {sorted(CAPS)}")
        if not CAPS[caps].exists():
            raise SystemExit(f"{CAPS[caps].name} does not exist, and the {caps} caps profile needs it")
        self.caps = caps
        self.caps_file = CAPS[caps]
        self.name = name
        self.compose = (HARNESS / spec["compose"]).resolve()
        self.project = spec["project"]
        self.profile = spec["profile"]
        self.cli = spec["cli"]
        # compose_env feeds the side's Compose interpolation; container_env is
        # set on every container the harness starts, in case a side reads it
        # directly. Both are recorded with every result.
        self.compose_env = spec.get("compose_env", {})
        self.container_env = spec.get("container_env", {})
        # Workloads this side does not run, such as the bulk workloads on the
        # Marten side, whose writer is the direct side's (WORKLOADS.md).
        self.workloads_excluded = spec.get("workloads_excluded", [])
        self.pgbouncer = pgbouncer

    def env_flags(self):
        return [flag for key, value in self.container_env.items() for flag in ("-e", f"{key}={value}")]

    def files(self):
        files = ["-f", str(self.compose), "-f", str(self.caps_file)]
        return files + (["-f", str(PGBOUNCER)] if self.pgbouncer else [])

    def dc(self, *args, database=None, check=True, timeout=None):
        env = {**self.compose_env, **({"BAKEOFF_DATABASE": database} if database else {})}
        return sh(["docker", "compose", "-p", self.project, *self.files(), "--profile", self.profile, *args],
                  env=env, check=check, timeout=timeout)

    def container(self, service):
        return self.dc("ps", "-q", service).stdout.strip()

    def psql(self, database, sql, check=True):
        result = sh(["docker", "exec", self.container("db"), "psql", "-U", "hivear", "-d", database,
                     "-AtX", "-v", "ON_ERROR_STOP=1", "-c", sql], check=check)
        return result.stdout.strip() if result.returncode == 0 else None

    def up_db(self):
        self.dc("up", "-d", "db", *(["pgbouncer"] if self.pgbouncer else []))
        for _ in range(120):
            cid = self.container("db")
            if cid and sh(["docker", "inspect", "-f", "{{.State.Health.Status}}", cid], check=False).stdout.strip() == "healthy":
                return
            time.sleep(1)
        raise RuntimeError("the database did not become healthy")

    def oneshot(self, database, args, owner=False, timeout=None):
        """Run a side command once in the bench service and return its JSON."""
        result = self.dc("run", "--rm", "-T", "-e", f"DATABASE_NAME={database}", *self.env_flags(),
                         *(OWNER_ENV if owner else []), "--entrypoint", self.cli[0], "bench", *self.cli[1:], *args,
                         database=database, check=False, timeout=timeout)
        return result, last_json(result.stdout)

    def start(self, service, database, name, entrypoint, args):
        sh(["docker", "rm", "-f", name], check=False)
        self.dc("run", "-d", "--name", name, "-e", f"DATABASE_NAME={database}", *self.env_flags(),
                "--entrypoint", entrypoint, service, *args, database=database)
        return name


def cpu_stat(container):
    """The container's cgroup cpu.stat, every counter in it, as integers."""
    out = sh(["docker", "exec", container, "cat", "/sys/fs/cgroup/cpu.stat"], check=False).stdout
    stat = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stat[parts[0]] = int(parts[1])
    return stat


def cpu_usec(container):
    return cpu_stat(container).get("usage_usec")


def throttling(before, after):
    """Each container's CPU throttling over the bench command: of its scheduler
    periods, how many hit the container's CPU limit, and how long it was held
    back. A container throttled for much of the run was capped by its limit."""
    result = {}
    for name, start in before["cpu_stat"].items():
        end = after["cpu_stat"].get(name, {})
        periods = end.get("nr_periods", 0) - start.get("nr_periods", 0)
        throttled = end.get("nr_throttled", 0) - start.get("nr_throttled", 0)
        result[name] = {
            "nr_periods": periods,
            "nr_throttled": throttled,
            "throttled_s": round((end.get("throttled_usec", 0) - start.get("throttled_usec", 0)) / 1e6, 3),
            "throttled_pct": round(100 * throttled / periods, 1) if periods else None,
        }
    return result


# A fixed CPU-bound statement, timed five times in the run's database just
# before the bench. generate_series sits in the select list so its rows stream
# rather than filling a tuplestore that could spill to disk, and JIT is off so
# compilation adds nothing; the probe measures one core's speed and nothing else.
HOST_PROBE_SQL = (
    "SET jit = off; "
    "CREATE FUNCTION pg_temp.host_probe(n int) RETURNS SETOF double precision LANGUAGE plpgsql AS $$ "
    "DECLARE started timestamptz; total bigint; "
    "BEGIN FOR k IN 1..n LOOP started := clock_timestamp(); "
    "SELECT sum(x) INTO total FROM (SELECT generate_series(1, 20000000) AS x) s; "
    "RETURN NEXT extract(epoch FROM clock_timestamp() - started) * 1000; END LOOP; END $$; "
    "SELECT round(ms::numeric, 1) FROM pg_temp.host_probe(5) AS ms;"
)


def host_probe(side, database):
    """Returns (median ms, every sample). A median well above the night's usual
    marks a run during which the machine itself ran slow."""
    samples = []
    for line in (side.psql(database, HOST_PROBE_SQL, check=False) or "").splitlines():
        try:
            samples.append(float(line))
        except ValueError:
            continue  # the SET and CREATE FUNCTION status lines
    return (round(statistics.median(samples), 1) if samples else None), samples


def running(container):
    return sh(["docker", "inspect", "-f", "{{.State.Running}}", container], check=False).stdout.strip() == "true"


def limits(container):
    out = sh(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}", container], check=False).stdout.split()
    return {"cpus": int(out[0]) / 1e9, "mem_bytes": int(out[1])} if len(out) == 2 else None


def template_name(accounts):
    return f"bakeoff_seed_{accounts}"


# ---------------------------------------------------------------- selftest

def cmd_selftest(args):
    side = Side(args.side, args.pgbouncer, caps=args.caps)
    side.dc("config", "--quiet")
    log("compose files merge cleanly")
    side.up_db()
    preload = side.psql("postgres", "SHOW shared_preload_libraries")
    log(f"shared_preload_libraries = {preload}")
    vectors = json.loads((BAKEOFF / "shared" / "derived-uuids.json").read_text(encoding="utf-8"))
    bad = 0
    for vector in vectors:
        name = vector["name"].replace("'", "''")
        got = side.psql("postgres", f"{DERIVED_UUID_SQL} SELECT pg_temp.derived_uuid('{name}');").splitlines()[-1]
        if got != vector["uuid"]:
            bad += 1
            log(f"MISMATCH {vector['name']!r}: SQL {got}, expected {vector['uuid']}")
    log(f"derived_uuid in SQL matches {len(vectors) - bad} of {len(vectors)} shared vectors")
    cpu = cpu_usec(side.container("db"))
    log(f"cgroup CPU readable from the db container: {cpu is not None}")
    lim = limits(side.container("db"))
    log(f"db container limits: {lim}")
    if bad or cpu is None:
        raise SystemExit(1)


# ---------------------------------------------------------------- prepare

def cmd_prepare(args):
    side = Side(args.side, args.pgbouncer, caps=args.caps)
    template = template_name(args.accounts)
    log(f"{side.name}: building the image")
    side.dc("build", "migrate", timeout=3600)
    side.up_db()
    log(f"{side.name}: creating {template}")
    side.psql("postgres", f"DROP DATABASE IF EXISTS {template} WITH (FORCE)")
    side.psql("postgres", f"CREATE DATABASE {template}")
    result = side.dc("run", "--rm", "-T", "-e", f"DATABASE_NAME={template}", *side.env_flags(), "migrate",
                     database=template, check=False)
    if result.returncode != 0:
        raise SystemExit(f"migrate failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    log(f"{side.name}: seeding {args.accounts:,} accounts (not measured)")
    started = time.perf_counter()
    result, seeded = side.oneshot(template, ["seed", "--accounts", str(args.accounts)], owner=True, timeout=6 * 3600)
    if result.returncode != 0 or seeded is None:
        raise SystemExit(f"seed failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    seed_s = time.perf_counter() - started
    side.psql(template, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    side.psql(template, "VACUUM ANALYZE")
    info = {
        "side": side.name,
        "template": template,
        "accounts": int(side.psql(template, "SELECT count(*) FROM ar.account")),
        "events": int(side.psql(template, "SELECT count(*) FROM core.bakeoff_event_log")),
        "database_bytes": int(side.psql(template, "SELECT pg_database_size(current_database())")),
        "seed_s": round(seed_s, 1),
        "seed_output": seeded,
        "prepared": datetime.now().isoformat(timespec="seconds"),
    }
    out = HARNESS / "out" / "templates"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{side.name}-{args.accounts}.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
    log(f"{side.name}: {template} ready: {info['accounts']:,} accounts, {info['events']:,} events,"
        f" {info['database_bytes'] / 2**30:.2f} GiB, seeded in {seed_s:.0f} s")


# ---------------------------------------------------------------- run

class LagSampler(threading.Thread):
    """Samples how far the slowest reader trails the head of the stream."""

    def __init__(self, side, database, interval=1.0):
        super().__init__(daemon=True)
        self.side, self.database, self.interval = side, database, interval
        self.samples, self.available = [], True
        self._stop = threading.Event()

    def lag(self):
        out = self.side.psql(self.database, "SELECT coalesce(max(head) - min(position), 0), max(head) FROM core.bakeoff_progress",
                             check=False)
        if not out:
            self.available = False
            return None, None
        lag, head = out.split("|")
        return int(lag), int(head or 0)

    def positions(self):
        """(slowest reader's position, current head), or (None, None)."""
        out = self.side.psql(self.database, "SELECT min(position), max(head) FROM core.bakeoff_progress", check=False)
        if not out or "|" not in out or out.startswith("|"):
            return None, None
        low, head = out.split("|")
        return int(low), int(head or 0)

    def run(self):
        started = time.perf_counter()
        while not self._stop.is_set():
            lag, _ = self.lag()
            if lag is not None:
                self.samples.append((round(time.perf_counter() - started, 1), lag))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(10)


def kernel_cpu_ticks(container):
    """The aggregate cpu line of /proc/stat as seen from a container: user, nice,
    system, idle, iowait, irq, softirq, and steal, in clock ticks."""
    out = sh(["docker", "exec", container, "cat", "/proc/stat"], check=False).stdout
    for line in out.splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:9]]
            return dict(zip(("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"), values))
    return None


def steal(before, after):
    """Over the bench command: the share of the kernel's CPU time stolen by the
    hypervisor for other tenants, and the share spent busy at all."""
    a, b = before.get("kernel_ticks"), after.get("kernel_ticks")
    if not a or not b:
        return {"steal_pct": None, "busy_pct": None}
    delta = {k: b[k] - a[k] for k in a}
    total = sum(delta.values())
    if total <= 0:
        return {"steal_pct": None, "busy_pct": None}
    return {"steal_pct": round(100 * delta["steal"] / total, 2),
            "busy_pct": round(100 * (total - delta["idle"] - delta["iowait"]) / total, 1)}


def wait_for_engine(engine, started, timeout, idle_cores=0.5, window=2.0):
    """Waits until the engine has finished starting, judged from outside by its
    CPU: two windows in a row in which it used less than idle_cores. A Python
    engine spends about half a minute at its CPU limit while its processes
    import Django; a run that starts sooner measures that start-up, and the
    host probe competes with it. Returns (seconds from the engine's start,
    cores in the last window), or (None, cores) if it never settled."""
    quiet, cores = 0, None
    while time.perf_counter() - started < timeout:
        first = cpu_usec(engine)
        time.sleep(window)
        second = cpu_usec(engine)
        if first is None or second is None:
            return None, None
        cores = round((second - first) / 1e6 / window, 2)
        quiet = quiet + 1 if cores < idle_cores else 0
        if quiet >= 2:
            return round(time.perf_counter() - started, 1), cores
    return None, cores


def _json_rows(side, database, sql):
    text = side.psql(database, f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t", check=False)
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def db_activity(side, database):
    """The database's write-side counters: pg_stat_wal and pg_stat_checkpointer
    are cluster-wide, which is the run's database alone while a run is going;
    pg_stat_bgwriter likewise; pg_stat_user_tables is this database's. Each
    view is read on its own, so one a PostgreSQL version lacks is just null."""
    def one(view):
        rows = _json_rows(side, database, f"SELECT * FROM {view}")
        return rows[0] if rows else None

    tables = _json_rows(side, database, "SELECT schemaname || '.' || relname AS name, n_tup_ins, n_tup_upd,"
                        " n_tup_hot_upd, n_tup_del, n_dead_tup, autovacuum_count, autoanalyze_count"
                        " FROM pg_stat_user_tables")
    return {"wal": one("pg_stat_wal"), "checkpointer": one("pg_stat_checkpointer"), "bgwriter": one("pg_stat_bgwriter"),
            "tables": {row.pop("name"): row for row in tables or []}}


def _numeric_delta(start, end):
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    return {key: end[key] - start[key] for key in end
            if isinstance(end.get(key), (int, float)) and isinstance(start.get(key), (int, float))}


def activity_delta(before, after):
    """Write activity over the bench command. Per table, only tables that
    changed, with n_dead_tup as it stood at the end."""
    a, b = before.get("db_activity") or {}, after.get("db_activity") or {}
    tables = {}
    for name, end in (b.get("tables") or {}).items():
        delta = _numeric_delta((a.get("tables") or {}).get(name, {}), end) or {}
        if any(v for k, v in delta.items() if k != "n_dead_tup"):
            delta["n_dead_tup"] = end.get("n_dead_tup")
            tables[name] = delta
    return {"wal": _numeric_delta(a.get("wal"), b.get("wal")),
            "checkpointer": _numeric_delta(a.get("checkpointer"), b.get("checkpointer")),
            "bgwriter": _numeric_delta(a.get("bgwriter"), b.get("bgwriter")),
            "tables": dict(sorted(tables.items(), key=lambda item: -item[1].get("n_tup_upd", 0)))}


def counters(side, database, containers):
    stats = side.psql(database, "SELECT xact_commit, xact_rollback FROM pg_stat_database WHERE datname = current_database()")
    commit, rollback = (int(x) for x in stats.split("|"))
    stats_by_container = {k: cpu_stat(v) for k, v in containers.items()}
    return {"cpu": {k: stat.get("usage_usec") for k, stat in stats_by_container.items()},
            "cpu_stat": stats_by_container, "kernel_ticks": kernel_cpu_ticks(containers["db"]),
            "db_activity": db_activity(side, database), "commits": commit, "rollbacks": rollback,
            "clock": side.psql(database, "SELECT clock_timestamp()"), "t": time.perf_counter()}


def percentile_row(text):
    if not text:
        return None
    parts = text.split("|")
    return {"count": int(parts[0]), **{k: (round(float(v), 1) if v else None) for k, v in zip(("p50", "p95", "p99", "max"), parts[1:])}}


def measurements(side, database, start_clock):
    rules = {}
    rows = side.psql(database, f"""
        SELECT coalesce(event_type, 'all'), count(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY ms),
               percentile_cont(0.99) WITHIN GROUP (ORDER BY ms), max(ms)
        FROM (SELECT a.event_type, extract(epoch FROM a.recorded_at - t.recorded_at) * 1000 AS ms
              FROM core.bakeoff_event_log a JOIN core.bakeoff_event_log t ON t.event_key = a.correlation_pk_ref
              WHERE a.correlation_type = 'rule' AND a.recorded_at >= '{start_clock}') q
        GROUP BY ROLLUP (event_type)""", check=False) or ""
    for line in rows.splitlines():
        name, rest = line.split("|", 1)
        rules[name] = percentile_row(rest)
    lateness = percentile_row(side.psql(database, f"""
        SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY ms), percentile_cont(0.95) WITHIN GROUP (ORDER BY ms),
               percentile_cont(0.99) WITHIN GROUP (ORDER BY ms), max(ms)
        FROM (SELECT extract(epoch FROM fired_at - due_at) * 1000 AS ms FROM core.pending_event
              WHERE status = 'fired' AND fired_at >= '{start_clock}') q""", check=False))
    events = side.psql(database, f"SELECT count(*) FROM core.bakeoff_event_log WHERE recorded_at >= '{start_clock}'", check=False)
    return {"rule_latency_ms": rules, "register_lateness_ms": lateness, "events_written": int(events or 0)}


def gates(side, database, workload, bench, before_total, accounts, placements, end_clock, verify,
          engine_paused=False, hash_off=False):
    """Returns (checks, skipped): the gates that ran, and the names of those a
    diagnostic made meaningless, which run.md lists."""
    checks, skipped = {}, []

    def count(sql):
        return int(side.psql(database, sql).splitlines()[-1])

    # The bulk payment file has the batch payment file's gates, unchanged.
    if workload in ("batch-payments", "batch-payments-bulk"):
        run_pk = bench.get("run_pk")
        paid = count(f"""{DERIVED_UUID_SQL}
            SELECT count(DISTINCT a.account_pk) FROM ar.account a
            JOIN ar.payment p ON p.payment_pk = pg_temp.derived_uuid('bakeoff:payment:{run_pk}:' || a.account_pk::text);""")
        payments = count("SELECT count(*) FROM ar.payment")
        after = count("SELECT coalesce(sum(balance_minor_units), 0) FROM ar.account_balance_bucket")
        checks["one payment per account, keyed for this run"] = paid == accounts and payments == accounts
        checks["balances fell by exactly 2,500 x N"] = before_total - after == 2500 * accounts
    elif workload == "batch-accrual":
        good = count(f"""SELECT count(*) FROM ar.account a
            JOIN ar.account_balance_bucket b ON b.account_fk = a.account_pk
            JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk AND d.code = 'interest'
            WHERE a.interest_accrued_through = DATE '2026-10-01'
              AND b.balance_minor_units = (100 + ({INDEX_SQL} * 104729) % 20000)
                                        + (50000 + ({INDEX_SQL} * 7919) % 950000) * 1800 / 3650000""")
        checks["every account accrued exactly principal x 1800 / 3650000, through 2026-10-01"] = good == accounts
    elif workload in PLACEMENTS:
        run_pk = bench.get("run_pk")
        good = count(f"""{DERIVED_UUID_SQL}
            SELECT count(*) FROM generate_series(0::bigint, {placements - 1}) AS g(i)
            JOIN ar.account a ON a.account_pk = pg_temp.derived_uuid('bakeoff:placement:{run_pk}:' || g.i)
            WHERE (SELECT jsonb_object_agg(d.code, b.balance_minor_units) FROM ar.account_balance_bucket b
                   JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk
                   WHERE b.account_fk = a.account_pk)
                  = jsonb_build_object('principal', 50000 + (g.i * 7919) % 950000,
                                       'interest', 100 + (g.i * 104729) % 20000, 'fee', 500 + (g.i % 3) * 1000)
              AND (SELECT count(*) FROM core.bakeoff_event_log e
                   WHERE e.aggregate_pk_ref = a.account_pk AND e.event_type = 'account.placed') = 1;""")
        added = count("SELECT count(*) FROM ar.account") - accounts
        checks["M new accounts under their derived keys, with the formulas' balances and one account.placed each"] = (
            good == placements and added == placements)
    elif workload == "realtime":
        bad = count(f"""WITH opening AS (SELECT a.account_pk, {OPENING_SQL.format(i=INDEX_SQL)} AS total FROM ar.account a),
            current AS (SELECT account_fk, sum(balance_minor_units) AS total FROM ar.account_balance_bucket GROUP BY account_fk),
            applied AS (SELECT d.key::uuid AS account_pk, sum(b.value::bigint) AS amount
                        FROM ar.payment p, jsonb_each(p.distribution) d, jsonb_each_text(d.value) b
                        WHERE p.status = 'posted' GROUP BY 1)
            SELECT count(*) FROM opening o LEFT JOIN current c ON c.account_fk = o.account_pk
            LEFT JOIN applied ap ON ap.account_pk = o.account_pk
            WHERE o.total - coalesce(c.total, 0) <> coalesce(ap.amount, 0)""")
        checks["every account's opening minus current balance equals what payments applied"] = bad == 0
        if engine_paused:
            skipped += ["every register entry due 5 s before the end has fired",
                        "every first_contact_due event has exactly one task"]
        else:
            overdue = count(f"""SELECT count(*) FROM core.pending_event
                WHERE status <> 'fired' AND due_at < '{end_clock}'::timestamptz - interval '5 seconds'""")
            checks["every register entry due 5 s before the end has fired"] = overdue == 0
            tasks = count("""SELECT count(*) FROM core.bakeoff_event_log e WHERE e.event_type = 'account.first_contact_due'
                AND (SELECT count(*) FROM ar.task t WHERE t.account_fk = e.aggregate_pk_ref AND t.task_type = 'first_contact') <> 1""")
            checks["every first_contact_due event has exactly one task"] = tasks == 0
    if hash_off:
        skipped.append("every hash chain verifies")
    else:
        checks["every hash chain verifies"] = bool(verify and verify.get("ok"))
    if engine_paused:
        skipped += ["every account summary matches the account and its buckets",
                    "every payment read model row matches its payment",
                    "every payment line's applied amount matches the payment's distribution"]
        return checks, skipped
    checks["every account summary matches the account and its buckets"] = count("""
        SELECT count(*) FROM ar.account a
        LEFT JOIN ar.account_summary_read_model s ON s.account_summary_read_model_pk = a.account_pk
        LEFT JOIN (SELECT account_fk, sum(balance_minor_units) AS total FROM ar.account_balance_bucket GROUP BY account_fk) b
               ON b.account_fk = a.account_pk
        WHERE s.account_summary_read_model_pk IS NULL OR s.balance_minor_units <> coalesce(b.total, 0)
           OR s.status <> a.status OR s.is_disputed <> a.is_disputed""") == 0
    checks["every payment read model row matches its payment"] = count("""
        SELECT count(*) FROM ar.payment p LEFT JOIN ar.payment_read_model r ON r.payment_read_model_pk = p.payment_pk
        WHERE r.payment_read_model_pk IS NULL OR r.amount_minor_units <> p.amount_minor_units OR r.status <> p.status
           OR r.unapplied_minor_units <> p.unapplied_minor_units""") == 0
    checks["every payment line's applied amount matches the payment's distribution"] = count("""
        SELECT count(*) FROM ar.account_payment_read_model_v2 l JOIN ar.payment p ON p.payment_pk = l.payment_pk
        WHERE l.applied_minor_units <> coalesce((SELECT sum(value::bigint) FROM jsonb_each_text(p.distribution -> l.account_pk::text)), 0)""") == 0
    return checks, skipped


def top_statements(side, database, limit):
    """The run's most expensive statements by total time, from pg_stat_statements."""
    text = side.psql(database, f"""SELECT coalesce(json_agg(t), '[]') FROM (
        SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 4) AS mean_ms, rows,
               left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query
        FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
        ORDER BY total_exec_time DESC LIMIT {int(limit)}) t""", check=False)
    return json.loads(text) if text else None


def run_one(side, args, workers, index, raw_dir):
    template = template_name(args.accounts)
    if side.psql("postgres", f"SELECT 1 FROM pg_database WHERE datname = '{template}'") != "1":
        raise SystemExit(f"{template} does not exist; run: prepare --side {side.name} --accounts {args.accounts}")
    database = f"bakeoff_run_{index}"
    stem = f"{index:02d}-{args.workload}-w{workers}"
    side.psql("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    side.psql("postgres", f"CREATE DATABASE {database} TEMPLATE {template}")
    accounts = int(side.psql(database, "SELECT count(*) FROM ar.account"))
    before_total = int(side.psql(database, "SELECT coalesce(sum(balance_minor_units), 0) FROM ar.account_balance_bucket"))

    engine = None
    if not args.no_engine:
        engine = side.start("engine", database, f"{side.project}-run-engine", side.cli[0],
                            [*side.cli[1:], "run-engine", *args.engine_args.split()])
    engine_started = time.perf_counter()
    bench = side.start("bench", database, f"{side.project}-run-bench", "sleep", ["infinity"])
    time.sleep(args.engine_settle)
    for name in (engine, bench):
        if name and not running(name):
            logs = sh(["docker", "logs", "--tail", "40", name], check=False)
            raise RuntimeError(f"{name} is not running:\n{logs.stdout}{logs.stderr}")
    containers = {"db": side.container("db"), "bench": bench, **({"engine": engine} if engine else {})}
    # The engine must have finished starting before the probe and the bench.
    engine_ready_s = engine_idle_cores = None
    if engine:
        engine_ready_s, engine_idle_cores = wait_for_engine(engine, engine_started, args.engine_ready_timeout)
        if engine_ready_s is None:
            log(f"  the engine did not settle within {args.engine_ready_timeout} s"
                f" (last {engine_idle_cores} cores); the run goes ahead, and the result says so")
    # Before the measured window, and before the statement statistics reset.
    probe_ms, probe_samples = host_probe(side, database)
    side.psql(database, "SELECT pg_stat_statements_reset()")
    before = counters(side, database, containers)
    sampler = LagSampler(side, database)
    sampler.start()

    command = [*side.cli, "bench", args.workload, "--workers", str(workers)]
    if args.workload == "realtime":
        command += ["--warmup", str(args.warmup), "--duration", str(args.duration), "--random-seed", str(args.random_seed)]
    elif args.workload in PLACEMENTS:
        command += ["--accounts", str(args.placements)]
    log(f"run {index}: {side.name} {args.workload} with {workers} workers")
    result = sh(["docker", "exec", bench, *command], check=False, timeout=args.timeout)
    after = counters(side, database, containers)
    _, end_head = sampler.positions()
    statements = int(side.psql(database, """SELECT coalesce(sum(calls), 0) FROM pg_stat_statements
        WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())""") or 0)
    # Snapshotted with the statement count, at the end of the bench command.
    statement_breakdown = top_statements(side, database, args.top_statements)
    (raw_dir / f"{stem}.statements.json").write_text(json.dumps(statement_breakdown, indent=1), encoding="utf-8")
    (raw_dir / f"{stem}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (raw_dir / f"{stem}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    bench_json = last_json(result.stdout) or {}

    def finish():
        if engine:
            sh(["docker", "stop", "-t", "15", engine], check=False)
        sh(["docker", "rm", "-f", *filter(None, (engine, bench))], check=False)
        if not args.keep:
            side.psql("postgres", f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")

    # Per-command figures divide the whole bench command's counters by every
    # command it sent, the warm-up's included (WORKLOADS.md). A side that
    # does not report sent_total cannot be measured, so stop here.
    if "sent_total" not in bench_json:
        sampler.stop()
        finish()
        raise RuntimeError(f"run {index}: the bench JSON has no sent_total, which WORKLOADS.md requires;"
                           f" exit code {result.returncode}. Output is in {raw_dir / (stem + '.stdout.txt')}")

    # Catch-up (measured): until every reader has reached the head as it stood
    # when the bench command ended; events written after that, such as
    # scheduled reviews still firing, do not count. Settle (not measured):
    # then until nothing is left and, in real time and placement, every entry
    # due five seconds before the end has fired, so the gates see a finished
    # state.
    # With the engine paused nothing reads the stream, so there is no wait.
    caught_up_at = settled_at = catchup_cpu = None
    wait_started = time.perf_counter()
    while engine and time.perf_counter() - wait_started < args.catchup_timeout:
        low, head = sampler.positions()
        if low is None:
            if time.perf_counter() - wait_started > 30:
                break  # no progress view on this side; give the engine 30 s
            time.sleep(1)
            continue
        if caught_up_at is None and end_head is not None and low >= end_head:
            caught_up_at = time.perf_counter()
            catchup_cpu = cpu_usec(engine)
        overdue = 0
        if args.workload == "realtime" or args.workload in PLACEMENTS:
            overdue = int(side.psql(database, f"""SELECT count(*) FROM core.pending_event WHERE status <> 'fired'
                AND due_at < '{after['clock']}'::timestamptz - interval '5 seconds'""") or 0)
        if caught_up_at is not None and low >= head and overdue == 0:
            settled_at = time.perf_counter()
            break
        time.sleep(0.5)
    sampler.stop()

    # With hashing off, events carry a placeholder hash, so the chain cannot verify.
    verify = None
    if args.hash == "on":
        verify_result = sh(["docker", "exec", bench, *side.cli, "verify-chain"], check=False, timeout=6 * 3600)
        (raw_dir / f"{stem}.verify.txt").write_text(verify_result.stdout + verify_result.stderr, encoding="utf-8")
        verify = last_json(verify_result.stdout)
    checks, skipped = gates(side, database, args.workload, bench_json, before_total, accounts, args.placements,
                            after["clock"], verify, engine_paused=engine is None, hash_off=args.hash == "off")
    measured = measurements(side, database, before["clock"])
    if engine:
        engine_logs = sh(["docker", "logs", engine], check=False)
        (raw_dir / f"{stem}.engine.txt").write_text(engine_logs.stdout + engine_logs.stderr, encoding="utf-8")
    images = {k: sh(["docker", "inspect", "-f", "{{.Image}}", v], check=False).stdout.strip() for k, v in containers.items()}
    container_limits = {k: limits(v) for k, v in containers.items()}

    finish()

    window = after["t"] - before["t"]
    sent = bench_json.get("sent") or 0
    sent_total = bench_json["sent_total"]

    def cpu(phase_from, phase_to, key):
        a, b = phase_from["cpu"].get(key), phase_to["cpu"].get(key)
        return round((b - a) / 1e6, 3) if a is not None and b is not None else None

    cpu_window = {k: cpu(before, after, k) for k in containers}
    engine_after = after["cpu"].get("engine")
    record = {
        "index": index,
        "side": side.name,
        "workload": args.workload,
        "workers": workers,
        "accounts": accounts,
        "placements": args.placements if args.workload in PLACEMENTS else None,
        "engine_args": args.engine_args,
        "engine_paused": engine is None,
        "hashing": args.hash,
        "caps": side.caps,
        "pgbouncer": side.pgbouncer,
        "bench": bench_json,
        "bench_exit_code": result.returncode,
        "window_s": round(window, 3),
        "sent_total": sent_total,
        "cpu_s": cpu_window,
        "cpu_throttling": throttling(before, after),
        "host_cpu": steal(before, after),
        "host_probe_ms": probe_ms,
        "engine_ready_s": engine_ready_s,
        "engine_idle_cores": engine_idle_cores,
        "db_activity": activity_delta(before, after),
        "host_probe_samples_ms": probe_samples,
        "cpu_ms_per_command": {k: (round(v * 1000 / sent_total, 3) if v is not None and sent_total else None)
                               for k, v in cpu_window.items()},
        "engine_catchup_cpu_s": (round((catchup_cpu - engine_after) / 1e6, 3)
                                 if catchup_cpu is not None and engine_after is not None else None),
        "statements": statements,
        "statements_per_command": round(statements / sent_total, 2) if sent_total else None,
        "top_statements": statement_breakdown,
        "commits_per_command": round((after["commits"] - before["commits"]) / sent_total, 2) if sent_total else None,
        "rollbacks": after["rollbacks"] - before["rollbacks"],
        "lag_samples": sampler.samples,
        "max_lag_events": max((lag for _, lag in sampler.samples), default=None) if sampler.available else None,
        "end_head": end_head,
        "catchup_s": round(caught_up_at - after["t"], 1) if caught_up_at else None,
        "settle_s": round(settled_at - after["t"], 1) if settled_at else None,
        "verify": verify,
        "gates": checks,
        "gates_skipped": skipped,
        "gates_pass": all(checks.values()),
        **measured,
        "images": images,
        "limits": container_limits,
        "clock": {"start": before["clock"], "end": after["clock"]},
    }
    (raw_dir / f"{stem}.result.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    log(f"  {sent} sent, {bench_json.get('succeeded')} succeeded, {bench_json.get('elapsed_s')} s;"
        f" gates {'PASS' if record['gates_pass'] else 'FAIL'}")
    return record


# The host under Docker, read on Windows with one PowerShell call. Docker's
# NCPU and MemTotal are what Docker may use; these are the machine itself.
HOST_PROBE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$processors = @(Get-CimInstance Win32_Processor)
$system = Get-CimInstance Win32_ComputerSystem
$disks = @(Get-PhysicalDisk | ForEach-Object {
    [pscustomobject]@{ name = $_.FriendlyName; media_type = [string]$_.MediaType; bus_type = [string]$_.BusType; size_bytes = [int64]$_.Size }
})
$batteries = @(Get-CimInstance Win32_Battery)
$online = @(Get-CimInstance -Namespace root/WMI -ClassName BatteryStatus | ForEach-Object { $_.PowerOnline })
if ($batteries.Count -eq 0) { $ac = $true }
elseif ($online.Count -gt 0) { $ac = [bool]($online -contains $true) }
else { $ac = [bool]($batteries | Where-Object { $_.BatteryStatus -ne 1 }) }
[pscustomobject]@{
    cpu_model = ($processors | Select-Object -First 1).Name.Trim()
    logical_cores = [int](($processors | Measure-Object NumberOfLogicalProcessors -Sum).Sum)
    ram_bytes = [int64]$system.TotalPhysicalMemory
    disks = $disks
    power_plan = ((powercfg /getactivescheme) -join ' ').Trim()
    has_battery = $batteries.Count -gt 0
    on_ac_power = $ac
} | ConvertTo-Json -Depth 4 -Compress
"""


HOST_FACTS = Path("/etc/openar-sandbox/host-facts.json")
HOST_FACT_KEYS = ("provider", "instance_type", "instance_id", "region", "availability_zone")


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def linux_host():
    """The Linux host, read from /proc, /sys, and lsblk. Anything unreadable stays null."""
    host = {}
    cpuinfo = _read("/proc/cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines()
                  if line.lower().startswith("model name")), None)
    if not model:
        # Arm processors, such as AWS Graviton, have no "model name" line.
        lscpu = sh(["lscpu"], check=False).stdout
        model = next((line.split(":", 1)[1].strip() for line in lscpu.splitlines()
                      if line.startswith("Model name")), None)
    host["cpu_model"] = model or platform.processor() or None
    host["logical_cores"] = os.cpu_count()
    meminfo = _read("/proc/meminfo") or ""
    total_kb = next((int(line.split()[1]) for line in meminfo.splitlines() if line.startswith("MemTotal:")), None)
    host["ram_bytes"] = total_kb * 1024 if total_kb else None
    host["kernel"] = platform.release()
    disks = []
    out = sh(["lsblk", "-J", "-b", "-d", "-o", "NAME,MODEL,ROTA,TRAN,SIZE,TYPE"], check=False).stdout
    try:
        for d in json.loads(out).get("blockdevices", []):
            if d.get("type") != "disk":
                continue
            rota = d.get("rota")
            disks.append({"name": " ".join(filter(None, (d.get("name"), (d.get("model") or "").strip()))),
                          "media_type": None if rota is None else ("HDD" if rota in (True, "1", 1) else "SSD"),
                          "bus_type": d.get("tran"), "size_bytes": int(d["size"]) if d.get("size") else None})
    except (ValueError, AttributeError):
        disks = None
    host["disks"] = disks or None
    host["cpu_governor"] = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    host["energy_performance_preference"] = _read(
        "/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
    supplies = list(Path("/sys/class/power_supply").glob("*"))
    batteries = [d for d in supplies if (_read(d / "type") or "") == "Battery"]
    mains = [d for d in supplies if (_read(d / "type") or "") == "Mains"]
    host["has_battery"] = bool(batteries)
    if mains:
        host["on_ac_power"] = any(_read(d / "online") == "1" for d in mains)
    else:
        host["on_ac_power"] = True if not batteries else None
    return host


def host_facts():
    """The cloud sandbox's facts file, written on the host's first boot from the
    provider's metadata service, so the harness never calls that service. A
    missing file means the host is not such a sandbox."""
    try:
        facts = json.loads(HOST_FACTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict.fromkeys(HOST_FACT_KEYS)
    return {key: facts.get(key) for key in HOST_FACT_KEYS}


def host_machine():
    """CPU model, logical cores, RAM, physical disks, and power settings, read
    on Windows with PowerShell and on Linux from /proc, /sys, and lsblk, plus
    the cloud sandbox's host facts where that file exists. Values that cannot
    be read are null."""
    host = dict.fromkeys(("cpu_model", "logical_cores", "ram_bytes", "disks", "power_plan", "power_plan_name",
                          "has_battery", "on_ac_power", "kernel", "cpu_governor", "energy_performance_preference"))
    if platform.system() == "Linux":
        host.update(linux_host())
    if platform.system() == "Windows":
        encoded = base64.b64encode(HOST_PROBE.encode("utf-16-le")).decode("ascii")
        result = sh(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=False, timeout=120)
        try:
            host.update(json.loads(result.stdout.strip().splitlines()[-1]))
        except (IndexError, json.JSONDecodeError):
            pass
    if isinstance(host["disks"], dict):
        host["disks"] = [host["disks"]]  # ConvertTo-Json unwraps a one-item list
    if host["power_plan"]:
        name = re.search(r"\(([^()]*)\)\s*$", host["power_plan"])
        host["power_plan_name"] = name.group(1) if name else None
    host.update(host_facts())
    return host


def environment(side):
    info = json.loads(sh(["docker", "info", "--format", "{{json .}}"]).stdout)
    others = sh(["docker", "ps", "--format", "{{.Names}}"], check=False).stdout.split()
    settings = side.psql("postgres", "SELECT name || ' = ' || setting || coalesce(' ' || unit, '') FROM pg_settings"
                                     " WHERE source NOT IN ('default', 'override') ORDER BY name")
    side_dir = side.compose.parent
    return {
        "host": platform.platform(),
        "host_machine": host_machine(),
        "python_version": platform.python_version(),
        # Raw and unparsed, so no change in its format can break the harness.
        "compose_version": sh(["docker", "compose", "version"], check=False).stdout.strip() or None,
        "docker": {k: info.get(k) for k in ("ServerVersion", "OperatingSystem", "KernelVersion", "NCPU", "MemTotal")},
        "docker_platform": sh(["docker", "version", "--format", "{{.Server.Platform.Name}}"],
                              check=False).stdout.strip() or None,
        "postgres": side.psql("postgres", "SELECT version()"),
        "postgres_settings": settings.splitlines() if settings else [],
        "repository_commit": sh(["git", "-C", str(BAKEOFF), "rev-parse", "HEAD"], check=False).stdout.strip(),
        "side_uncommitted_changes": sh(["git", "-C", str(BAKEOFF), "status", "--porcelain", "--", str(side_dir), str(HARNESS)],
                                       check=False).stdout.strip().splitlines(),
        "caps": side.caps,
        "caps_file": side.caps_file.name,
        "caps_sha256": hashlib.sha256(side.caps_file.read_bytes()).hexdigest(),
        # The file in place of compose.override.yml; the same hash as caps_sha256.
        "override_sha256": hashlib.sha256(side.caps_file.read_bytes()).hexdigest(),
        "other_containers": [name for name in others if not name.startswith(side.project)],
    }


def summarize(records):
    """Median of the measured repeats for each worker count."""
    by_workers = {}
    for record in records:
        by_workers.setdefault(record["workers"], []).append(record)

    def median(values):
        values = [v for v in values if v is not None]
        return round(statistics.median(values), 3) if values else None

    rows = []
    for workers, group in sorted(by_workers.items()):
        rate = [r["bench"].get("succeeded", 0) / r["bench"]["elapsed_s"] for r in group if r["bench"].get("elapsed_s")]
        rows.append({
            "workers": workers,
            "runs": len(group),
            "succeeded_per_s": median(rate),
            "p50_ms": median([r["bench"].get("latency_ms", {}).get("p50") for r in group]),
            "p99_ms": median([r["bench"].get("latency_ms", {}).get("p99") for r in group]),
            "bench_cpu_ms": median([r["cpu_ms_per_command"].get("bench") for r in group]),
            "engine_cpu_ms": median([r["cpu_ms_per_command"].get("engine") for r in group]),
            "db_cpu_ms": median([r["cpu_ms_per_command"].get("db") for r in group]),
            "throttled": {k: median([(r.get("cpu_throttling") or {}).get(k, {}).get("throttled_pct") for r in group])
                          for k in ("db", "engine", "bench")},
            "host_probe_ms": median([r.get("host_probe_ms") for r in group]),
            "steal_pct": median([(r.get("host_cpu") or {}).get("steal_pct") for r in group]),
            "wal_kb": median([round(((r.get("db_activity") or {}).get("wal") or {}).get("wal_bytes", 0) / 1024
                                    / r["sent_total"], 2) if r.get("sent_total") and ((r.get("db_activity") or {}).get("wal"))
                              else None for r in group]),
            "fpi": median([round(((r.get("db_activity") or {}).get("wal") or {}).get("wal_fpi", 0) / r["sent_total"], 3)
                           if r.get("sent_total") and ((r.get("db_activity") or {}).get("wal")) else None for r in group]),
            "engine_ready_s": median([r.get("engine_ready_s") for r in group]),
            "statements": median([r["statements_per_command"] for r in group]),
            "commits": median([r["commits_per_command"] for r in group]),
            "transactions": median([r["bench"].get("transactions") for r in group]),
            "fallback_chunks": median([r["bench"].get("fallback_chunks") for r in group]),
            "max_lag": median([r["max_lag_events"] for r in group]),
            "catchup_s": median([r["catchup_s"] for r in group]),
            "rule_p50": median([(r["rule_latency_ms"].get("all") or {}).get("p50") for r in group]),
            "rule_p99": median([(r["rule_latency_ms"].get("all") or {}).get("p99") for r in group]),
            "late_p50": median([(r["register_lateness_ms"] or {}).get("p50") for r in group]),
            "late_p99": median([(r["register_lateness_ms"] or {}).get("p99") for r in group]),
            "gates": all(r["gates_pass"] for r in group),
        })
    return rows


def host_line(env):
    """The host machine, for run.md: empty when the probe found nothing."""
    host = env.get("host_machine") or {}
    platform_name = f" {env['docker_platform']}." if env.get("docker_platform") else ""
    if not host.get("cpu_model"):
        return platform_name
    disks = ", ".join(
        d.get("name") + (f" ({' '.join(filter(None, (d.get('bus_type'), d.get('media_type'))))})"
                         if d.get("bus_type") or d.get("media_type") else "")
        for d in host.get("disks") or [] if d.get("name"))
    power = {True: "on AC power", False: "on battery"}.get(host.get("on_ac_power"), "power source unknown")
    if host.get("power_plan"):
        setting = f"power plan {host.get('power_plan_name') or host['power_plan']}"
    else:
        setting = f"CPU governor {host.get('cpu_governor') or 'not exposed'}"
    cloud = (f" {host['provider']} {host.get('instance_type')} in {host.get('availability_zone')}."
             if host.get("provider") else "")
    return (f" Host: {host['cpu_model']}, {host.get('logical_cores')} logical cores,"
            f" {round((host.get('ram_bytes') or 0) / 2**30)} GiB RAM; disks: {disks or 'unknown'};"
            f" {setting}, {power}.{cloud}{platform_name}")


def render(meta, env, rows, records):
    lines = [
        f"# {meta['label']}",
        "",
        f"{'Recorded run.' if meta['record'] else 'Development run, not a recorded result.'}"
        f" Side: {meta['side']}"
        f"{' (' + ', '.join(f'{k}={v}' for k, v in meta['side_env'].items()) + ')' if meta['side_env'] else ''}."
        f" Workload: {meta['workload']}. Accounts: {meta['accounts']:,}."
        f"{' New accounts placed per run: ' + format(meta['placements'], ',') + '.' if meta.get('placements') else ''}"
        f" Engine arguments: `{meta['engine_args'] or '(defaults)'}`. pgBouncer: {'yes' if meta['pgbouncer'] else 'no'}."
        f" Caps: {meta.get('caps', 'standard')}"
        f"{' (' + (meta.get('caps_sha256') or '')[:12] + ')' if meta.get('caps_sha256') else ''}."
        f"{' Engine paused: the engine did not run, so nothing read the stream and there is no catch-up.' if meta.get('engine_paused') else ''}"
        f"{' Hashing off: WAX_HASH=off, so events carry a placeholder hash and verify-chain did not run.' if meta.get('hashing') == 'off' else ''}"
        f" Warm-up runs per worker count: {meta['warmup_runs']}; measured runs: {meta['repeat']}."
        f" Started {meta['started']}, finished {meta['finished']}.",
        "",
        f"Machine: {env['host']}; Docker {env['docker']['ServerVersion']} on {env['docker']['OperatingSystem']},"
        f" {env['docker']['NCPU']} CPUs, {round((env['docker']['MemTotal'] or 0) / 2**30)} GiB."
        f"{host_line(env)}"
        f" PostgreSQL: {env['postgres']}. Repository commit {env['repository_commit']}"
        f"{' with uncommitted changes in this side or the harness' if env['side_uncommitted_changes'] else ''}."
        f" Other containers running: {len(env['other_containers'])}.",
        "",
        "Medians of the measured runs. CPU is milliseconds per command, from each container's cgroup counter over the whole"
        " bench command, divided by every command it sent, the warm-up's included (sent_total); engine CPU spent catching up"
        " afterward is in the raw results. Statements and commits per command are divided the same way, and count everything"
        " the run's database executed in that window, the engine's reads and rules included. Catch-up is the time from the"
        " end of the bench command until every reader reached the head as it stood at that end; events written afterward do"
        " not count. Rule latency and register lateness are in milliseconds. Transactions are those the bench driver"
        " reports committing; in a bulk workload, latency is per transaction, that is, per chunk. Throttled is the"
        " percentage of each container's scheduler periods in which it hit its CPU limit; a container throttled for much"
        " of the run was capped by its limit. Host probe is the median of five timings of a fixed CPU-bound statement in"
        " the run's database just before the bench, outside the measured window; a probe well above the night's usual"
        " marks a run during which the machine itself ran slow. Steal is the share of the kernel's CPU time taken by the"
        " hypervisor for other tenants over the bench command, from /proc/stat as the database container sees it. WAL is"
        " the write-ahead log written per command over the bench command, with full-page images per command; the WAL,"
        " checkpointer, background writer, and per-table update and vacuum counts are in the raw results. Engine ready"
        " is how long the engine took to finish starting, judged by its CPU, before the probe and the bench began.",
        "",
        "| Workers | Succeeded/s | p50 ms | p99 ms | Bench CPU | Engine CPU | DB CPU | Statements/cmd | Commits/cmd |"
        " Transactions (fallback chunks) | Max lag (events) | Catch-up s | Rule p50 / p99 | Lateness p50 / p99 |"
        " Throttled % db / engine / bench | Host probe ms | Steal % | WAL KB/cmd (FPI/cmd) | Engine ready s | Gates |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r['workers']} | {r['succeeded_per_s']} | {r['p50_ms']} | {r['p99_ms']} | {r['bench_cpu_ms']} |"
                     f" {r['engine_cpu_ms']} | {r['db_cpu_ms']} | {r['statements']} | {r['commits']} |"
                     f" {r['transactions']} ({r['fallback_chunks']}) | {r['max_lag']} | {r['catchup_s']} |"
                     f" {r['rule_p50']} / {r['rule_p99']} | {r['late_p50']} / {r['late_p99']} |"
                     f" {r['throttled']['db']} / {r['throttled']['engine']} / {r['throttled']['bench']} |"
                     f" {r['host_probe_ms']} | {r['steal_pct']} | {r['wal_kb']} ({r['fpi']}) | {r['engine_ready_s']} |"
                     f" {'pass' if r['gates'] else 'FAIL'} |")
    skipped = sorted({name for r in records for name in r.get("gates_skipped", [])})
    if skipped:
        lines += ["", "Gates skipped because of the diagnostic: " + "; ".join(skipped) + "."]
    lines += ["", "Each run's top statements by total time, from pg_stat_statements, are in its `raw/` `.statements.json`."]
    failed = [r for r in records if not r["gates_pass"]]
    if failed:
        lines += ["", "Failed gates:"]
        for r in failed:
            lines.append(f"- run {r['index']} ({r['workers']} workers): "
                         + "; ".join(name for name, ok in r["gates"].items() if not ok))
    lines += ["", "Raw output, one set per run, is in `raw/`: the bench command's stdout and stderr, verify-chain's output,"
              " the engine's log, and the full measurements as JSON. `environment.json` holds the PostgreSQL settings"
              " and the image of every container."]
    return "\n".join(lines) + "\n"


def cmd_run(args):
    side = Side(args.side, args.pgbouncer, caps=args.caps)
    if args.workload in side.workloads_excluded:
        raise SystemExit(f"{side.name} does not run {args.workload}; see workloads_excluded in sides.json")
    if args.hash == "off":
        side.container_env = {**side.container_env, "WAX_HASH": "off"}
    side.up_db()
    label = args.label or "-".join([side.name, args.workload, *(["no-engine"] if args.no_engine else []),
                                    *(["hash-off"] if args.hash == "off" else [])])
    if not re.fullmatch(r"[a-z0-9-]+", label):
        raise SystemExit("--label may contain only lowercase letters, digits, and hyphens")
    base = BAKEOFF / "results" if args.record else HARNESS / "out"
    out_dir = base / f"{datetime.now():%Y-%m-%d}-{label}"
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    env = environment(side)
    (out_dir / "environment.json").write_text(json.dumps(env, indent=1), encoding="utf-8")
    meta = {"label": label, "side": side.name, "side_env": {**side.compose_env, **side.container_env},
            "workload": args.workload, "accounts": args.accounts,
            "placements": args.placements if args.workload in PLACEMENTS else None,
            "engine_args": args.engine_args, "engine_paused": args.no_engine, "hashing": args.hash,
            "caps": side.caps, "caps_sha256": env["caps_sha256"],
            "pgbouncer": args.pgbouncer, "repeat": args.repeat,
            "warmup_runs": args.warmup_runs, "record": args.record,
            "started": datetime.now().isoformat(timespec="seconds")}
    records, index = [], 0
    for workers in [int(w) for w in args.workers.split(",")]:
        for attempt in range(args.warmup_runs + args.repeat):
            index += 1
            record = run_one(side, args, workers, index, raw_dir)
            record["warmup"] = attempt < args.warmup_runs
            if not record["warmup"]:
                records.append(record)
    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    rows = summarize(records)
    (out_dir / "results.json").write_text(json.dumps({"meta": meta, "summary": rows, "runs": records}, indent=1), encoding="utf-8")
    (out_dir / "run.md").write_text(render(meta, env, rows, records), encoding="utf-8")
    log(f"wrote {out_dir}")
    print(render(meta, env, rows, records))


def cmd_teardown(args):
    side = Side(args.side, args.pgbouncer, caps=args.caps)
    for suffix in ("run-engine", "run-bench"):
        sh(["docker", "rm", "-f", f"{side.project}-{suffix}"], check=False)
    side.dc("--profile", "tools", "down", "-v", "--remove-orphans", check=False)
    log(f"{side.name}: containers, network, and database volume removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("selftest", "prepare", "run", "teardown"):
        p = sub.add_parser(name)
        p.add_argument("--side", choices=sorted(SIDES), required=True)
        p.add_argument("--pgbouncer", action="store_true", help="put pgBouncer in front of PostgreSQL (recorded tuning)")
        p.add_argument("--caps", choices=sorted(CAPS), default="standard",
                       help="the CPU, memory, and PostgreSQL profile: standard is compose.override.yml,"
                            " scaled is compose.scale.yml in its place")
        if name in ("prepare", "run"):
            p.add_argument("--accounts", type=int, required=True)
        if name == "run":
            p.add_argument("--workload", choices=WORKLOADS, required=True)
            p.add_argument("--workers", default="1,2,4,8,16,32", help="comma-separated worker counts")
            p.add_argument("--placements", type=int, default=100_000, help="new accounts per placement run")
            p.add_argument("--no-engine", action="store_true",
                           help="diagnostic: run without the engine; read-model and rule gates are skipped")
            p.add_argument("--hash", choices=["on", "off"], default="on",
                           help="diagnostic: off sets WAX_HASH=off on the engine and bench; the chain gate is skipped")
            p.add_argument("--top-statements", type=int, default=25,
                           help="pg_stat_statements rows recorded per run, by total time")
            p.add_argument("--warmup", type=float, default=15, help="real-time warm-up seconds")
            p.add_argument("--duration", type=float, default=120, help="real-time measured seconds")
            p.add_argument("--random-seed", default="1")
            p.add_argument("--engine-args", default="", help='run-engine flags, for example "--rule-workers 4"')
            p.add_argument("--repeat", type=int, default=1, help="measured runs per worker count (3 for a recorded result)")
            p.add_argument("--warmup-runs", type=int, default=0, help="unreported runs first (1 for a recorded result)")
            p.add_argument("--engine-settle", type=float, default=5,
                           help="minimum seconds between starting the engine and checking that it has settled")
            p.add_argument("--engine-ready-timeout", type=float, default=180,
                           help="seconds to wait for the engine to finish starting before the run goes ahead anyway")
            p.add_argument("--catchup-timeout", type=float, default=1800)
            p.add_argument("--timeout", type=float, default=6 * 3600)
            p.add_argument("--label")
            p.add_argument("--keep", action="store_true", help="keep each run's database")
            p.add_argument("--record", action="store_true", help="write to ../results/ (only with Rob's go)")
    args = parser.parse_args()
    {"selftest": cmd_selftest, "prepare": cmd_prepare, "run": cmd_run, "teardown": cmd_teardown}[args.command](args)


if __name__ == "__main__":
    main()
