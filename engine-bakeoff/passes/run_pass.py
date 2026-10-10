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
"""

import argparse
import contextlib
import json
import shutil
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
        window = plan["accounts"] / RATE[(fam, workload)][workers]
        if entry["diagnostic"] == "no-engine":
            catchup = 0
        elif fam == "python":
            catchup = 1800 if workload == "batch-payments" else 900
        else:
            catchup = 60
        return window + catchup + OVERHEAD_S
    if workload == "batch-payments-bulk":
        return 700 if fam == "csharp" else 1450
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
    if entry["diagnostic"] == "no-engine":
        args += ["--no-engine"]
    if entry["diagnostic"] == "hash-off":
        args += ["--hash", "off"]
    return args


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def stop_databases():
    names = sh(["docker", "ps", "--filter", "name=hivear-bakeoff-", "--format", "{{.Names}}"]).stdout.split()
    for name in names:
        sh(["docker", "stop", "-t", "30", name])
    return names


def stop_run(process, entry):
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
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


def prepare(plan):
    """Seed each side's template at the plan's size; not measured. Sides that
    share a Compose project share a template, so each project is seeded once."""
    projects = {name: spec["project"] for name, spec in
                json.loads((HARNESS / "sides.json").read_text(encoding="utf-8")).items()}
    seeded = set()
    for side in dict.fromkeys(entry["side"] for entry in plan["runs"]):
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
    args = parser.parse_args()
    plan = json.loads((HERE / f"{args.name}.plan.json").read_text(encoding="utf-8"))
    if args.groups:
        groups = {int(group) for group in args.groups.split(",")}
        plan["runs"] = [entry for entry in plan["runs"] if entry["group"] in groups]
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

    clock = datetime.now()
    opened = contextlib.nullcontext() if args.dry_run else log_path.open("a", encoding="utf-8", newline="\n")
    with opened as log:
        for entry in plan["runs"]:
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
            record["stopped_databases"] = stop_databases()
            record["started"] = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
            print(f"[{datetime.now():%H:%M}] {entry['order']:>3} run  {entry['label']} ({record['estimate_min']} min)", flush=True)
            log.write(f"\n===== {entry['order']} {entry['label']} {record['started']}\n{' '.join(command(plan, entry))}\n")
            log.flush()
            process = subprocess.Popen(command(plan, entry), cwd=HARNESS, stdout=log, stderr=subprocess.STDOUT)
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
        save()
    states = [run["state"] for run in status["runs"]]
    print(f"{states.count('done') + states.count('would run')} run, {states.count('failed')} failed, "
          f"{sum(state.startswith('skipped') for state in states)} skipped, of {len(plan['runs'])}")


if __name__ == "__main__":
    main()
