"""Summarizes the pass that tests the reader fix (plan round2-fix) from the
recorded results and prints Markdown.

The driver block first: the payment file at 8 workers with five csharp-wax
driver settings, back to back. Then every run of the fix in plan order, and
the plain runs against the small-pass runs. Each run's readers printed their
passes and applied events every 10 seconds; an interval whose passes applied
fewer than 50 events each on average counts as the many-small-passes state.

    py -3 summarize_fix.py [results folder]

The results folder defaults to the one beside this folder.
"""

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "results"
PLAN = json.loads((HERE / "round2-fix.plan.json").read_text(encoding="utf-8"))
SMALL_PASS_EVENTS = 50


def passes(folder):
    """The pass report's intervals that applied events: (passes, events) each."""
    rows = []
    for log in sorted((folder / "raw").glob("*.engine.txt")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 8 and parts[0] == "passes" and parts[4] == "passes":
                count, events = int(parts[5]), int(parts[7])
                if events > 0:
                    rows.append((count, events))
    return rows


def load(entry):
    found = sorted(RESULTS.glob(f"*-{entry['label']}/results.json"))
    if not found:
        return None
    folder = found[0].parent
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    commands = bench["succeeded"]
    calls = {"discard": 0, "light": 0, "summary": 0, "checkpoint": 0}
    insert_ms = insert_rows = 0
    for statement in run.get("top_statements") or []:
        query = " ".join(statement["query"].split()).upper()
        if query.startswith("INSERT INTO CORE.EVENT"):
            insert_ms += statement["total_ms"]
            insert_rows += statement["rows"]
        elif query.startswith("DISCARD ALL"):
            calls["discard"] += statement["calls"]
        elif query.startswith("SET SESSION AUTHORIZATION DEFAULT"):
            calls["light"] += statement["calls"]
        elif query.startswith("UPDATE AR.ACCOUNT_SUMMARY_READ_MODEL"):
            calls["summary"] += statement["calls"]
        elif query.startswith("UPDATE CORE.PROJECTION_CHECKPOINT SET LAST_GLOBAL_SEQUENCE_NUMBER"):
            calls["checkpoint"] += statement["calls"]
    intervals = passes(folder)
    small = [n for n, e in intervals if e / n < SMALL_PASS_EVENTS]
    failed = [name for name, passed in (run.get("gates") or {}).items() if passed is False]
    return {
        "rate": commands / bench["elapsed_s"],
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "db": cpu.get("db"),
        "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "statements": run.get("statements_per_command"),
        "discards": calls["discard"] / commands,
        "light": calls["light"] / commands,
        "insert": insert_ms / insert_rows if insert_rows else None,
        "summary": calls["summary"],
        "checkpoints": calls["checkpoint"] / commands,
        "catchup": run.get("catchup_s"),
        "gates": run.get("gates_pass"),
        "failed": failed,
        "passes_s": sum(n for n, _ in intervals) / (10 * len(intervals)) if intervals else None,
        "events_pass": sum(e for _, e in intervals) / sum(n for n, _ in intervals) if intervals else None,
        "small_share": len(small) / len(intervals) * 100 if intervals else None,
    }


def f(value, digits=1):
    return "" if value is None else f"{value:,.{digits}f}"


def gate_cell(r):
    return "pass" if r["gates"] else "FAIL: " + ", ".join(str(name) for name in r["failed"])


NOTE = ("CPU is milliseconds per command; statements, DISCARD ALL, and the light reset (counted by its first statement, "
        "SET SESSION AUTHORIZATION DEFAULT) are calls per command; the event insert is execution time per row written, "
        "trigger and foreign-key check included; summary updates are the account summary read model's UPDATE calls over "
        "the run, against 500,000 payments; checkpoint writes are per command; passes are the projection readers' passes "
        "per second and events per pass over the 10-second intervals that applied events; small-pass share is the "
        f"percentage of those intervals averaging under {SMALL_PASS_EVENTS} events per pass.")
HEADER = ("| Order | Run | Commands/s | p99 ms | DB CPU | App CPU | Statements | DISCARD ALL | Light reset "
          "| Event insert ms per row | Summary updates | Checkpoint writes | Passes/s | Events per pass | Small-pass share % "
          "| Catch-up s | Gates |")
RULE = "| --- " * 17 + "|"


def row(entry, r):
    name = entry["label"].replace("round2-fix-", "").replace("-batch-payments-w8", "")
    return (f"| {entry['order']} | {name} | {f(r['rate'], 0)} | {f(r['p99'])} | {f(r['db'], 2)} | {f(r['app'], 2)} "
            f"| {f(r['statements'])} | {f(r['discards'], 2)} | {f(r['light'], 2)} | {f(r['insert'], 3)} "
            f"| {r['summary']:,} | {f(r['checkpoints'], 3)} | {f(r['passes_s'])} | {f(r['events_pass'])} "
            f"| {f(r['small_share'], 0)} | {f(r['catchup'])} | {gate_cell(r)} |")


def main():
    loaded = [(entry, load(entry)) for entry in PLAN["runs"]]
    loaded = [(entry, r) for entry, r in loaded if r]
    out = ["### Driver settings, payment file at 8 workers", "",
           f"*The five csharp-wax driver settings run back to back in one block, in run order, with 16 rule workers. {NOTE}*", "",
           HEADER, RULE]
    out += [row(entry, r) for entry, r in loaded if entry["group"] == 1]
    out += ["", "### The fix: csharp-wax-rw8, payment file at 8 workers", "",
            f"*Every run of the fix, in run order, with Npgsql's defaults and 8 rule workers; the small-pass runs sleep a "
            f"reader 50 ms after a pass that applies fewer than 100 events. {NOTE}*", "", HEADER, RULE]
    out += [row(entry, r) for entry, r in loaded if entry["group"] == 2]
    groups = {"plain": [r for entry, r in loaded if entry["group"] == 2 and entry["diagnostic"] is None],
              "small-pass sleep": [r for entry, r in loaded if entry["group"] == 2 and entry["diagnostic"] == "small-pass-sleep"]}
    out += ["", "*The fix's plain runs against its small-pass runs: the count of runs, how many failed a gate, and the "
            "median of each figure over the runs; units as in the table above.*", "",
            "| Runs | Count | Failed a gate | Commands/s | DB CPU | DISCARD ALL | Passes/s | Events per pass | Small-pass share % "
            "| Runs over half small-pass |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, runs in groups.items():
        if not runs:
            continue
        med = {k: statistics.median(r[k] for r in runs if r[k] is not None) for k in
               ("rate", "db", "discards", "passes_s", "events_pass", "small_share")}
        out.append(f"| {name} | {len(runs)} | {sum(not r['gates'] for r in runs)} | {f(med['rate'], 0)} | {f(med['db'], 2)} "
                   f"| {f(med['discards'], 2)} | {f(med['passes_s'])} | {f(med['events_pass'])} | {f(med['small_share'], 0)} "
                   f"| {sum((r['small_share'] or 0) > 50 for r in runs)} |")
    print("\n".join(out))


if __name__ == "__main__":
    main()
