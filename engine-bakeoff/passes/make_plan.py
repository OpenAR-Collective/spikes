"""Writes a measured pass's run list: every run, its priority, and its place in a
shuffled order, from a recorded seed, so that anyone can regenerate the same
list. The plan is committed before the pass starts.

    py -3 make_plan.py second-pass 20261009 laptop

writes second-pass.plan.json and second-pass.plan.md beside this file. The
third argument picks the layout: "laptop" for passes on Rob's laptop, which
has the first pass as its baseline; "third" for the pass after the second,
which carries over the second pass's skipped runs; or "pc", the default, for
the full grid a separate benchmark PC would need. Runs that share a block
stay together, back to back: the blocks are shuffled within their group, and
the runs within each block.
"""

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

ACCOUNTS = 500_000
PLACEMENTS = 100_000
WARMUP_S = 60
DURATION_S = 180
CATCHUP_TIMEOUT_S = 3600

SIDES = ["csharp", "csharp-direct", "csharp-wax", "python"]
BULK_SIDES = ["csharp-direct", "csharp-wax", "python"]

# Each side's engine settings, from its tuning trial. The C# trial on the
# laptop, real time at 50,000 accounts: at 8 rule workers the rule delay
# kept up at 16 bench workers (p99 1.3 s) but not at 32 (p50 4.8 s); at 16
# it kept up at both (p99 1.4 s). The Python trial: with 3 projection
# workers, one per read model, the payment file's catch-up at 16 workers
# fell from 345 s with one worker to 100 s; at 8 rule workers the real-time
# rule delay kept up at 16 and 32 bench workers (p99 3.4 s and 3.0 s), at 4
# it fell behind (p99 23 s), and 16, one process each, crowded the engine's
# 2 CPUs (p99 14 s). Re-tested on 2026-10-10 at 500,000 accounts with the
# engine-readiness wait: at 4 rule workers the rule delay's p99 was 25 s at 8
# bench workers and 18 s at 16; at 8 it was 5.3 s and 7.9 s, so 8 stays.
# Neither keeps fully up: the engine was at its 2-CPU limit about 90 percent
# of the time either way. The csharp-wax trial, with 3 projection workers: at 8
# rule workers the rules fell behind at 32 bench workers (p50 4.2 s); at 16
# they kept up at 16 and 32 (p99 0.7 s and 1.9 s).
TUNED = {
    "csharp": "--rule-workers 16",
    "csharp-direct": "--rule-workers 16",
    "csharp-wax": "--projection-workers 3 --rule-workers 16",
    "python": "--projection-workers 3 --rule-workers 8",
}
# The first pass ran every side with these, untuned.
UNTUNED = "--rule-workers 4"


def run(group, side, workload, workers, diagnostic=None, block=None, suffix=None):
    entry = {"group": group, "side": side, "workload": workload, "workers": workers, "diagnostic": diagnostic}
    if block:
        entry["block"] = block
    if suffix:
        entry["suffix"] = suffix
    return entry


def triples(group, suffix=None):
    """The diagnostics as back-to-back triples at 8 workers, below Python's
    bench cap: a normal run, the engine paused, and hashing off, on each side."""
    return [run(group, side, "batch-payments", 8, diagnostic, block=side, suffix=suffix)
            for side in SIDES for diagnostic in ("normal", "no-engine", "hash-off")]


def third_runs():
    planned = []
    # 1. Every run the second pass skipped for time, to complete its plan.
    status = json.loads((HERE / "second-pass.status.json").read_text(encoding="utf-8"))
    earlier = {entry["label"]: entry for entry in
               json.loads((HERE / "second-pass.plan.json").read_text(encoding="utf-8"))["runs"]}
    for record in status["runs"]:
        if record["state"].startswith("skipped"):
            entry = earlier[record["label"]]
            planned.append(run(1, entry["side"], entry["workload"], entry["workers"], entry["diagnostic"]))
    # 2. The diagnostics again, as triples at 8 workers.
    planned += triples(2)
    # 3. The core comparison again, for a second sample.
    planned += [dict(entry, group=3) for entry in laptop_runs() if entry["group"] == 1]
    # 4. A second round of triples, if time is left.
    planned += triples(4, suffix="r2")
    return planned


