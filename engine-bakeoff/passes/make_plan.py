"""Writes a measured pass's run list: every run, its priority, and its place in a
shuffled order, from a recorded seed, so that anyone can regenerate the same
list. The plan is committed before the pass starts.

    py -3 make_plan.py second-pass 20261009 laptop

writes second-pass.plan.json and second-pass.plan.md beside this file. The
third argument picks the layout: "laptop" for passes on the laptop, which
has the first pass as its baseline; "third" for the pass after the second,
which carries over the second pass's skipped runs; "pc", the default, for
the full grid a separate benchmark PC would need; or one of the AWS layouts,
"aws-anchor" at the standard caps, "aws-scaled" at the scaled caps,
"aws-pgbouncer" at the scaled caps through PgBouncer, and "aws-5m" at the
scaled caps with 5,000,000 accounts; or "efcore", the EF Core mini-pass,
whose fourth argument is the commit it measures:

    py -3 make_plan.py efcore-pass 20261011 efcore <commit>

or "round2-laptop", round 2's pass on the laptop, which also takes the
commit it measures as its fourth argument; or "round2-shape", round 2's
code-shape pass, which does too; or "round2-diag", round 2's diagnostic pass,
which does too; or "round2-fix", the pass that tests the reader fix after
it, which does too; or "round2-followup", round 2's follow-up tests after
the fix pass, which does too; or "round2-flush", the night that times the
WAL flush, which does too.

Runs that share a block stay together, back to back: the blocks are shuffled
within their group, and the runs within each block. A group laid out as a
Latin square, or whose runs carry a fixed position, is the exception.
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
    # Round 2's diagnostic pass: csharp-wax with Python's 8 rule workers, to
    # test whether its 16 add contention in the database.
    "csharp-wax-rw8": "--projection-workers 3 --rule-workers 8",
}
# The first pass ran every side with these, untuned.
UNTUNED = "--rule-workers 4"

# The engines' settings at the scaled caps, where the engine has 4 CPUs
# instead of 2 and the bench drives up to 64 workers. Provisional: each is
# confirmed by a short trial on the AWS host before the scaled pass, and the
# AWS host's runbook, which is not part of this repository, says how to
# change one.
SCALED_TUNED = {
    "csharp": "--rule-workers 32",
    "csharp-direct": "--rule-workers 32",
    "csharp-wax": "--projection-workers 3 --rule-workers 32",
    "python": "--projection-workers 3 --rule-workers 16",
}


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


def aws_anchor_runs():
    # Profile (a): the core comparison at the standard caps on a 32-vCPU host,
    # to see whether the laptop's ordering of the sides holds on server CPUs.
    return [run(1, side, workload, workers) for side in SIDES
            for workload in ("batch-payments", "batch-accrual", "realtime") for workers in (4, 8, 16)]


def aws_scaled_runs():
    # Profile (b): where each side stops scaling with a 16-CPU database. The
    # bulk workloads are where 6,900 commands per second is plausible on one
    # database, so they come second, at a low and a high worker count.
    planned = [run(1, side, workload, workers) for side in SIDES
               for workload in ("batch-payments", "realtime") for workers in (8, 16, 32, 64)]
    planned += [run(2, side, workload, workers) for side in BULK_SIDES
                for workload in ("batch-payments-bulk", "placement-bulk") for workers in (8, 32)]
    return planned


def aws_pgbouncer_runs():
    # The scaled pass's top worker counts again through PgBouncer with a small
    # transaction pool, to separate each side's ceiling from the contention
    # that many concurrent sessions cause.
    return [run(1, side, workload, workers) for side in SIDES
            for workload in ("batch-payments", "realtime") for workers in (32, 64)]


def aws_5m_runs():
    # A later check on the C# sides only: does a working set ten times larger,
    # beyond shared_buffers, change the cost per command?
    planned = [run(1, side, "batch-payments-bulk", workers) for side in ("csharp-direct", "csharp-wax")
               for workers in (8, 32)]
    planned += [run(1, side, "realtime", workers) for side in ("csharp", "csharp-direct", "csharp-wax")
                for workers in (16, 32)]
    return planned


# A mode side runs with the engine settings of the side it is a mode of.
MODE_BASE = {"csharp-wax-ef": "csharp-wax", "csharp-wax-batched": "csharp-wax", "python-direct": "python",
             "csharp-wax-noreset": "csharp-wax", "csharp-wax-prepared": "csharp-wax",
             "csharp-wax-fair-batched": "csharp-wax", "csharp-wax-fair-prepared": "csharp-wax",
             "csharp-wax-npgsql-defaults": "csharp-wax", "csharp-wax-autoprepare": "csharp-wax",
             "csharp-wax-custom-plans": "csharp-wax", "csharp-wax-rw8-sync-off-diagnostic": "csharp-wax-rw8"}

# The EF Core mini-pass's configurations, all at 8 workers: the payment
# file, real time, the accrual, and the payment file with the engine paused,
# which isolates the command path.
EFCORE_CONFIGS = [("batch-payments", None), ("realtime", None), ("batch-accrual", None), ("batch-payments", "no-engine")]


def efcore_runs():
    """Plain csharp-wax against its EF Core mode, csharp-wax-ef. Each block is a
    pair run back to back, its order drawn from the seed: three plain-and-EF
    blocks per configuration, and one plain-against-plain block that measures
    the noise floor. Placement, which also runs through the handlers, is one
    optional block last."""
    planned = []
    for workload, diagnostic in EFCORE_CONFIGS:
        key = f"{workload}-{diagnostic or 'normal'}"
        for n in (1, 2, 3):
            for side in ("csharp-wax", "csharp-wax-ef"):
                planned.append(run(1, side, workload, 8, diagnostic, block=f"{key}-r{n}", suffix=f"r{n}"))
        for n in (1, 2):
            planned.append(run(1, "csharp-wax", workload, 8, diagnostic, block=f"{key}-noise", suffix=f"noise{n}"))
    for side in ("csharp-wax", "csharp-wax-ef"):
        planned.append(run(2, side, "placement", 8, block="placement-r1", suffix="r1"))
    return planned


# Round 2's pass on the laptop: python-direct, the Python command path in
# plain SQL, against csharp-wax, the C# side without Marten, with the two run
# back to back for each workload and worker count; then Django's Python as a
# same-night anchor at 8 workers; then a second block of each 8-worker pair;
# then both at 32 workers if time allows.
ROUND2_PAIR = ("python-direct", "csharp-wax")
ROUND2_WORKLOADS = ("batch-payments", "batch-accrual", "realtime")


def round2_laptop_runs():
    planned = []
    for workload in ROUND2_WORKLOADS:
        for workers in (4, 8, 16):
            for side in ROUND2_PAIR:
                planned.append(run(1, side, workload, workers, block=f"{workload}-w{workers}-r1", suffix="r1"))
    for workload in ROUND2_WORKLOADS:
        planned.append(run(2, "python", workload, 8, suffix="r1"))
    for workload in ROUND2_WORKLOADS:
        for side in ROUND2_PAIR:
            planned.append(run(3, side, workload, 8, block=f"{workload}-w8-r2", suffix="r2"))
    for workload in ("batch-payments", "realtime"):
        for side in ROUND2_PAIR:
            planned.append(run(4, side, workload, 32, block=f"{workload}-w32-r1", suffix="r1"))
    return planned


# Round 2's code-shape pass: csharp-wax with python-direct's statement shapes
# (csharp-wax-batched) against plain csharp-wax and python-direct, the three
# run back to back for each workload and worker count; then a second block of
# each 8-worker trio; then placement, plain against batched, normally and with
# the engine paused, with a plain-against-plain pair for the noise.
SHAPE_TRIO = ("python-direct", "csharp-wax", "csharp-wax-batched")


def round2_shape_runs():
    planned = []
    for workload in ROUND2_WORKLOADS:
        for workers in (8, 16):
            for side in SHAPE_TRIO:
                planned.append(run(1, side, workload, workers, block=f"{workload}-w{workers}-r1", suffix="r1"))
    for workload in ROUND2_WORKLOADS:
        for side in SHAPE_TRIO:
            planned.append(run(2, side, workload, 8, block=f"{workload}-w8-r2", suffix="r2"))
    for diagnostic in (None, "no-engine"):
        for n in (1, 2, 3):
            for side in ("csharp-wax", "csharp-wax-batched"):
                planned.append(run(3, side, "placement", 8, diagnostic, block=f"placement-{diagnostic or 'normal'}-r{n}", suffix=f"r{n}"))
    for n in (1, 2):
        planned.append(run(3, "csharp-wax", "placement", 8, block="placement-normal-noise", suffix=f"noise{n}"))
    return planned


# Round 2's diagnostic pass: why csharp-wax's event insert costs about twice
# python-direct's per row. The core block runs the payment file at 8 workers
# five ways: csharp-wax unchanged; without the pool's connection reset;
# without it and with statement preparation; with 8 rule workers instead of
# 16; and python-direct. Group 2 runs the same five in the reverse order of
# group 1. The later groups are the extras, in the order they are worth
# running, so that the runner drops the least useful when time runs short.
DIAG_CORE = ("csharp-wax", "csharp-wax-noreset", "csharp-wax-prepared", "csharp-wax-rw8", "python-direct")
DIAG_TRIO = ("csharp-wax", "csharp-wax-prepared", "python-direct")
DIAG_FAIR = ("csharp-wax-fair-prepared", "csharp-wax-prepared")


def round2_diag_runs():
    planned = [run(1, side, "batch-payments", 8, block="core-r1", suffix="r1") for side in DIAG_CORE]
    planned += [dict(run(2, side, "batch-payments", 8, block="core-r2", suffix="r2"), mirror="core-r1")
                for side in DIAG_CORE]
    # 3. The fair batched mode against the same settings without it, on the
    # payment file and on placement with the engine paused.
    for n in (1, 2):
        planned += [run(3, side, "batch-payments", 8, block=f"fair-payments-f{n}", suffix=f"f{n}") for side in DIAG_FAIR]
        planned += [run(3, side, "placement", 8, "no-engine", block=f"fair-placement-f{n}", suffix=f"f{n}")
                    for side in DIAG_FAIR]
    # 4. Higher concurrency, where round 2 found the largest gaps.
    for workload in ("batch-payments", "realtime"):
        for workers in (16, 32):
            planned += [run(4, side, workload, workers, block=f"{workload}-w{workers}-r1", suffix="r1") for side in DIAG_TRIO]
    # 5. The other two workloads at 8 workers.
    for workload in ("batch-accrual", "realtime"):
        planned += [run(5, side, workload, 8, block=f"{workload}-w8-r1", suffix="r1") for side in DIAG_TRIO]
    # 6. A third core block, in its own order.
    planned += [run(6, side, "batch-payments", 8, block="core-r3", suffix="r3") for side in DIAG_CORE]
    # 7. A second sample of each higher-concurrency block.
    for workload in ("batch-payments", "realtime"):
        for workers in (16, 32):
            planned += [run(7, side, workload, workers, block=f"{workload}-w{workers}-r2", suffix="r2") for side in DIAG_TRIO]
    # 8. One worker with the engine paused, the least contended command path,
    # last because each run takes about an hour.
    planned += [run(8, side, "batch-payments", 1, "no-engine", block="single-r1", suffix="r1")
                for side in ("csharp-wax-prepared", "python-direct")]
    return planned


# The pass that tests the reader fix and settles what the diagnostic pass left
# open. Group 1 runs the payment file at 8 workers in one back-to-back block
# of driver settings: csharp-wax with its own settings, with Npgsql's
# defaults, with each setting alone, and with custom plans forced. Group 2 is
# the fix: the configuration that failed twice in 20 runs, csharp-wax-rw8 on
# the payment file at 8 workers, in six blocks of six, each block five plain
# runs and one with a sleep after small reader passes, in an order drawn from
# the seed. The second one-worker pair is left for another evening.
FIX_DRIVER_BLOCK = ("csharp-wax", "csharp-wax-npgsql-defaults", "csharp-wax-noreset", "csharp-wax-autoprepare",
                    "csharp-wax-custom-plans")
FIX_BLOCKS, FIX_PLAIN_PER_BLOCK = 6, 5
# Engine arguments the layout adds: every C# run with the engine prints its
# readers' pass counts every 10 seconds, and the small-pass runs sleep 50 ms
# after a pass that applies fewer than 100 events.
FIX_PASS_REPORT = "--pass-report-seconds 10"
FIX_SMALL_PASS = "--small-pass-events 100 --small-pass-sleep-ms 50"


def round2_fix_runs():
    planned = [run(1, side, "batch-payments", 8, block="drivers-d1", suffix="d1") for side in FIX_DRIVER_BLOCK]
    for n in range(1, FIX_BLOCKS + 1):
        planned += [run(2, "csharp-wax-rw8", "batch-payments", 8, block=f"fix-b{n}", suffix=f"b{n}-{k}")
                    for k in range(1, FIX_PLAIN_PER_BLOCK + 1)]
        planned.append(run(2, "csharp-wax-rw8", "batch-payments", 8, "small-pass-sleep", block=f"fix-b{n}", suffix=f"b{n}"))
    return planned


def round2_fix_engine_args(entry, args):
    if entry["side"].startswith("csharp-wax") and entry["diagnostic"] != "no-engine":
        args = f"{args} {FIX_PASS_REPORT}"
    if entry["diagnostic"] == "small-pass-sleep":
        args = f"{args} {FIX_SMALL_PASS}"
    return args


# Round 2's follow-up tests after the fix pass. Group 1 confirms or refutes the
# custom-plans indication: csharp-wax with its own settings, with custom plans
# forced, and with Max Auto Prepare alone, in three blocks laid out as a Latin
# square. Group 2 runs the configuration with the most runs in both rate
# regimes again and again, for the side sampler to watch. Group 3 is the second
# one-worker pair, in the reverse of the first pair's order. Group 4 fills
# whatever time is left with more of group 2.
FOLLOWUP_PLAN_ARMS = ("csharp-wax", "csharp-wax-custom-plans", "csharp-wax-autoprepare")
FOLLOWUP_PLAN_BLOCKS = 3
FOLLOWUP_REGIME_RUNS, FOLLOWUP_FILL_RUNS = 16, 12
FOLLOWUP_SINGLE = ("csharp-wax-prepared", "python-direct")


def round2_followup_runs():
    planned = []
    for n in range(1, FOLLOWUP_PLAN_BLOCKS + 1):
        planned += [dict(run(1, side, "batch-payments", 8, block=f"plans-c{n}", suffix=f"c{n}"), latin=True)
                    for side in FOLLOWUP_PLAN_ARMS]
    planned += [run(2, "csharp-wax-rw8", "batch-payments", 8, suffix=f"s{k}") for k in range(1, FOLLOWUP_REGIME_RUNS + 1)]
    planned += [dict(run(3, side, "batch-payments", 1, "no-engine", block="single-r2", suffix="r2"), position=k)
                for k, side in enumerate(FOLLOWUP_SINGLE)]
    planned += [run(4, "csharp-wax-rw8", "batch-payments", 8, suffix=f"f{k}") for k in range(1, FOLLOWUP_FILL_RUNS + 1)]
    return planned


# Round 2's flush-timing night: PostgreSQL times every WAL write and sync, and
# the storage, the host's disk, and its processes are read beside each run.
# Group 1 asks whether the high level belongs to being first or to custom
# plans, with csharp-wax-rw8 first this time. Group 2 is four idle gaps, each
# followed by one csharp-wax-rw8 run. Group 3 is the one diagnostic with
# synchronous_commit off for the bench's sessions, between two csharp-wax-rw8
# runs. Group 4 runs csharp-wax-rw8 back to back to the end of the night.
# Every group runs in its fixed order.
FLUSH_GAPS, FLUSH_GAP_MINUTES, FLUSH_FILL_RUNS = 4, 25, 40
FLUSH_DISK = {"physical": "0 C:", "logical": "C:"}


def idle(group, minutes, suffix):
    return {"group": group, "kind": "idle", "minutes": minutes, "side": "idle", "workload": "idle", "workers": 0,
            "diagnostic": None, "suffix": suffix}


def round2_flush_runs():
    planned = [run(1, "csharp-wax-rw8", "batch-payments", 8, suffix="a1"),
               run(1, "csharp-wax-custom-plans", "batch-payments", 8, suffix="a2")]
    for k in range(1, FLUSH_GAPS + 1):
        planned += [idle(2, FLUSH_GAP_MINUTES, f"g{k}"), run(2, "csharp-wax-rw8", "batch-payments", 8, suffix=f"b{k}")]
    planned += [run(3, "csharp-wax-rw8", "batch-payments", 8, suffix="d1"),
                run(3, "csharp-wax-rw8-sync-off-diagnostic", "batch-payments", 8, "sync-off", suffix="d2"),
                run(3, "csharp-wax-rw8", "batch-payments", 8, suffix="d3")]
    planned += [run(4, "csharp-wax-rw8", "batch-payments", 8, suffix=f"c{k}") for k in range(1, FLUSH_FILL_RUNS + 1)]
    for group in {entry["group"] for entry in planned}:
        for position, entry in enumerate(entry for entry in planned if entry["group"] == group):
            entry["position"] = position
    return planned


# Lines a layout adds to its plan's Markdown, under the generation line.
NOTES = {
    "efcore": [
        "The measured commit is `{measured_commit}`: the EF Core mode with the tuning the design allows (DbContext pooling, compiled queries, a compiled model, and no-tracking reads), merged with the published `main`. Before the first run the full test suite passes in both modes on that commit, the twin test included, and its output is kept beside this plan.",
        "",
        "Every configuration runs at 8 workers and 500,000 accounts with `--projection-workers 3 --rule-workers 16` in both modes. Group 1 is 16 blocks of two runs; group 2, one placement block, runs only if time allows. Every gate must pass in every run.",
        "",
        "The Foundation approved these criteria before any run. Each is judged on the median of a configuration's three blocks, and a difference smaller than the same configuration's plain-against-plain difference is reported as no difference. EF Core earns its keep on the command path only if it meets all of them:",
        "",
        "- database CPU per command within +10 percent of plain;",
        "- application CPU per command within +50 percent of plain, or within +1.0 ms;",
        "- commands per second within -10 percent of plain;",
        "- command latency at the 99th percentile within +25 percent of plain;",
        "- statements per command reported, with no threshold.",
        "",
        "Thirteen containers and development services unrelated to the bake-off stay up through the pass; every run records them, and the pass's figures compare only with each other.",
    ],
    "round2-laptop": [
        "Round 2's pass on the laptop, approved on 2026-11-12, in the window from 22:00 that night to 07:00 on 2026-11-13. The measured tree is `{measured_commit}`: the EF Core mini-pass's branch (`d7956db`, whose plain csharp-wax is the code that pass measured in plain mode, with EF Core's packages in the image but not used) with `round2/python-direct` (`08dd8c5`) merged in. The plan commit adds only the plan files and the generator's layout, and no code changes after it.",
        "",
        "The sides are python-direct, the Python command path in plain SQL with its rule runners and register pollers off Django, and csharp-wax, the C# side without Marten. Each workload and worker count is a block of the two run back to back, in an order drawn from the seed. Django's Python runs once per workload at 8 workers as a same-night anchor. Group 3 repeats every 8-worker block, and group 4 runs both sides at 32 workers if time allows. The Marten sides and the EF Core mode are not in this pass. The bulk workloads and placement are left out because python-direct does not implement them.",
        "",
        "The tree's Python code is `08dd8c5`'s, whose 97 tests pass, and its C# code is `d7956db`'s, whose 39 tests pass in both state modes (see efcore-pass.tests.txt). The harness differs from `08dd8c5`'s only by the EF Core mode's entry in sides.json.",
        "",
        "Every run uses 500,000 accounts and the standard caps. python-direct uses Python's engine settings. Every gate must pass in every run. Thirteen containers and several development services unrelated to the bake-off stay up through the pass, as they did in the EF Core mini-pass; every run records them, and the pass's figures compare only with each other.",
    ],
    "round2-shape": [
        "Round 2's code-shape pass on the laptop, approved on 2026-11-13, in the window from 18:00 that day to 07:00 on 2026-11-14. It asks how much of the difference between python-direct and csharp-wax in the laptop pass comes from how each writes its SQL rather than from the language. The measured tree is `{measured_commit}`: the laptop pass's branch (`4c8e5ea`) with `round2/csharp-batched` merged in, which adds csharp-wax-batched, a mode of csharp-wax (`WAX_STATE=batched`) that writes a command's events in one multi-row insert and its account and bucket changes in set-based statements, as python-direct does. Plain csharp-wax and python-direct are unchanged from the laptop pass. The plan commit adds only the plan files and the generator's layout, and no code changes after it.",
        "",
        "Group 1 runs python-direct, csharp-wax, and csharp-wax-batched back to back for the payment file, the accrual, and real time at 8 and 16 workers, in an order drawn from the seed. Group 2 repeats every 8-worker trio. Group 3 runs placement at 8 workers for plain and batched csharp-wax, three blocks normally and three with the engine paused, which isolates the command path, and a plain-against-plain pair that measures the noise. python-direct does not implement placement. The bulk workloads, the Marten sides, and the EF Core mode are not in this pass.",
        "",
        "Every run uses 500,000 accounts and the standard caps, and both C# modes use csharp-wax's engine settings. Every gate must pass in every run. Fourteen containers and several development services unrelated to the bake-off stay up through the pass, as they did in the laptop pass; every run records them, and the pass's figures compare only with each other and with that pass.",
    ],
    "round2-diag": [
        "Round 2's diagnostic pass on the laptop, approved on 2026-11-14, in the window from 17:30 that day to 08:00 on 2026-11-15. The code-shape pass found that csharp-wax's event insert costs about twice python-direct's per row, with the same columns, table, trigger, and WAL per command. This pass tests the suspects that remain. Npgsql returns each pooled connection with DISCARD ALL, which also discards the session's cached plans, including those of the event trigger and the foreign-key check, so they are planned again inside the next insert. Npgsql also prepares no statement by default, where psycopg prepares one after five uses. csharp-wax runs 16 rule workers to Python's 8. The measured tree is `{measured_commit}`: the published code-shape pass (`47d3b79`) with two driver settings csharp-wax reads from the environment, the MultiXact caches of pg_stat_slru added to every run's database activity, and a fair batched mode of csharp-wax (`WAX_STATE=fair-batched`) that uses a single-row statement for a single row and writes only the account columns a command changed. The plan commit adds only the plan files, the test record, and the generator's layout, and no code changes after it.",
        "",
        "The sides are modes of csharp-wax in its Compose project: csharp-wax-noreset sets No Reset On Close; csharp-wax-prepared also sets Max Auto Prepare to 20; csharp-wax-rw8 runs 8 rule workers; and csharp-wax-fair-prepared is the fair batched mode with both settings. python-direct is unchanged and runs as the anchor in the core blocks and in every block of three; the fair batched blocks compare two C# modes only.",
        "",
        "Before group 1, [reset_check.py](reset_check.py) times one event insert in csharp-wax's database straight after DISCARD ALL and with the session's plans warm, in a rolled-back transaction on a throwaway copy of the template, and its output is kept with the pass as `round2-diag.reset-check.txt`.",
        "",
        "Group 1 runs the payment file at 8 workers five ways in an order drawn from the seed: csharp-wax unchanged, csharp-wax-noreset, csharp-wax-prepared, csharp-wax-rw8, and python-direct. Group 2 runs the same five in exactly the reverse order. Groups 3 to 8 are extras in the order they are worth running: the fair batched mode against csharp-wax-prepared on the payment file and on placement with the engine paused; csharp-wax, csharp-wax-prepared, and python-direct at 16 and 32 workers on the payment file and real time; the same three on the accrual and real time at 8 workers; a third core block; a second sample of each 16- and 32-worker block; and csharp-wax-prepared against python-direct at 1 worker with the engine paused. The runner drops what would not finish before its deadline, from the end.",
        "",
        "Every run uses 500,000 accounts and the standard caps. Every gate must pass in every run. Fourteen containers and several development services unrelated to the bake-off stay up through the pass, as they did in the earlier round 2 passes; every run records them. The laptop ran at different speeds on different evenings, so only comparisons within a back-to-back block are sound, and the pass's absolute figures do not compare with earlier passes.",
    ],
    "round2-fix": [
        "The pass that tests the reader fix after round 2's diagnostic pass, approved on 2026-11-15, in the window from 22:00 that day to 07:00 on 2026-11-16. The diagnostic pass and its follow-up repeats found that csharp-wax-rw8 on the payment file at 8 workers failed a read model gate in 2 of 20 runs, because the reader's high-water mark read the sequence without its lock and could run up to 32 numbers ahead for a moment. The measured tree is `{measured_commit}`: the published `main` (`9eacdbb`, the diagnostic pass and its sequence-read test) with the fix, which reads the mark with `pg_sequence_last_value()` and never moves a checkpoint back on a claim, and with No Reset On Close and Max Auto Prepare 20 as csharp-wax's own settings. Every earlier mode keeps the driver settings it was measured with, csharp-wax-rw8 included, so the fix is tested in exactly the configuration that failed. The plan commit adds only the plan files, the test record, and the generator's layout, and no code changes after it.",
        "",
        "Before group 1, [reset_check.py](reset_check.py) runs again on a throwaway copy of csharp-wax's template, with nothing else running: step 0, timed cleanly this time, and the split of DISCARD ALL into the commands it stands for. Its output is kept with the pass as `round2-fix.reset-check.txt`.",
        "",
        "Group 1 runs the payment file at 8 workers in one back-to-back block of five csharp-wax driver settings, in an order drawn from the seed: csharp-wax with its own settings; csharp-wax-npgsql-defaults, as earlier passes ran it; csharp-wax-noreset and csharp-wax-autoprepare, each setting alone, which the diagnostic pass did not measure apart, although Max Auto Prepare alone also switches Npgsql to a reset that keeps cached plans once a connection has a statement prepared, so it does not measure preparation apart from plan retention; and csharp-wax-custom-plans, which forces custom plans to test whether a generic plan made the prepared event insert slower. One block is an indication, not a result.",
        "",
        "Group 2 is the fix: 30 plain runs of csharp-wax-rw8 on the payment file at 8 workers, in six blocks of six, each with one more run that sleeps a reader 50 ms after a pass that applies fewer than 100 events. The plain runs ask whether the fix holds: if the failure rate were still near 2 in 20, 30 clean runs would happen by chance about 4 percent of the time. The small-pass runs test a hypothesis about the follow-up repeats that ran slower: that readers close to the head of the stream make many small passes, whose fixed cost slows the bench. Every C# run with the engine prints its readers' passes and events every 10 seconds, so each run's pass rate can be followed over time. A second one-worker pair with the engine paused would take about two and a half hours, and is left for another evening.",
        "",
        "Every run uses 500,000 accounts and the standard caps. Every gate must pass in every run. The harness's own reads of the head of the stream, in both sides' progress views, still read the sequence without its lock, so a lag sample or the catch-up target can read up to 32 high for a moment. They decide only when the harness stops waiting for catch-up, never what a gate checks, and changing them would change both sides' measuring method, migrations, and seed templates, so they wait for a later change. A run that fails or stalls on catch-up is checked for this before anything else. Fourteen containers and several development services unrelated to the bake-off stay up through the pass, as they did in the earlier round 2 passes; every run records them. Only comparisons within a back-to-back block are sound.",
    ],
    "round2-followup": [
        "Round 2's follow-up tests on the laptop, approved on 2026-11-16, in the window from 22:00 that day to 07:00 on 2026-11-17. The fix pass left two questions open. Its one block of driver settings suggested that forcing custom plans lowers the execution time of csharp-wax's event insert, and its runs moved abruptly between two rate regimes, about 2,700 events per second and about 2,000, for a cause it could not see. The measured tree is `{measured_commit}`: the published `main` (`436df6f`, tag `round-2-fix`) with diagnostics added and the command path unchanged. After its timer stops, the C# bench reads from pg_prepared_statements how often PostgreSQL chose a generic or a custom plan for each statement prepared on each of its own connections, and records it with the driver settings and the reset Npgsql gives a connection returned to the pool. The harness can run a side sampler, the runner can log the host's processor counters, and [plan_check.py](plan_check.py) times an insert like the event insert under each plan_cache_mode setting; all three are described below. The plan commit adds only the plan files, the test record, the generator's layout, and the script that will tabulate the results, [summarize_followup.py](summarize_followup.py), and no measured code changes after it.",
        "",
        "Before group 1, [plan_check.py](plan_check.py) runs on a throwaway copy of csharp-wax's template, with nothing else running. It prepares an insert with the event insert's columns, trigger, and foreign-key check, its values as parameters, settles its plan choice with eight uses, and then times it under EXPLAIN ANALYZE in each of plan_cache_mode's three settings, rotating their order, with the planning time, the BEFORE INSERT trigger, and the foreign-key check read apart. The insert's own plan is trivial, and the trigger runs no query with a parameter, so its plans do not change with the setting. The foreign-key check runs such a query for every event of the payment file, since each carries a reason code; each session caches that plan apart from the statement's, and plan_cache_mode governs it too. With custom plans forced, the check is planned again for every row, and that planning falls inside the check's time. Its output is kept with the pass as `round2-followup.plan-check.txt`.",
        "",
        "Every run of the pass has the side sampler: a connection of its own in the run's database, from the start of the bench to the end of the catch-up. Each second it counts, by wait event, the backends in pg_stat_activity that are not idle; a backend that reports no wait event may be on a core or waiting for one. Every 10 seconds it times a fixed CPU-bound statement, a fifth of the host probe's, and reads the WAL and commit counters, so each of the readers' 10-second intervals holds one probe. Its own cost is measured in every run, from the CPU time its backend and its psql use in the database container; in a smoke run at 20,000 accounts it used about 2 percent of one core, under 1 percent of the database container's CPU. The loop commits after every sample, so the snapshot it holds is never more than about a second old, and the run's database may keep up to a second more of dead row versions than without it. Every run tonight pays both costs alike, and no earlier pass ran the sampler, so tonight's figures are not like-for-like with earlier nights: settings are compared only within tonight, and any comparison with an earlier pass is marked as one across instrumentation.",
        "",
        "Through the whole pass, typeperf logs the Windows host's processor counters every 5 seconds: its speed against nominal, the share of it a power or thermal limit allows and the flags that name the limit, Windows' busy figures, the CPU of the virtual machine Docker runs in, the hypervisor's run time, and each logical processor's load and speed, since the laptop's processor has both performance and efficiency cores. Each C# run with the engine also prints its readers' passes and events every 10 seconds, as in the fix pass, which places each interval in a regime. Under load the probe's time mixes the speed of a core with waiting for one, so the probe alone cannot tell a slower machine from a busier one; the host's counters can. If the slow regime comes from the machine, the host's speed, limit, or spread of load across its two kinds of core should change when a run's rate drops, and the probe should slow with it. If it comes from the database, the host's counters should hold while the share of particular waits, such as WALWrite, WalSync, or a lock, grows; under load those waits are always present, so it is their share that would change.",
        "",
        "Group 1 repeats the custom-plans comparison: csharp-wax with its own settings, csharp-wax-custom-plans, and csharp-wax-autoprepare on the payment file at 8 workers with 16 rule workers, in three back-to-back blocks laid out as a Latin square, so that each goes first, second, and third once. The first block's order is drawn from the seed. The square is cyclic, so within a block each setting always follows the same other one; the database container restarts before every run, which should keep any carryover small. The measure is the event insert's execution time per row. Rates count only where a run stayed in the fast regime, and every rate and insert cost is printed beside the run's place in the run order and its share of slow intervals. A smoke run of csharp-wax-custom-plans showed no generic plan for any statement on any of its connections, so the setting reaches the bench's own sessions. The plan-choice read covers the statements Npgsql prepared, the event insert among them, and not the statements inside the event trigger or the foreign-key check, whose plans are cached apart. Npgsql replaces its least-used statement when Max Auto Prepare's 20 are full, and the replacement's counts start again, so a connection holding 20 may report partial counts.",
        "",
        "The reading is set before the results. A smoke run of csharp-wax-autoprepare already showed every connection's insert taking 5 custom plans and then generic ones. If csharp-wax shows the same, the two made the same plan choice for the insert itself, so that choice cannot explain a difference between them, though the lighter reset still can: the fix pass's reset check found that it adds about 0.06 ms to the next insert. The measure leaves out planning. With custom plans forced, the insert is planned again before every execution, outside the measure, while the foreign-key check's planning falls inside it; step 0's planning time is stated beside the result. A trial of step 0 on a copy of a 20,000-account template found forcing custom plans raised both the insert's planning time and its execution time over the default setting, in isolation. If csharp-wax-custom-plans' insert takes less execution time per row than csharp-wax's in all three blocks, the fix pass's indication is repeated; if forcing custom plans made no difference, that would still happen one time in eight. If it takes more in any block, the indication is not repeated, and the fix pass's lower figure stays a single sample. If the blocks repeat the indication while step 0 again finds custom plans dearer in isolation, the plan choice within a session is not shown to be the mechanism.",
        "",
        "Group 2 is 16 runs of csharp-wax-rw8 on the payment file at 8 workers, with Npgsql's defaults and 8 rule workers, the configuration with the most runs in both regimes. No number of runs can promise to catch the slow regime, which has come and gone in stretches of about half an hour to nearly two hours, so every run of the pass carries the sampler, and group 4 adds up to 12 more runs of the same configuration in whatever time is left at the end.",
        "",
        "Group 3 is the second one-worker pair, which the fix pass left for another evening: csharp-wax-prepared and python-direct on the payment file at 1 worker with the engine paused, in the reverse of the first pair's order in the diagnostic pass. Neither side's command path has changed since that pair, and the reader fix does not run with the engine paused, but that pair ran without the sampler, so the two sides are compared with each other first, and any comparison with the first pair is marked as one across instrumentation. Each run took over an hour then. The runner starts a run only if its estimate fits before the deadline, and it goes in plan order, so neither group 3 nor group 4 can cut groups 1 or 2 short.",
        "",
        "Every run uses 500,000 accounts and the standard caps. Every gate must pass in every run. The runner starts no run whose estimate, the longest earlier run of its kind plus 15 percent, or the plan's rough estimate where none is on record, would end after 07:00, in group 4 as in every other, and stops any run still going at 07:00. The harness's own reads of the head of the stream still read the sequence without its lock, as in the fix pass, and a run that fails or stalls on catch-up is checked for this before anything else. Fourteen containers and several development services unrelated to the bake-off stay up through the pass, as they did in the earlier round 2 passes; every run records them. Only comparisons within a back-to-back block, or between the intervals of one run, are sound.",
    ],
    "round2-flush": [
        "Round 2's flush-timing night on the laptop, approved on 2026-11-17, in the window from 22:00 that day to 07:00 on 2026-11-18. Every round 2 pass on the laptop since the code-shape pass has had runs at more than one level of the write-ahead log's sync rate, and a run's throughput has moved with it ([round2-sync-rates.md](round2-sync-rates.md)). The follow-up tests on the night of 2026-11-16, whose records are not yet published, ran the side sampler in every run, and its counts suggested that at the low sync rate each sync took about twice as long as in that night's first, faster run. That was inferred from the share of the sampler's looks at which a backend waited on a sync, not measured. Tonight measures it, with the storage, the host's disk, and the host's processes read beside every run. The measured tree is `{measured_commit}`: the published `main` (`08049a8`); the follow-up tests' three diagnostic commits, carried over unchanged (`bde034a`, `7e49b76`, and `c2b9ee4`: the bench's plan-choice read, [plan_check.py](plan_check.py), and the side sampler with its own cost measured); and tonight's two commits, which add the measures and the diagnostic side described below (`992399c`) and apply their reviews (`{measured_commit}`). The command path is unchanged. The plan commit adds only the plan files, the test record, the generator's layout, and the script that will tabulate the results, [summarize_flush.py](summarize_flush.py), and no measured code changes after it.",
        "",
        "Four measures are new tonight. First, the database runs with track_wal_io_timing on, given on its command line through the harness's shared settings ([compose.override.yml](../harness/compose.override.yml)), where it is off, PostgreSQL's default, unless a plan asks for it; the runner refuses to start unless the database reports it on. pg_stat_wal then times every WAL write and every sync, the WAL writer's as well as the backends'. Each run records the totals over its bench, and the side sampler adds them every 10 seconds, with the checkpointer's counters, so a run's mean time per sync, and each 10-second interval's, is measured rather than inferred. The setting adds two clock reads to every WAL write and sync, which against syncs of milliseconds is inferred to cost little; it changes the database's configuration, so tonight's runs are compared with each other, and any comparison with an earlier night is marked as one across instrumentation, as the side sampler's already are. Second, before every run and at the start of every idle gap, the runner starts the database alone and runs pg_test_fsync in it, on the data volume, while it is idle, then stops it again, so each run still starts its database fresh. The tool cannot be limited to one method, so the check stops it as soon as it has timed fdatasync, the method PostgreSQL uses here, with one 8 kB write for 2 seconds; before that it writes and syncs its test file once and times open_datasync for 2 seconds. Every check is therefore the same short burst of synced writes, about 5 seconds in a trial, and its start and end are recorded. Its fdatasync rate reads the storage path's state with no load. An idle gap's 25 minutes are counted from the end of its check, with the database stopped again, so the check is never part of the idle time, and the gaps stay comparable with the roughly 24 idle minutes before last night's first run. Third, typeperf adds disk 0's counters every 5 seconds: write latency, the latency of every transfer, writes and bytes written per second, the write queue, and idle time, for the physical disk and its volume, C:. Disk 0 is the 4 TB NVMe drive that holds C:, Docker's virtual disk, Windows, and every other program's files, so its counters include the host's own writes. Windows' disk counters count reads and writes and, as far as its documentation says, not the flush requests a sync becomes, so a flush's own time may not show in them. typeperf also logs the laptop's thermal zones; the drive's own temperature cannot be read without administrator rights and is not recorded. Fourth, every 30 seconds a snapshot records each Windows process's CPU, bytes written, and write operations over the interval, from [process_snapshot.ps1](process_snapshot.ps1). Its raw file names every process on the laptop and is never committed; the committed copy names only the bake-off's own processes, Docker's and WSL's, and Windows' kernel, and shows every other process as unrelated-process, with its process ID, so one process can be followed through the night unnamed. In a trial its query took about 0.9 seconds every 30 seconds and 0.5 to 0.6 seconds of CPU, about 2 percent of one core; most of that CPU falls in the Windows service that answers the query, which the committed copy cannot tell from the others and shows as unrelated-process. Every run also records the server settings that bear on writing and flushing WAL.",
        "",
        "Every run of the pass also has the side sampler, as last night, and each C# run with the engine prints its readers' passes and events every 10 seconds. Each run at 8 workers is marked by PostgreSQL's own mean time per sync over the harness's window, now that it is measured: slow at 4.3 ms or more, below every time the follow-up tests implied for their slow runs, about 5.0 ms and more at 8 workers and 4.3 ms and more at 1 worker, and high at 2.8 ms or less, around the 2.4 ms implied for the high level; a run between is not marked, since no run at the earlier nights' fast level has been timed yet. A run's sync rate depends on whether its configuration keeps the flush busy as well as on the time each sync takes: a smoke run of csharp-wax-rw8 at 20,000 accounts took 2.4 ms per sync at only 275 syncs per second, with the flush busy 65 percent of the time. The earlier nights' sync-rate bands, the high level at 340 or more, fast at 280 to 320, and slow at 200 or less, are therefore printed beside every run as a cross-check, never as its level. Every rate is printed beside its syncs per second, its time per sync, and its busy share: the share of its window in which a sync was in progress, the measured sync time over the window's length, since one process syncs at a time. A run whose database did not report track_wal_io_timing on is not marked, and its measured columns stay empty.",
        "",
        "Group 1 asks whether the high level, about 370 syncs per second, belongs to being first after an idle stretch or to custom plans: the first run of each of the last two nights reached it, and both were csharp-wax-custom-plans. Tonight csharp-wax-rw8 runs first, at 22:00 after the preparation's idle stretch, and csharp-wax-custom-plans second, with the 16 rule workers it ran with before. Group 2 is four cycles of an idle gap of 25 minutes, with every bake-off container stopped, each followed by one csharp-wax-rw8 run. Group 3 is the night's one diagnostic, between two csharp-wax-rw8 runs: csharp-wax-rw8-sync-off-diagnostic is csharp-wax-rw8 whose bench sessions run with synchronous_commit off, so the bench's commits return before their WAL reaches the disk, while the engine's sessions keep it on. It is a diagnostic only, never a configuration to compare or to run: it shows how much faster the bench goes without waiting for the flush, which the earlier nights could not show. Its bench records each of its connections' synchronous_commit, and a smoke run at 20,000 accounts showed off on every bench connection; it is never marked, since its commits do not wait for a sync. Group 4 runs csharp-wax-rw8 back to back until the window ends, to catch moves between levels with the sync time measured. Every group runs in its listed order. Python does not run tonight.",
        "",
        "The readings are set before the results, and [summarize_flush.py](summarize_flush.py) prints each one with its figures. Sync time: while the flush stays busy, the ratio of two levels' sync times follows from their sync rates, so it is not a separate test. This one reading takes its runs by the earlier nights' low band, 200 syncs a second or less, which is where the follow-up tests' slow runs fell, so that a low-rate run with short syncs, the case that would refute their inference, is not left out by the marking. Each run's busy share is taken over its bench's 10-second intervals without the first and last, so the ramps at either end of a bench do not dilute it. If the median time per sync of those runs is 4.3 ms or more and their median busy share 0.9 or more, the low sync rate comes from long syncs, measured, and their time per sync is stated. If the median busy share is below 0.7, the low rate is less demand, not a slower flush, and the follow-up tests' inference fails. Otherwise the night does not decide. Any low-rate run with a time per sync of 2.8 ms or less is named, since each one is a direct counterexample. That holds whether or not any run tonight is marked high; runs marked high are stated beside it with their own time per sync and busy share. In the smoke runs at 20,000 accounts, the ramps took a run's busy share over its whole window down to 0.75 while its steady intervals read 0.95 or more. If no timed run falls in the low band, the reading is not made. The follow-up's method: over each run's side sampler span, the measured time per sync is set against the time the sampler's share of looks in a WAL sync implies. If the two agree within 20 percent in at least 80 percent of the runs at 8 workers, the diagnostic aside, the follow-up's inferred figures stand as estimates; if not, its write-up withdraws them.",
        "",
        "Where the time goes: the median idle fdatasync rate in the checks before runs marked slow is set against the median before runs marked high, with at least two checks on each side. If it is at least 25 percent lower, the storage path is slower even with no load, which places the change below the database, in the virtual disk, the host's file system, or the drive. If it is within 10 percent, the storage path idles at the same speed, and the slowdown comes with load. Between the two, or with fewer than two checks on a side, the night does not decide. Every check is printed in run order, so a drift through the night shows either way. Disk 0's write latency is read beside them: if it rises with the sync time, the drive or the host's storage is implicated; if it holds, that does not clear them, since flush requests may not be counted. Competing load: a move to the slow level is a run marked slow whose predecessor among the timed runs at 8 workers, the diagnostic aside, took at least 25 percent less time per sync, marked or not, tested over the whole span from the end of one bench to the start of the next; or, within one of those runs, an interval marked slow after one that took at least 25 percent less. That catches a move from the earlier nights' fast level as well as from the high one. Each move is set against the 30-second snapshots that overlap it, in which every process not named counts, small ones included. If, for every move, processes unrelated to the bake-off used at least one core more, wrote at least 20 MB per second more, or issued at least 500 write operations per second more than their median for the night in one of those snapshots, competing load is the likelier cause. If that holds for no move, competing load is not shown. Otherwise the night does not decide, and if no move is seen, the reading is not made. As far as Windows' documentation says, it does not count the flush calls a sync makes among a process's write operations.",
        "",
        "The high level, read from the measured time per sync, since a configuration that cannot keep the flush busy syncs less often even when each sync is quick: if the first run, csharp-wax-rw8, is marked high, the high level comes with being first after an idle stretch, not with custom plans. If it is not and the second, csharp-wax-custom-plans, is, custom plans stay a candidate. If neither is, the night does not decide, which covers a high level that needs both. The idle gaps: a gap counts only after a run marked slow, since a gap cannot bring a faster level back when the level is already faster, and it brings a faster level back if the run after it takes at least 25 percent less time per sync than the run before it. If at least three gaps count and a faster level comes back after all of them but at most one, a return after 25 idle minutes is shown; if after none, it is not; if fewer than three count, or otherwise, the night does not decide. The diagnostic: its commands per second are set against those of the run on either side of it, as a percent change, beside each one's time per sync and level, and beside the diagnostic's own sync rate, sync time, catch-up, and largest lag. The engine still commits synchronously and its syncs now carry the bench's WAL too, so a faster bench can leave the engine further behind. The change shows what waiting for the flush costs the bench at that level, for this workload on this laptop; it is not sustained throughput, and it says nothing about any configuration.",
        "",
        "Every run uses 500,000 accounts and the standard caps. Every gate must pass in every run. The runner starts no run or idle gap whose estimate, for a run the longest earlier run of its kind plus 15 percent, would end after 07:00, and stops any run still going at 07:00. The harness's own reads of the head of the stream still read the sequence without its lock, as in the fix pass, and a run that fails or stalls on catch-up is checked for this before anything else. Sixteen containers and several development services unrelated to the bake-off were up when this plan was written and stay up through the pass, as in the earlier round 2 passes; every run records them. Only comparisons within the night, or between the intervals of one run, are sound.",
    ],
}


# Each layout's runs and the plan settings that differ from the defaults.
LAYOUTS = {
    "laptop": (lambda: laptop_runs(), {}),
    "third": (lambda: third_runs(), {}),
    "pc": (lambda: pc_runs(), {}),
    "aws-anchor": (aws_anchor_runs, {}),
    "aws-scaled": (aws_scaled_runs, {"caps": "scaled"}),
    "aws-pgbouncer": (aws_pgbouncer_runs, {"caps": "scaled", "pool_size": 32}),
    "aws-5m": (aws_5m_runs, {"caps": "scaled", "accounts": 5_000_000}),
    "efcore": (efcore_runs, {}),
    "round2-laptop": (round2_laptop_runs, {}),
    "round2-shape": (round2_shape_runs, {}),
    "round2-diag": (round2_diag_runs, {}),
    "round2-fix": (round2_fix_runs, {}),
    "round2-followup": (round2_followup_runs, {"side_sampler": True, "host_counters": True}),
    "round2-flush": (round2_flush_runs, {"side_sampler": True, "host_counters": True, "host_disk": FLUSH_DISK,
                                         "wal_io_timing": True, "fsync_check": True, "process_snapshot": True}),
}

# A layout's changes to the engine arguments each run would otherwise get.
ENGINE_ARGS = {"round2-fix": round2_fix_engine_args, "round2-followup": round2_fix_engine_args,
               "round2-flush": round2_fix_engine_args}


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
    if entry.get("kind") == "idle":
        return f"{name}-idle-{entry['suffix']}"
    parts = [name, entry["side"], entry["workload"], f"w{entry['workers']}"]
    return "-".join(parts + [part for part in (entry["diagnostic"], entry.get("suffix")) if part])


def main():
    name, seed = sys.argv[1], int(sys.argv[2])
    layout = sys.argv[3] if len(sys.argv) > 3 else "pc"
    rng = random.Random(seed)
    runs, settings = LAYOUTS[layout]
    if layout in ("efcore", "round2-laptop", "round2-shape", "round2-diag", "round2-fix", "round2-followup",
                  "round2-flush"):
        settings = {**settings, "measured_commit": sys.argv[4]}
    planned = runs()
    tuned = SCALED_TUNED if settings.get("caps") == "scaled" else TUNED
    ordered = []
    for group in sorted({entry["group"] for entry in planned}):
        members = [entry for entry in planned if entry["group"] == group]
        # A group whose runs carry a position runs in that order, unshuffled.
        if all("position" in entry for entry in members):
            ordered.extend(sorted(members, key=lambda entry: entry["position"]))
            continue
        # A Latin square: the first block's order is drawn from the seed, and
        # each later block turns it by one more, so every side takes every place once.
        if any(entry.get("latin") for entry in members):
            blocks = {}
            for entry in members:
                blocks.setdefault(entry["block"], []).append(entry)
            sides = list(dict.fromkeys(entry["side"] for entry in members))
            rng.shuffle(sides)
            for turn, block in enumerate(blocks.values()):
                order = sides[turn % len(sides):] + sides[:turn % len(sides)]
                ordered.extend(sorted(block, key=lambda entry: order.index(entry["side"])))
            continue
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
            # A block that mirrors an earlier one runs its sides in that
            # block's order reversed, instead of in an order of its own.
            mirror = block[0].get("mirror")
            if mirror:
                earlier = [entry["side"] for entry in ordered if entry.get("block") == mirror]
                block.sort(key=lambda entry: earlier[::-1].index(entry["side"]))
            else:
                rng.shuffle(block)
            ordered.extend(block)
    for index, entry in enumerate(ordered, start=1):
        entry["order"] = index
        entry["label"] = label(name, entry)
        if entry.get("kind") == "idle":
            entry["engine_args"] = ""
            continue
        side = entry["side"] if entry["side"] in tuned else MODE_BASE[entry["side"]]
        entry["engine_args"] = UNTUNED if entry["diagnostic"] == "untuned" else tuned[side]
        if layout in ENGINE_ARGS:
            entry["engine_args"] = ENGINE_ARGS[layout](entry, entry["engine_args"])
    plan = {
        "name": name, "seed": seed, "layout": layout, "accounts": settings.get("accounts", ACCOUNTS),
        "placements": PLACEMENTS, "warmup_s": WARMUP_S, "duration_s": DURATION_S,
        "catchup_timeout_s": CATCHUP_TIMEOUT_S,
        **{key: settings[key] for key in ("caps", "pool_size", "measured_commit", "side_sampler", "host_counters",
                                          "host_disk", "wal_io_timing", "fsync_check", "process_snapshot")
           if key in settings},
        "engine_args": {"tuned": tuned, "untuned": UNTUNED}, "runs": ordered,
    }
    (HERE / f"{name}.plan.json").write_text(json.dumps(plan, indent=1) + "\n", encoding="utf-8", newline="\n")
    lines = [
        f"# {name} run order",
        "",
        f"Generated by `py -3 make_plan.py {name} {seed}{'' if layout == 'pc' else ' ' + layout}"
        f"{' ' + settings['measured_commit'] if 'measured_commit' in settings else ''}`. Runs go in this order, one at a time; each priority group is shuffled separately with the seed {seed}, and a run that cannot finish before the window ends is skipped, so the lowest group drops first.",
        "",
        *[line.format(**settings) for line in NOTES.get(layout, [])],
        *([""] if layout in NOTES else []),
        "| Order | Group | Side | Workload | Workers | Diagnostic | Engine |" + (" Block |" if layout in NOTES else ""),
        "| --- | --- | --- | --- | --- | --- | --- |" + (" --- |" if layout in NOTES else ""),
    ]
    for entry in ordered:
        if entry.get("kind") == "idle":
            lines.append(f"| {entry['order']} | {entry['group']} | idle gap, {entry['minutes']} minutes, every bake-off "
                         f"container stopped | | | | |" + (f" {entry['suffix']} |" if layout in NOTES else ""))
            continue
        lines.append(f"| {entry['order']} | {entry['group']} | {entry['side']} | {entry['workload']} | {entry['workers']} "
                     f"| {entry['diagnostic'] or ''} | `{entry['engine_args']}` |"
                     + (f" {entry.get('suffix', '')} |" if layout in NOTES else ""))
    (HERE / f"{name}.plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(ordered)} runs written")


if __name__ == "__main__":
    main()
