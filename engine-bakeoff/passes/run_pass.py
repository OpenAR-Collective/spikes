"""Runs a measured pass from its plan, one run at a time, in the plan's order,
and stops at a deadline.

    py -3 run_pass.py benchmark-pass-1 --prepare-only
    py -3 run_pass.py benchmark-pass-1 --deadline "2026-10-10 09:30" --dry-run
    py -3 run_pass.py benchmark-pass-1 --deadline "2026-10-10 09:30"
    py -3 run_pass.py benchmark-pass-1 --deadline "2026-10-10 09:30" --groups 1,2,3

Before each run every bake-off database container is stopped, so the run
starts with only its own side's database up, freshly started, whichever side
ran before it. A run whose estimated time would carry it past the deadline is
skipped and the next one is tried; a run still going at the deadline is
stopped, and its partial output is moved out of results/. Progress goes to
<name>.status.json and the harness's output to <name>.log, beside this file.
A plan that asks for the side sampler gets it in every run; one that asks for
the host counters gets the Windows host's processor counters every 5 seconds
from the first run to the last, in <name>.host-counters.csv, with the named
disk's counters and the thermal zones' if the plan names a disk. A plan may
also ask for these, each described where it is done: WAL I/O timing on the
server (wal_io_timing), a storage check before every run and at the start of
every idle gap (fsync_check), and a snapshot of the host's processes every 30
seconds (process_snapshot). An entry whose kind is "idle" is an idle gap: every
bake-off container is stopped for its minutes, and nothing runs.
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent / "harness"
RESULTS = HERE.parent / "results"

# Rough commands per second from the first pass, rounded down, for the
# deadline check only; nothing measured depends on them.
RATE = {
    ("csharp", "batch-payments"): {4: 300, 8: 450, 16: 750, 32: 800},
    ("csharp", "batch-accrual"): {4: 300, 8: 550, 16: 1000, 32: 1100},
    ("python", "batch-payments"): {4: 190, 8: 240, 16: 290, 32: 290},
    ("python", "batch-accrual"): {4: 250, 8: 330, 16: 450, 32: 450},
}
OVERHEAD_S = 180


def family(side):
    return "python" if side == "python" else "csharp"


def estimate_s(plan, entry):
    fam, workload, workers = family(entry["side"]), entry["workload"], entry["workers"]
    if workload == "realtime":
        return plan["warmup_s"] + plan["duration_s"] + OVERHEAD_S + (400 if fam == "python" else 120)
    if workload in ("batch-payments", "batch-accrual"):
        rates = RATE[(fam, workload)]
        known = [w for w in rates if w <= workers]
        # Below the fewest workers measured, assume the rate falls in proportion.
        rate = rates[max(known)] if known else rates[min(rates)] * workers / min(rates)
        window = plan["accounts"] / rate
        if entry["diagnostic"] == "no-engine":
            catchup = 0
        elif fam == "python":
            catchup = 1800 if workload == "batch-payments" else 900
        else:
            catchup = 60
        return window + catchup + OVERHEAD_S
    if workload == "batch-payments-bulk":
        # Bulk time, verify-chain, and catch-up all grow with the seed.
        return (700 if fam == "csharp" else 1450) * max(1, plan["accounts"] / 500_000)
    if workload == "placement":
        return 980 if fam == "csharp" else 1880
    if workload == "placement-bulk":
        return 840 if fam == "csharp" else 1500
    raise ValueError(workload)


def history(names):
    """How long each kind of run took in earlier passes, in minutes, keyed by
    side and by family, so the deadline check can use real times."""
    seen = {}
    for name in names:
        status = json.loads((HERE / f"{name}.status.json").read_text(encoding="utf-8"))
        runs = {entry["label"]: entry for entry in
                json.loads((HERE / f"{name}.plan.json").read_text(encoding="utf-8"))["runs"]}
        for record in status["runs"]:
            if record["state"] != "done":
                continue
            entry = runs[record["label"]]
            took = (datetime.fromisoformat(record["finished"]) - datetime.fromisoformat(record["started"])).total_seconds()
            for key in history_keys(entry):
                seen[key] = max(seen.get(key, 0), took)
    return seen


def history_keys(entry):
    # A triple's normal run and an untuned run take as long as a plain one.
    diagnostic = entry["diagnostic"] if entry["diagnostic"] in ("no-engine", "hash-off") else ""
    return [(entry["side"], entry["workload"], entry["workers"], diagnostic),
            (family(entry["side"]), entry["workload"], entry["workers"], diagnostic)]


def estimate(plan, entry, seen):
    """The longest earlier run of the same kind plus 15 percent, or the rough
    estimate when no earlier run matches."""
    for key in history_keys(entry):
        if key in seen:
            return seen[key] * 1.15
    return estimate_s(plan, entry)


def command(plan, entry):
    args = [sys.executable, "bakeoff_harness.py", "run", "--side", entry["side"], "--workload", entry["workload"],
            "--workers", str(entry["workers"]), "--accounts", str(plan["accounts"]),
            f"--engine-args={entry['engine_args']}", "--catchup-timeout", str(plan["catchup_timeout_s"]),
            "--label", entry["label"], "--record"]
    if entry["workload"] == "realtime":
        args += ["--warmup", str(plan["warmup_s"]), "--duration", str(plan["duration_s"])]
    if entry["workload"].startswith("placement"):
        args += ["--placements", str(plan["placements"])]
    if plan.get("caps", "standard") != "standard":
        args += ["--caps", plan["caps"]]
    if plan.get("pool_size"):
        args += ["--pgbouncer", "--pool-size", str(plan["pool_size"])]
    if entry["diagnostic"] == "no-engine":
        args += ["--no-engine"]
    if entry["diagnostic"] == "hash-off":
        args += ["--hash", "off"]
    if plan.get("side_sampler"):
        args += ["--side-sampler"]
    return args


# The Windows host's processor counters: its speed against nominal, the share
# of it a power or thermal limit allows and the flags that name the limit,
# Windows' own busy figures, the CPU of the virtual machine Docker runs in,
# the hypervisor's run time across every logical processor, and each logical
# processor's own load and speed.
HOST_COUNTERS = [
    r"\Processor Information(_Total)\% Processor Performance",
    r"\Processor Information(_Total)\% Performance Limit",
    r"\Processor Information(_Total)\Performance Limit Flags",
    r"\Processor Information(_Total)\% Processor Utility",
    r"\Processor(_Total)\% Processor Time",
    r"\Process(vmmemWSL)\% Processor Time",
    r"\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time",
    r"\Hyper-V Hypervisor Logical Processor(_Total)\% Guest Run Time",
    # Each logical processor's load and speed: a hybrid processor's
    # performance and efficiency cores run at different speeds, and the
    # totals cannot show load moving from one kind to the other.
    r"\Processor Information(*)\% Processor Time",
    r"\Processor Information(*)\% Processor Performance",
]


def disk_counters(disk):
    """The named disk's counters: write latency, the latency of every transfer,
    writes and bytes written per second, the write queue, and idle time, for
    the physical disk and its logical volume; and every thermal zone's
    temperature, the share of its speed a passive cooling limit allows, and
    the reasons it gives for throttling. Windows counts reads and writes here,
    not the flush requests a sync becomes, so a flush's own time may not show."""
    if not disk:
        return []
    names = ["Avg. Disk sec/Write", "Avg. Disk sec/Transfer", "Disk Writes/sec", "Disk Write Bytes/sec",
             "Avg. Disk Write Queue Length", "% Idle Time"]
    return ([rf"\PhysicalDisk({disk['physical']})\{name}" for name in names]
            + [rf"\LogicalDisk({disk['logical']})\{name}" for name in names]
            + [r"\Thermal Zone Information(*)\Temperature", r"\Thermal Zone Information(*)\% Passive Limit",
               r"\Thermal Zone Information(*)\Throttle Reasons"])


