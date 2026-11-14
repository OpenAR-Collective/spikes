"""Summarizes round 2's code-shape pass (plan 4a318a7, layout round2-shape)
from the recorded results and prints Markdown.

For each of the payment file, interest accrual, and real time: one table with
every run, then two comparisons within each back-to-back block (batched
csharp-wax against plain csharp-wax, and python-direct against batched
csharp-wax). For placement: one table with every run, then batched against
plain within each block, normal and with the engine paused, and the spread of
the noise pair.

    py -3 summarize_shape.py [results folder]

The results folder defaults to the one beside this folder.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "results"
PASS = "round2-shape"
TRIO = ("python-direct", "csharp-wax", "csharp-wax-batched")
WORKLOADS = (("batch-payments", "Payment file"), ("batch-accrual", "Interest accrual"), ("realtime", "Real time"))
BLOCKS = ((8, "r1"), (16, "r1"), (8, "r2"))


def load(side, workload, workers, sample, diagnostic=None):
    middle = f"-{diagnostic}" if diagnostic else ""
    found = sorted(RESULTS.glob(f"*-{PASS}-{side}-{workload}-w{workers}{middle}-{sample}/results.json"))
    if not found:
        return None
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    throttle = run.get("cpu_throttling") or {}
    rules = (run.get("rule_latency_ms") or {}).get("all") or {}
    calls = rows = None
    for statement in run.get("top_statements") or []:
        if statement["query"].lstrip().upper().startswith("INSERT INTO CORE.EVENT"):
            calls = (calls or 0) + statement["calls"]
            rows = (rows or 0) + statement["rows"]
    return {
        "rate": bench["succeeded"] / bench["elapsed_s"],
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "bench_cpu": cpu.get("bench"),
        "engine_cpu": cpu.get("engine"),
        "db": cpu.get("db"),
        "statements": run.get("statements_per_command"),
        "event_calls": calls,
        "event_rows": rows,
        "catchup": run.get("catchup_s"),
        "catchup_cpu": run.get("engine_catchup_cpu_s"),
        "rule_p99": rules.get("p99"),
        "register_p99": (run.get("register_lateness_ms") or {}).get("p99"),
        "bench_thr": (throttle.get("bench") or {}).get("throttled_pct"),
        "engine_thr": (throttle.get("engine") or {}).get("throttled_pct"),
        "db_thr": (throttle.get("db") or {}).get("throttled_pct"),
        "probe": run.get("host_probe_ms"),
        "gates": run.get("gates_pass"),
        "failed": bench.get("failed"),
    }


def f(value, digits=1):
    return "" if value is None else f"{value:,.{digits}f}"


def pct(new, old):
    return None if not old or new is None else (new - old) / old * 100


def diff_row(lead, a, b, keys):
    return lead + " | ".join(f"{pct(a[k], b[k]):+.0f}%" if pct(a[k], b[k]) is not None else "" for k in keys) + " |"


def workload_tables(workload, title):
    rows = [(side, w, s, load(side, workload, w, s)) for w, s in BLOCKS for side in TRIO]
    rows = [row for row in rows if row[3]]
    if not rows:
        return []
    out = [f"### {title}", "",
           "*Every recorded run of this workload at 500,000 accounts, in block order. CPU is milliseconds per command (application CPU is the bench plus the engine); event inserts are the calls and rows of INSERT INTO core.event; throttled is the share of scheduler periods each container hit its CPU limit; rule p99 is milliseconds from an event to its rule's action.*",
           "",
           "| Side | Workers | Sample | Commands/s | p99 ms | App CPU | Bench CPU | Engine CPU | DB CPU | Statements | Event insert calls / rows | Catch-up s | Rule p99 ms | Throttled % bench / engine / db | Probe ms | Gates |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for side, workers, sample, r in rows:
        out.append(f"| {side} | {workers} | {sample} | {f(r['rate'], 0)} | {f(r['p99'])} | {f(r['app'], 2)} | {f(r['bench_cpu'], 2)} "
                   f"| {f(r['engine_cpu'], 2)} | {f(r['db'], 2)} | {f(r['statements'])} | {f(r['event_calls'], 0)} / {f(r['event_rows'], 0)} "
                   f"| {f(r['catchup'])} | {f(r['rule_p99'], 0)} | {f(r['bench_thr'], 0)} / {f(r['engine_thr'], 0)} / {f(r['db_thr'], 0)} "
                   f"| {f(r['probe'], 0)} | {'pass' if r['gates'] else 'FAIL'} |")
    out.append("")
    keys = ("rate", "p99", "app", "db", "statements")
    for a_side, b_side, caption in (("csharp-wax-batched", "csharp-wax", "Batched csharp-wax against plain csharp-wax"),
                                    ("python-direct", "csharp-wax-batched", "python-direct against batched csharp-wax")):
        pairs = []
        for workers, sample in BLOCKS:
            a, b = load(a_side, workload, workers, sample), load(b_side, workload, workers, sample)
            if a and b:
                pairs.append(diff_row(f"| {workers} | {sample} | ", a, b, keys))
        if pairs:
            out += [f"*{caption} within each back-to-back block, as percent differences (positive means {a_side} is higher).*", "",
                    "| Workers | Sample | Commands/s | p99 | App CPU | DB CPU | Statements |",
                    "| --- | --- | --- | --- | --- | --- | --- |"] + pairs + [""]
    return out


def placement_tables():
    runs = []
    for diagnostic in (None, "no-engine"):
        for sample in ("r1", "r2", "r3"):
            for side in ("csharp-wax", "csharp-wax-batched"):
                r = load(side, "placement", 8, sample, diagnostic)
                if r:
                    runs.append((side, diagnostic or "normal", sample, r))
    for sample in ("noise1", "noise2"):
        r = load("csharp-wax", "placement", 8, sample)
        if r:
            runs.append(("csharp-wax", "normal", sample, r))
    if not runs:
        return []
    out = ["### Placement", "",
           "*Every recorded placement run at 500,000 accounts and 8 workers. CPU is milliseconds per placement; catch-up CPU is the engine's CPU seconds after the bench ends; register p99 is milliseconds from placement to its register entry.*",
           "",
           "| Side | Engine | Sample | Placements/s | p99 ms | Bench CPU | Engine CPU | DB CPU | Statements | Event insert calls / rows | Catch-up s | Catch-up CPU s | Register p99 ms | Throttled % bench / engine / db | Probe ms | Gates |",
           "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for side, engine, sample, r in runs:
        out.append(f"| {side} | {engine} | {sample} | {f(r['rate'], 0)} | {f(r['p99'])} | {f(r['bench_cpu'], 3)} | {f(r['engine_cpu'], 3)} "
                   f"| {f(r['db'], 3)} | {f(r['statements'])} | {f(r['event_calls'], 0)} / {f(r['event_rows'], 0)} | {f(r['catchup'])} "
                   f"| {f(r['catchup_cpu'])} | {f(r['register_p99'], 0)} | {f(r['bench_thr'], 0)} / {f(r['engine_thr'], 0)} / {f(r['db_thr'], 0)} "
                   f"| {f(r['probe'], 0)} | {'pass' if r['gates'] else 'FAIL'} |")
    out.append("")
    keys = ("rate", "p99", "db", "statements", "catchup_cpu")
    pairs = []
    for diagnostic in (None, "no-engine"):
        for sample in ("r1", "r2", "r3"):
            a = load("csharp-wax-batched", "placement", 8, sample, diagnostic)
            b = load("csharp-wax", "placement", 8, sample, diagnostic)
            if a and b:
                pairs.append(diff_row(f"| {diagnostic or 'normal'} | {sample} | ", a, b, keys))
    n1, n2 = load("csharp-wax", "placement", 8, "noise1"), load("csharp-wax", "placement", 8, "noise2")
    if n1 and n2:
        pairs.append(diff_row("| noise pair | plain noise2 against noise1 | ", n2, n1, keys))
    if pairs:
        out += ["*Batched csharp-wax against plain csharp-wax within each placement block, as percent differences (positive means batched is higher). The last row is the noise pair: two plain runs back to back.*", "",
                "| Engine | Sample | Placements/s | p99 | DB CPU | Statements | Catch-up CPU |",
                "| --- | --- | --- | --- | --- | --- | --- |"] + pairs + [""]
    return out


def main():
    out = []
    for workload, title in WORKLOADS:
        out += workload_tables(workload, title)
    out += placement_tables()
    print("\n".join(out))


if __name__ == "__main__":
    main()