def laptop_runs():
    planned = []
    # 1. The core comparison: the batch jobs at the worker counts the first
    # pass left out, the real-time mix at every count under the corrected
    # method, and the side without Marten at the first pass's 4 and 16.
    for side in SIDES:
        for workload in ("batch-payments", "batch-accrual"):
            for workers in (8, 32):
                planned.append(run(1, side, workload, workers))
        for workers in (4, 8, 16, 32):
            planned.append(run(1, side, "realtime", workers))
    for workload in ("batch-payments", "batch-accrual"):
        for workers in (4, 16):
            planned.append(run(1, "csharp-wax", workload, workers))
    # 2. The diagnostics and 3. the bulk workloads, as on the PC.
    planned += [entry for entry in pc_runs() if entry["group"] in (2, 3)]
    # 4. The real-time mix with the engines untuned, as in the first pass but
    # under the corrected method, to show what tuning changed. Optional.
    for side in SIDES:
        for workers in (4, 16):
            planned.append(run(4, side, "realtime", workers, "untuned"))
    # 5. The first pass's batch runs again, for a second sample, if time is left.
    for side in ("csharp", "csharp-direct", "python"):
        for workload in ("batch-payments", "batch-accrual"):
            for workers in (4, 16):
                planned.append(run(5, side, workload, workers))
    return planned


def pc_runs():
    planned = []
    # 1. The core comparison: both batch jobs and the real-time mix at every
    # worker count, on every side.
    for side in SIDES:
        for workload in ("batch-payments", "batch-accrual", "realtime"):
            for workers in (4, 8, 16, 32):
                planned.append(run(1, side, workload, workers))
    # 2. The diagnostics, on the payment file at 16 workers.
    for side in SIDES:
        planned.append(run(2, side, "batch-payments", 16, "no-engine"))
        planned.append(run(2, side, "batch-payments", 16, "hash-off"))
    # 3. The bulk workloads.
    for side in BULK_SIDES:
        for workload in ("batch-payments-bulk", "placement", "placement-bulk"):
            for workers in (4, 16):
                planned.append(run(3, side, workload, workers))
    # 4. The real-time mix with the engines untuned, as in the first pass,
    # to show what tuning changed.
    for side in SIDES:
        for workers in (4, 16):
            planned.append(run(4, side, "realtime", workers, "untuned"))
    return planned


def label(name, entry):
    parts = [name, entry["side"], entry["workload"], f"w{entry['workers']}"]
    return "-".join(parts + [part for part in (entry["diagnostic"], entry.get("suffix")) if part])


def main():
    name, seed = sys.argv[1], int(sys.argv[2])
    layout = sys.argv[3] if len(sys.argv) > 3 else "pc"
    rng = random.Random(seed)
    planned = {"laptop": laptop_runs, "third": third_runs, "pc": pc_runs}[layout]()
    ordered = []
    for group in sorted({entry["group"] for entry in planned}):
        members = [entry for entry in planned if entry["group"] == group]
        if not any("block" in entry for entry in members):
            rng.shuffle(members)
            ordered.extend(members)
            continue
        blocks = {}
        for entry in members:
            blocks.setdefault(entry.get("block") or id(entry), []).append(entry)
        blocks = list(blocks.values())
        rng.shuffle(blocks)
        for block in blocks:
            rng.shuffle(block)
            ordered.extend(block)
    for index, entry in enumerate(ordered, start=1):
        entry["order"] = index
        entry["label"] = label(name, entry)
        entry["engine_args"] = UNTUNED if entry["diagnostic"] == "untuned" else TUNED[entry["side"]]
    plan = {
        "name": name, "seed": seed, "layout": layout, "accounts": ACCOUNTS, "placements": PLACEMENTS,
        "warmup_s": WARMUP_S, "duration_s": DURATION_S, "catchup_timeout_s": CATCHUP_TIMEOUT_S,
        "engine_args": {"tuned": TUNED, "untuned": UNTUNED}, "runs": ordered,
    }
    (HERE / f"{name}.plan.json").write_text(json.dumps(plan, indent=1) + "\n", encoding="utf-8", newline="\n")
    lines = [
        f"# {name} run order",
        "",
        f"Generated by `py -3 make_plan.py {name} {seed}{'' if layout == 'pc' else ' ' + layout}`. Runs go in this order, one at a time; each priority group is shuffled separately with the seed {seed}, and a run that cannot finish before the window ends is skipped, so the lowest group drops first.",
        "",
        "| Order | Group | Side | Workload | Workers | Diagnostic | Engine |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in ordered:
        lines.append(f"| {entry['order']} | {entry['group']} | {entry['side']} | {entry['workload']} | {entry['workers']} "
                     f"| {entry['diagnostic'] or ''} | `{entry['engine_args']}` |")
    (HERE / f"{name}.plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(ordered)} runs written")


if __name__ == "__main__":
    main()
