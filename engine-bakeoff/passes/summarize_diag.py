"""Summarizes round 2's diagnostic pass (plan 4333ce1, layout round2-diag)
from the recorded results and prints Markdown.

The core block is the payment file at 8 workers, five ways, three times: one
table with every run, then each mode against unchanged csharp-wax within its
back-to-back block. Then the fair batched mode against csharp-wax-prepared;
csharp-wax, csharp-wax-prepared, and python-direct at 16 and 32 workers and
on the accrual and real time at 8; the one-worker pair with the engine paused;
and the MultiXact caches.

    py -3 summarize_diag.py [results folder]

The results folder defaults to the one beside this folder.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "results"
PASS = "round2-diag"
CORE = ("csharp-wax", "csharp-wax-noreset", "csharp-wax-prepared", "csharp-wax-rw8", "python-direct")
TRIO = ("csharp-wax", "csharp-wax-prepared", "python-direct")


def load(side, workload, workers, sample, diagnostic=None):
    middle = f"-{diagnostic}" if diagnostic else ""
    found = sorted(RESULTS.glob(f"*-{PASS}-{side}-{workload}-w{workers}{middle}-{sample}/results.json"))
    if not found:
        return None
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    throttle = run.get("cpu_throttling") or {}
    commands = bench["succeeded"]
    insert_ms = insert_rows = discards = 0
    for statement in run.get("top_statements") or []:
        query = statement["query"].lstrip().upper()
        if query.startswith("INSERT INTO CORE.EVENT"):
            insert_ms += statement["total_ms"]
            insert_rows += statement["rows"]
        elif query.startswith("DISCARD ALL"):
            discards += statement["calls"]
    slru = (run.get("db_activity") or {}).get("slru") or {}
    member, offset = slru.get("multixact_member") or {}, slru.get("multixact_offset") or {}
    return {
        "rate": commands / bench["elapsed_s"],
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "db": cpu.get("db"),
        "statements": run.get("statements_per_command"),
        "discards": discards / commands if commands else None,
        "insert": insert_ms / insert_rows if insert_rows else None,
        "catchup": run.get("catchup_s"),
        "rule_p99": ((run.get("rule_latency_ms") or {}).get("all") or {}).get("p99"),
        "db_thr": (throttle.get("db") or {}).get("throttled_pct"),
        "probe": run.get("host_probe_ms"),
        "gates": run.get("gates_pass"),
        "mx_member_zeroed": member.get("blks_zeroed"),
        "mx_member_read": member.get("blks_read"),
        "mx_offset_zeroed": offset.get("blks_zeroed"),
        "mx_offset_read": offset.get("blks_read"),
        "commands": commands,
    }


def f(value, digits=1):
    return "" if value is None else f"{value:,.{digits}f}"


def pct(new, old):
    return None if not old or new is None else (new - old) / old * 100


def signed(value):
    whole = round(value)
    return "0%" if whole == 0 else f"{whole:+d}%"


def diff_cells(a, b, keys):
    return " | ".join(signed(pct(a[k], b[k])) if pct(a[k], b[k]) is not None else "" for k in keys)


RUN_HEADER = ("| Side | Workers | Sample | Commands/s | p99 ms | App CPU | DB CPU | Statements | DISCARD ALL | Event insert ms per row "
              "| Catch-up s | Rule p99 ms | DB throttled % | Probe ms | Gates |")
RUN_RULE = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
RUN_NOTE = ("CPU is milliseconds per command (application CPU is the bench plus the engine); statements and DISCARD ALL are calls "
            "per command; the event insert is the execution time of INSERT INTO core.event per row written, which includes its "
            "trigger and foreign-key check; rule p99 is milliseconds from an event to its rule's action; DB throttled is the share of scheduler periods the database hit its CPU limit.")


def run_row(side, workers, sample, r):
    return (f"| {side} | {workers} | {sample} | {f(r['rate'], 0)} | {f(r['p99'])} | {f(r['app'], 2)} | {f(r['db'], 2)} "
            f"| {f(r['statements'])} | {f(r['discards'], 2)} | {f(r['insert'], 3)} | {f(r['catchup'])} | {f(r['rule_p99'], 0)} | {f(r['db_thr'], 1)} "
            f"| {f(r['probe'], 0)} | {'pass' if r['gates'] else 'FAIL'} |")


def core_tables():
    blocks = ("r1", "r2", "r3")
    runs = [(side, sample, load(side, "batch-payments", 8, sample)) for sample in blocks for side in CORE]
    runs = [run for run in runs if run[2]]
    out = ["### Payment file at 8 workers", "",
           f"*Every run of the core block at 500,000 accounts, by block. {RUN_NOTE}*", "", RUN_HEADER, RUN_RULE]
    out += [run_row(side, 8, sample, r) for side, sample, r in runs]
    out.append("")
    keys = ("rate", "p99", "db", "statements", "insert")
    rows = []
    for side in CORE[1:]:
        for sample in blocks:
            a, b = load(side, "batch-payments", 8, sample), load("csharp-wax", "batch-payments", 8, sample)
            if a and b:
                rows.append(f"| {side} | {sample} | {diff_cells(a, b, keys)} |")
    out += ["*Each mode against unchanged csharp-wax within the same back-to-back block, as percent differences (positive means "
            "the mode is higher).*", "",
            "| Mode | Block | Commands/s | p99 | DB CPU | Statements | Event insert per row |",
            "| --- | --- | --- | --- | --- | --- | --- |"] + rows + [""]
    means = []
    for side in CORE:
        found = [load(side, "batch-payments", 8, sample) for sample in blocks]
        found = [r for r in found if r and r["gates"] is not False]
        if found:
            avg = {k: sum(r[k] for r in found) / len(found) for k in ("rate", "db", "statements", "insert", "discards")}
            means.append(f"| {side} | {len(found)} | {f(avg['rate'], 0)} | {f(avg['db'], 2)} | {f(avg['statements'])} "
                         f"| {f(avg['discards'], 2)} | {f(avg['insert'], 3)} |")
    out += ["*Means over the blocks for each mode, counting only runs that passed every gate; units as in the first table.*", "",
            "| Side | Runs | Commands/s | DB CPU | Statements | DISCARD ALL | Event insert ms per row |",
            "| --- | --- | --- | --- | --- | --- | --- |"] + means + [""]
    return out


def fair_tables():
    out = []
    for workload, diagnostic, title in (("batch-payments", None, "payment file"), ("placement", "no-engine", "placement with the engine paused")):
        runs = [(side, sample, load(side, workload, 8, sample, diagnostic))
                for sample in ("f1", "f2") for side in ("csharp-wax-fair-prepared", "csharp-wax-prepared")]
        runs = [run for run in runs if run[2]]
        if not runs:
            continue
        out += [f"### Fair batched mode, {title}", "",
                f"*Both modes run with both driver settings, 8 workers, 500,000 accounts. {RUN_NOTE}*", "", RUN_HEADER, RUN_RULE]
        out += [run_row(side, 8, sample, r) for side, sample, r in runs]
        rows = []
        for sample in ("f1", "f2"):
            a = load("csharp-wax-fair-prepared", workload, 8, sample, diagnostic)
            b = load("csharp-wax-prepared", workload, 8, sample, diagnostic)
            if a and b:
                rows.append(f"| {sample} | {diff_cells(a, b, ('rate', 'p99', 'db', 'statements', 'insert'))} |")
        out += ["", "*The fair batched mode against csharp-wax-prepared within each block, as percent differences (positive means "
                    "the fair mode is higher).*", "",
                "| Block | Commands/s | p99 | DB CPU | Statements | Event insert per row |",
                "| --- | --- | --- | --- | --- | --- |"] + rows + [""]
    return out


def trio_tables(workload, title, settings):
    runs = [(side, workers, sample, load(side, workload, workers, sample)) for workers, sample in settings for side in TRIO]
    runs = [run for run in runs if run[3]]
    if not runs:
        return []
    out = [f"### {title}", "", f"*Every run at 500,000 accounts, by block. {RUN_NOTE}*", "", RUN_HEADER, RUN_RULE]
    out += [run_row(side, workers, sample, r) for side, workers, sample, r in runs]
    keys = ("rate", "p99", "db", "statements", "insert")
    for a_side, b_side, caption in (("csharp-wax-prepared", "csharp-wax", "csharp-wax-prepared against unchanged csharp-wax"),
                                    ("python-direct", "csharp-wax-prepared", "python-direct against csharp-wax-prepared")):
        rows = []
        for workers, sample in settings:
            a, b = load(a_side, workload, workers, sample), load(b_side, workload, workers, sample)
            if a and b:
                rows.append(f"| {workers} | {sample} | {diff_cells(a, b, keys)} |")
        if rows:
            out += ["", f"*{caption} within each back-to-back block, as percent differences (positive means {a_side} is higher).*", "",
                    "| Workers | Block | Commands/s | p99 | DB CPU | Statements | Event insert per row |",
                    "| --- | --- | --- | --- | --- | --- | --- |"] + rows
    return out + [""]


def single_worker_tables():
    runs = [(side, load(side, "batch-payments", 1, "r1", "no-engine")) for side in ("python-direct", "csharp-wax-prepared")]
    runs = [run for run in runs if run[1]]
    if not runs:
        return []
    out = ["### One worker with the engine paused", "",
           f"*The payment file with a single worker and the engine paused, so each command's latency is the whole story. {RUN_NOTE}*", "",
           RUN_HEADER, RUN_RULE]
    out += [run_row(side, 1, "r1", r) for side, r in runs]
    if len(runs) == 2:
        out += ["", "*python-direct against csharp-wax-prepared, as percent differences (positive means python-direct is higher).*", "",
                "| Commands/s | p99 | DB CPU | Statements | Event insert per row |", "| --- | --- | --- | --- | --- |",
                f"| {diff_cells(runs[0][1], runs[1][1], ('rate', 'p99', 'db', 'statements', 'insert'))} |"]
    return out + [""]


def multixact_table():
    rows = []
    for sample in ("r1", "r2", "r3"):
        for side in CORE:
            r = load(side, "batch-payments", 8, sample)
            if r:
                rows.append(f"| {side} | {sample} | {f(r['mx_member_zeroed'], 0)} | {f(r['mx_member_read'], 0)} "
                            f"| {f(r['mx_offset_zeroed'], 0)} | {f(r['mx_offset_read'], 0)} |")
    if not rows:
        return []
    return ["### MultiXact caches", "",
            "*Blocks of the two MultiXact caches in pg_stat_slru over each core run: zeroed is new pages, read is pages that had to "
            "come from disk because the cache missed.*", "",
            "| Side | Block | Member zeroed | Member read | Offset zeroed | Offset read |",
            "| --- | --- | --- | --- | --- | --- |"] + rows + [""]


def main():
    out = core_tables()
    out += fair_tables()
    out += trio_tables("batch-payments", "Payment file at 16 and 32 workers", ((16, "r1"), (16, "r2"), (32, "r1"), (32, "r2")))
    out += trio_tables("realtime", "Real time at 8, 16, and 32 workers", ((8, "r1"), (16, "r1"), (16, "r2"), (32, "r1"), (32, "r2")))
    out += trio_tables("batch-accrual", "Interest accrual at 8 workers", ((8, "r1"),))
    out += single_worker_tables()
    out += multixact_table()
    print("\n".join(out))


if __name__ == "__main__":
    main()
