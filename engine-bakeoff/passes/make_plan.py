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
it, which does too.

Runs that share a block stay together, back to back: the blocks are shuffled
within their group, and the runs within each block.
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
             "csharp-wax-custom-plans": "csharp-wax"}

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
}

# A layout's changes to the engine arguments each run would otherwise get.
ENGINE_ARGS = {"round2-fix": round2_fix_engine_args}


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
    runs, settings = LAYOUTS[layout]
    if layout in ("efcore", "round2-laptop", "round2-shape", "round2-diag", "round2-fix"):
        settings = {**settings, "measured_commit": sys.argv[4]}
    planned = runs()
    tuned = SCALED_TUNED if settings.get("caps") == "scaled" else TUNED
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
        side = entry["side"] if entry["side"] in tuned else MODE_BASE[entry["side"]]
        entry["engine_args"] = UNTUNED if entry["diagnostic"] == "untuned" else tuned[side]
        if layout in ENGINE_ARGS:
            entry["engine_args"] = ENGINE_ARGS[layout](entry, entry["engine_args"])
    plan = {
        "name": name, "seed": seed, "layout": layout, "accounts": settings.get("accounts", ACCOUNTS),
        "placements": PLACEMENTS, "warmup_s": WARMUP_S, "duration_s": DURATION_S,
        "catchup_timeout_s": CATCHUP_TIMEOUT_S,
        **{key: settings[key] for key in ("caps", "pool_size", "measured_commit") if key in settings},
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
        lines.append(f"| {entry['order']} | {entry['group']} | {entry['side']} | {entry['workload']} | {entry['workers']} "
                     f"| {entry['diagnostic'] or ''} | `{entry['engine_args']}` |"
                     + (f" {entry.get('suffix', '')} |" if layout in NOTES else ""))
    (HERE / f"{name}.plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(ordered)} runs written")


if __name__ == "__main__":
    main()
