"""Summarizes round 2's laptop pass from the recorded results and prints
Markdown: one table per workload with every run, then python-direct against
csharp-wax within each back-to-back block.

    py -3 summarize_round2.py [results folder]

The results folder defaults to the one beside this folder.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "results"
PASS = "round2-laptop"
SIDES = ("python-direct", "csharp-wax", "python")
WORKLOADS = (("batch-payments", "Payment file"), ("batch-accrual", "Interest accrual"), ("realtime", "Real time"))


def load(side, workload, workers, sample):
    found = sorted(RESULTS.glob(f"*-{PASS}-{side}-{workload}-w{workers}-{sample}/results.json"))
    if not found:
        return None
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    throttle = run.get("cpu_throttling") or {}
    rules = (run.get("rule_latency_ms") or {}).get("all") or {}
    return {
        "rate": bench["succeeded"] / bench["elapsed_s"],
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "bench_cpu": cpu.get("bench"),
        "engine_cpu": cpu.get("engine"),
        "db": cpu.get("db"),
        "statements": run.get("statements_per_command"),
        "catchup": run.get("catchup_s"),
        "rule_p99": rules.get("p99"),
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


def main():
    out = []
    for workload, title in WORKLOADS:
        rows = []
        for workers in (4, 8, 16, 32):
            for sample in ("r1", "r2"):
                for side in SIDES:
                    run = load(side, workload, workers, sample)
                    if run:
                        rows.append((side, workers, sample, run))
        if not rows:
            continue
        out += [f"### {title}", "",
                "*Every recorded run of this workload at 500,000 accounts. CPU is milliseconds per command (application CPU is the bench plus the engine); throttled is the share of scheduler periods each container hit its CPU limit; rule p99 is milliseconds from an event to its rule's action.*",
                "",
                "| Side | Workers | Sample | Commands/s | p99 ms | App CPU | Bench CPU | Engine CPU | DB CPU | Statements | Catch-up s | Rule p99 ms | Throttled % bench / engine / db | Probe ms | Gates |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for side, workers, sample, r in rows:
            out.append(f"| {side} | {workers} | {sample} | {f(r['rate'], 0)} | {f(r['p99'])} | {f(r['app'], 2)} | {f(r['bench_cpu'], 2)} "
                       f"| {f(r['engine_cpu'], 2)} | {f(r['db'], 2)} | {f(r['statements'])} | {f(r['catchup'])} | {f(r['rule_p99'], 0)} "
                       f"| {f(r['bench_thr'], 0)} / {f(r['engine_thr'], 0)} / {f(r['db_thr'], 0)} | {f(r['probe'], 0)} | {'pass' if r['gates'] else 'FAIL'} |")
        out.append("")
        pairs = []
        for workers in (4, 8, 16, 32):
            for sample in ("r1", "r2"):
                py, cs = load("python-direct", workload, workers, sample), load("csharp-wax", workload, workers, sample)
                if py and cs:
                    pairs.append((workers, sample, py, cs))
        if pairs:
            out += ["*python-direct against csharp-wax within each back-to-back block, as percent differences (positive means python-direct is higher).*", "",
                    "| Workers | Sample | Commands/s | p99 | App CPU | DB CPU | Statements |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for workers, sample, py, cs in pairs:
                out.append(f"| {workers} | {sample} | " + " | ".join(
                    f"{pct(py[k], cs[k]):+.0f}%" if pct(py[k], cs[k]) is not None else "" for k in ("rate", "p99", "app", "db", "statements")) + " |")
            out.append("")
    print("\n".join(out))


if __name__ == "__main__":
    main()