def start_host_counters(name, deadline, disk=None):
    """Logs HOST_COUNTERS every 5 seconds with typeperf, on Windows only, for a
    sample count that ends it soon after the deadline even if this runner dies."""
    if os.name != "nt":
        print("host counters: not on Windows, so none are logged", flush=True)
        return None
    raw = HARNESS / "out" / f"{name}.host-counters.raw.csv"
    raw.parent.mkdir(parents=True, exist_ok=True)
    samples = max(1, int((deadline - datetime.now()).total_seconds() / 5)) + 60
    process = subprocess.Popen(["typeperf", *HOST_COUNTERS, *disk_counters(disk), "-si", "5", "-sc", str(samples),
                                "-f", "CSV", "-o", str(raw), "-y"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, raw


def stop_host_counters(started, name):
    """Stops the log and writes it beside the plan without the host's name,
    which typeperf puts in every column heading."""
    if not started:
        return
    process, raw = started
    process.terminate()
    process.wait(timeout=30)
    lines = raw.read_text(encoding="utf-8", errors="replace").splitlines() if raw.exists() else []
    if lines:
        lines[0] = re.sub(r'\\\\[^\\"]+\\', r"\\", lines[0])
    (HERE / f"{name}.host-counters.csv").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def start_process_snapshot(name, deadline):
    """Every 30 seconds, each Windows process's CPU and bytes written over the
    interval, from process_snapshot.ps1, to a raw file that stays in the
    harness's ignored out/ folder, since it names every process on the host.
    It ends itself 5 minutes after the deadline even if this runner dies."""
    if os.name != "nt":
        print("process snapshot: not on Windows, so none is taken", flush=True)
        return None
    raw = HARNESS / "out" / f"{name}.processes.raw.csv"
    raw.parent.mkdir(parents=True, exist_ok=True)
    until = (deadline + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
    return subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-File", str(HERE / "process_snapshot.ps1"),
                             "-Out", str(raw), "-Until", until, "-Runner", str(os.getpid())],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# The processes a committed copy of the snapshot names: Docker's own, the
# virtual machine it runs in and WSL's services for it, Windows' kernel, and
# any process the snapshot marked as descended from this runner (the harness,
# typeperf, the Docker command lines, and the snapshot itself). Every other
# process becomes unrelated-process, with its process ID kept so that one
# process can be followed through the night without being named.
NAMED_PROCESSES = ("vmmemWSL", "System", "Docker Desktop.exe", "wsl.exe", "wslservice.exe", "wslhost.exe",
                   "wslrelay.exe", "vmcompute.exe")


def stop_process_snapshot(process, name):
    """Stops the snapshot and writes its committed copy, <name>.processes.csv,
    beside the plan."""
    if not process:
        return
    process.terminate()
    process.wait(timeout=30)
    raw = HARNESS / "out" / f"{name}.processes.raw.csv"
    if not raw.exists():
        return
    rows = raw.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [rows[0]] if rows else []
    for line in rows[1:]:
        cells = line.split(",")
        if len(cells) != 8:
            continue
        stamp, interval, pid, parent, process_name, cpu, written, mine = cells
        if not (process_name.startswith(("_", "com.docker.")) or process_name in NAMED_PROCESSES or mine == "1"):
            process_name = "unrelated-process"
        kept.append(",".join((stamp, interval, pid, parent, process_name, cpu, written, mine)))
    (HERE / f"{name}.processes.csv").write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def stop_databases():
    names = sh(["docker", "ps", "--filter", "name=hivear-bakeoff-", "--format", "{{.Names}}"]).stdout.split()
    for name in names:
        sh(["docker", "stop", "-t", "30", name])
    return names


# pg_test_fsync, from the database's own image, run in the side's database
# container on the data volume while the database is up and idle. The tool
# cannot be told to test one method only, and it first tests open_datasync,
# then fdatasync, the method PostgreSQL uses here, each with one 8 kB write
# for FSYNC_SECONDS; the check stops it as soon as fdatasync's rate is
# printed, so every check is the same short burst of synced writes: the
# tool's test file written and synced once, then the two timed tests. Its
# test file goes in the volume's root, beside the data, and is removed after.
FSYNC_SECONDS = 2
FSYNC_SCRIPT = (
    'F=/var/lib/postgresql/data/bakeoff_fsync_check.tmp; O=/tmp/bakeoff_fsync_check.out; rm -f "$F" "$O"; '
    'pg_test_fsync -f "$F" -s {seconds} > "$O" 2>&1 & p=$!; i=0; '
    'until grep -q "fdatasync .*ops/sec" "$O" || [ $i -ge 600 ]; do sleep 0.05; i=$((i + 1)); done; '
    'kill $p 2>/dev/null; wait $p 2>/dev/null; cat "$O"; rm -f "$F" "$O"'
).format(seconds=FSYNC_SECONDS)


def parse_fsync(text):
    """The ops per second of each method in the one-write section, keyed
    one_8k, and in the two-write section, two_8k, if the tool reached it."""
    found, section = {}, None
    for line in text.splitlines():
        if line.startswith("Compare file sync methods using one 8kB write"):
            section = "one_8k"
        elif line.startswith("Compare file sync methods using two 8kB writes"):
            section = "two_8k"
        elif line.startswith(("Compare", "Test", "Non-sync")):
            section = None
        match = re.match(r"\s+(\w+)\s+([\d.]+) ops/sec", line)
        if section and match:
            found.setdefault(section, {})[match.group(1)] = float(match.group(2))
    return found


def fsync_check(plan, name, tag):
    """Stops every bake-off database, starts the first run's side's database
    fresh, runs pg_test_fsync in it while it is idle, and stops it again, so
    the next run still starts its database fresh. The output goes to
    <name>.fsync.txt under a heading; the parsed rates are returned. A check
    that fails or takes over 5 minutes is recorded as an error and the pass
    goes on: it is a reading beside the runs, not one of them."""
    started = datetime.now()
    # The check's own start and end, from just before the tool starts to just
    # after it stops, without the database's start and stop around it.
    try:
        harness = harness_module()
        side = harness.Side(next(entry["side"] for entry in plan["runs"] if entry.get("kind") != "idle"))
        stop_databases()
        side.up_db()
        db = side.container("db")
        started = datetime.now()
        result = subprocess.run(["docker", "exec", "-u", "postgres", db, "sh", "-c", FSYNC_SCRIPT],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        output, code = result.stdout + result.stderr, result.returncode
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        output, code, result = f"check failed: {error}", None, None
    finished = datetime.now()
    stop_databases()
    with (HERE / f"{name}.fsync.txt").open("a", encoding="utf-8", newline="\n") as out:
        out.write(f"===== {tag} {started:%Y-%m-%dT%H:%M:%S} to {finished:%Y-%m-%dT%H:%M:%S} exit {code}\n")
        out.write(output + "\n")
    rates = parse_fsync(result.stdout) if result else {}
    return {"tag": tag, "started": f"{started:%Y-%m-%dT%H:%M:%S}", "finished": f"{finished:%Y-%m-%dT%H:%M:%S}",
            "seconds": round((finished - started).total_seconds(), 1), "exit_code": code, **rates}


def stop_run(process, entry):
    # The harness and everything it started go together: on Windows the
    # process tree, elsewhere the session the run was started in.
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    for name in sh(["docker", "ps", "--filter", "name=hivear-bakeoff-", "--format", "{{.Names}}"]).stdout.split():
        if "-run-" in name:
            sh(["docker", "rm", "-f", name])
    partial = [path for path in RESULTS.glob(f"*-{entry['label']}") if path.is_dir()]
    for path in partial:
        target = HARNESS / "out" / f"incomplete-{path.name}"
        shutil.rmtree(target, ignore_errors=True)
        shutil.move(str(path), str(target))
    return [str(path.name) for path in partial]


def outcome(entry):
    """The recorded result's gates and throughput, read from its results.json."""
    found = sorted(RESULTS.glob(f"*-{entry['label']}/results.json"))
    if not found:
        return {"gates_pass": None}
    runs = json.loads(found[-1].read_text(encoding="utf-8"))["runs"]
    return {"folder": found[-1].parent.name, "gates_pass": all(run["gates_pass"] for run in runs),
            "succeeded_per_s": [round(run["bench"].get("succeeded", 0) / run["bench"]["elapsed_s"], 1)
                                if run["bench"].get("elapsed_s") else None for run in runs]}


def harness_module():
    sys.path.insert(0, str(HARNESS))
    import bakeoff_harness
    return bakeoff_harness


def clean_databases(plan):
    """Drop every run database left in any side's cluster, so nothing an
    earlier or interrupted run left behind is vacuumed or checkpointed during
    this pass. Templates (bakeoff_seed_*) and the default databases stay."""
    harness = harness_module()
    projects = set()
    for name in dict.fromkeys(entry["side"] for entry in plan["runs"] if entry.get("kind") != "idle"):
        # Only a plan with non-default caps or PgBouncer passes them, so the
        # standard passes need nothing from the harness beyond Side(name).
        options = {}
        if plan.get("pool_size"):
            options["pgbouncer"] = True
        if plan.get("caps", "standard") != "standard":
            options["caps"] = plan["caps"]
        side = harness.Side(name, **options)
        if side.project in projects:
            continue
        projects.add(side.project)
        stop_databases()
        side.up_db()
        leftovers = side.psql("postgres", "SELECT datname FROM pg_database WHERE datname LIKE 'bakeoff_run_%'") or ""
        for database in leftovers.split():
            side.psql("postgres", f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        print(f"{side.project}: dropped {len(leftovers.split())} leftover run databases", flush=True)
        # A plan that asks for WAL I/O timing gets it, or the pass does not start.
        timing = side.psql("postgres", "SHOW track_wal_io_timing")
        if plan.get("wal_io_timing") and timing != "on":
            stop_databases()
            raise SystemExit(f"{side.project}: track_wal_io_timing is {timing!r}, but the plan asks for on; refusing to start")
    stop_databases()


def preflight(docker_root, min_free_gb):
    """On a host where Docker's data must live on a particular disk, such as a
    cloud host's local NVMe, refuse to start unless it does and has room. A
    data root that silently fell back to the boot disk would make every run
    measure the wrong disk."""
    if not docker_root:
        return
    actual = sh(["docker", "info", "--format", "{{.DockerRootDir}}"]).stdout.strip()
    if os.path.normpath(actual) != os.path.normpath(docker_root):
        raise SystemExit(f"Docker's data root is {actual!r}, not {docker_root!r}; refusing to start")
    if os.stat(docker_root).st_dev == os.stat(os.path.abspath(os.sep)).st_dev:
        raise SystemExit(f"{docker_root} is on the same disk as the root file system, not its own mount; refusing to start")
    free_gb = shutil.disk_usage(docker_root).free / 2**30
    if free_gb < min_free_gb:
        raise SystemExit(f"{docker_root} has {free_gb:.0f} GB free, under the {min_free_gb} GB minimum; refusing to start")
    print(f"preflight: Docker data root {docker_root} on its own disk, {free_gb:.0f} GB free", flush=True)


def prepare(plan):
    """Seed each side's template at the plan's size; not measured. Sides that
    share a Compose project share a template, so each project is seeded once."""
    projects = {name: spec["project"] for name, spec in
                json.loads((HARNESS / "sides.json").read_text(encoding="utf-8")).items()}
    seeded = set()
    for side in dict.fromkeys(entry["side"] for entry in plan["runs"] if entry.get("kind") != "idle"):
        if projects[side] in seeded:
            continue
        seeded.add(projects[side])
        stop_databases()
        result = subprocess.run([sys.executable, "bakeoff_harness.py", "prepare", "--side", side,
                                 "--accounts", str(plan["accounts"])], cwd=HARNESS, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
        print((result.stdout + result.stderr).strip().splitlines()[-1], flush=True)
        if result.returncode != 0:
            raise SystemExit(f"prepare failed for {side}")
    stop_databases()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--deadline", help="local time, YYYY-MM-DD HH:MM")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--groups", help="comma-separated priority groups to run; default every group")
    parser.add_argument("--history", default="", help="comma-separated earlier passes whose run times feed the deadline check")
    parser.add_argument("--docker-root", help="require Docker's data root to be this path, on its own disk (a cloud host's NVMe)")
    parser.add_argument("--min-free-gb", type=float, default=50, help="with --docker-root, the free space required there")
    args = parser.parse_args()
    plan = json.loads((HERE / f"{args.name}.plan.json").read_text(encoding="utf-8"))
    # WAL I/O timing is a server setting, given on the database's command line
    # through Compose, so every Compose call this runner or the harness makes
    # must agree on it, or the database container would be recreated.
    os.environ["BAKEOFF_WAL_IO_TIMING"] = "on" if plan.get("wal_io_timing") else "off"
    if args.groups:
        groups = {int(group) for group in args.groups.split(",")}
        plan["runs"] = [entry for entry in plan["runs"] if entry["group"] in groups]
    preflight(args.docker_root, args.min_free_gb)
    if args.prepare_only:
        prepare(plan)
        return
    if not args.deadline:
        parser.error("--deadline is required")
    deadline = datetime.strptime(args.deadline, "%Y-%m-%d %H:%M")
    seen = history([name for name in args.history.split(",") if name])
    status_path, log_path = HERE / f"{args.name}.status.json", HERE / f"{args.name}.log"
    status = {"name": args.name, "seed": plan["seed"], "deadline": args.deadline, "groups": args.groups or "all",
              "started": f"{datetime.now():%Y-%m-%dT%H:%M:%S}", "runs": []}

    def save():
        status_path.write_text(json.dumps(status, indent=1) + "\n", encoding="utf-8", newline="\n")

    if not args.dry_run:
        clean_databases(plan)
    host_counters = (start_host_counters(args.name, deadline, plan.get("host_disk"))
                     if plan.get("host_counters") and not args.dry_run else None)
    snapshot = start_process_snapshot(args.name, deadline) if plan.get("process_snapshot") and not args.dry_run else None
    clock = datetime.now()
    opened = contextlib.nullcontext() if args.dry_run else log_path.open("a", encoding="utf-8", newline="\n")
    with opened as log, contextlib.ExitStack() as stack:
        stack.callback(stop_host_counters, host_counters, args.name)
        stack.callback(stop_process_snapshot, snapshot, args.name)
        for entry in plan["runs"]:
            if entry.get("kind") == "idle":
                # An idle gap: the storage check first, if the plan asks for
                # it, then every bake-off container stopped for its minutes.
                record = {"order": entry["order"], "group": entry["group"], "label": entry["label"],
                          "kind": "idle", "minutes": entry["minutes"]}
                now = clock if args.dry_run else datetime.now()
                if now + timedelta(minutes=entry["minutes"] + 2) > deadline:
                    record["state"] = "skipped: would not finish before the deadline"
                    status["runs"].append(record)
                    print(f"[{now:%H:%M}] {entry['order']:>3} skip {entry['label']}", flush=True)
                    continue
                if args.dry_run:
                    clock = now + timedelta(minutes=entry["minutes"] + 1)
                    record["state"] = "would idle"
                    status["runs"].append(record)
                    print(f"[{now:%H:%M}] {entry['order']:>3} idle {entry['label']} ({entry['minutes']} min)", flush=True)
                    continue
                if plan.get("fsync_check"):
                    record["fsync_check"] = fsync_check(plan, args.name, f"{entry['order']} {entry['label']} start")
                record["stopped_databases"] = stop_databases()
                record["started"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
                print(f"[{datetime.now():%H:%M}] {entry['order']:>3} idle {entry['label']} ({entry['minutes']} min)", flush=True)
                until = min(datetime.now() + timedelta(minutes=entry["minutes"]), deadline)
                while datetime.now() < until:
                    time.sleep(5)
                record["finished"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
                record["state"] = "idle done"
                status["runs"].append(record)
                save()
                if datetime.now() >= deadline:
                    break
                continue
            expected = timedelta(seconds=estimate(plan, entry, seen))
            record = {"order": entry["order"], "group": entry["group"], "label": entry["label"],
                      "estimate_min": round(expected.total_seconds() / 60, 1)}
            now = clock if args.dry_run else datetime.now()
            if now + expected > deadline:
                record["state"] = "skipped: would not finish before the deadline"
                status["runs"].append(record)
                print(f"[{now:%H:%M}] {entry['order']:>3} skip {entry['label']} ({record['estimate_min']} min)", flush=True)
                continue
            if args.dry_run:
                clock = now + expected
                record["state"] = "would run"
                status["runs"].append(record)
                print(f"[{now:%H:%M}] {entry['order']:>3} run  {entry['label']} ({record['estimate_min']} min)", flush=True)
                continue
            if plan.get("fsync_check"):
                record["fsync_check"] = fsync_check(plan, args.name, f"{entry['order']} {entry['label']} before")
            record["stopped_databases"] = stop_databases()
            record["started"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
            print(f"[{datetime.now():%H:%M}] {entry['order']:>3} run  {entry['label']} ({record['estimate_min']} min)", flush=True)
            log.write(f"\n===== {entry['order']} {entry['label']} {record['started']}\n{' '.join(command(plan, entry))}\n")
            log.flush()
            process = subprocess.Popen(command(plan, entry), cwd=HARNESS, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
            while process.poll() is None and datetime.now() < deadline:
                time.sleep(5)
            if process.poll() is None:
                record["moved_out_of_results"] = stop_run(process, entry)
                record["state"] = "stopped at the deadline"
            else:
                record["exit_code"] = process.returncode
                record.update(outcome(entry))
                record["state"] = "done" if process.returncode == 0 and record["gates_pass"] else "failed"
            record["finished"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
            status["runs"].append(record)
            save()
            print(f"[{datetime.now():%H:%M}] {entry['order']:>3} {record['state']}", flush=True)
            if datetime.now() >= deadline:
                break
    status["finished"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
    if not args.dry_run:
        # Nothing a run started outlives the pass: a run stopped at the deadline
        # can leave work inside its database container, such as the side sampler.
        stop_databases()
        if plan.get("fsync_check") and datetime.now() < deadline:
            status["fsync_check_end"] = fsync_check(plan, args.name, "end")
        save()
    states = [run["state"] for run in status["runs"] if run.get("kind") != "idle"]
    gaps = [run["state"] for run in status["runs"] if run.get("kind") == "idle"]
    print(f"{states.count('done') + states.count('would run')} run, {states.count('failed')} failed, "
          f"{sum(state.startswith('skipped') for state in states)} skipped, of "
          f"{sum(entry.get('kind') != 'idle' for entry in plan['runs'])}; "
          f"{sum(not state.startswith('skipped') for state in gaps)} idle gaps of {len(gaps)}")


if __name__ == "__main__":
    main()
