"""Judges the EF Core mini-pass against the criteria approved before any
run, from the recorded results, and prints the verdicts as Markdown:

    py -3 judge_efcore.py

For each configuration it takes the EF-against-plain difference within each
of the three back-to-back blocks, then the median of those three, and the
plain-against-plain difference of the configuration's noise block. A median
difference that misses its criterion but is no larger than the noise block's
is reported as no difference beyond noise. The placement block is a single
pair with no noise block, so it is reported without a verdict. The criteria
are listed in efcore-pass.plan.md.
"""

import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
PASS = "efcore-pass"

# Each configuration's workload, its label suffix, and its heading.
CONFIGS = [
    ("batch-payments", "", "Payment file"),
    ("realtime", "", "Real time"),
    ("batch-accrual", "", "Interest accrual"),
    ("batch-payments", "-no-engine", "Payment file, engine paused"),
]

# Each measure's name, key, criterion, and test. The test takes the median
# percent difference and the median absolute difference; None means the
# measure is reported with no threshold.
MEASURES = [
    ("Database CPU, ms per command", "db", "+10%", lambda d, abs_d: d <= 10),
    ("Application CPU, ms per command", "app", "+50% or +1.0 ms", lambda d, abs_d: d <= 50 or abs_d <= 1.0),
    ("Commands per second", "rate", "-10%", lambda d, abs_d: d >= -10),
    ("Latency p99, ms", "p99", "+25%", lambda d, abs_d: d <= 25),
    ("Statements per command", "statements", "reported", None),
]


def load(label):
    """One recorded run's measures, or None if it has not run."""
    found = sorted(RESULTS.glob(f"*-{PASS}-{label}/results.json"))
    if not found:
        return None
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    return {
        "rate": bench["succeeded"] / bench["elapsed_s"],
        "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "db": cpu.get("db"),
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "statements": run.get("statements_per_command"),
        "gates": run.get("gates_pass"),
    }


def pct(new, old):
    return (new - old) / old * 100 if old else None


def digits(key):
    return 0 if key == "rate" else 2 if key in ("db", "app") else 1


def judge(workload, diagnostic, title, verdicts):
    plain = [load(f"csharp-wax-{workload}-w8{diagnostic}-r{n}") for n in (1, 2, 3)]
    ef = [load(f"csharp-wax-ef-{workload}-w8{diagnostic}-r{n}") for n in (1, 2, 3)]
    noise = [load(f"csharp-wax-{workload}-w8{diagnostic}-noise{n}") for n in (1, 2)]
    if None in plain or None in ef:
        return [f"### {title}", "", "Not every block has run yet.", ""]
    lines = [
        f"### {title}",
        "",
        "*Medians of three back-to-back blocks at 8 workers and 500,000 accounts. The Plain and EF Core columns are the median run of each mode; the difference is the median of the three within-block differences; the noise floor is the plain-against-plain block's difference.*",
        "",
        "| Measure | Plain | EF Core | Difference, median of blocks | Noise floor | Criterion | Verdict |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, key, criterion, ok in MEASURES:
        d = statistics.median(pct(e[key], p[key]) for e, p in zip(ef, plain))
        abs_d = statistics.median(e[key] - p[key] for e, p in zip(ef, plain))
        floor = abs(pct(noise[1][key], noise[0][key])) if None not in noise else None
        if ok is None:
            verdict = "reported"
        elif ok(d, abs_d):
            verdict = "meets"
        elif floor is not None and abs(d) <= floor:
            verdict = "no difference beyond noise"
        else:
            verdict = "fails"
        if ok is not None:
            verdicts.append((title, name, verdict))
        n = digits(key)
        plain_m = statistics.median(p[key] for p in plain)
        ef_m = statistics.median(e[key] for e in ef)
        lines.append(f"| {name} | {plain_m:,.{n}f} | {ef_m:,.{n}f} | {d:+.1f}% ({abs_d:+.{n}f}) "
                     f"| {'' if floor is None else f'{floor:.1f}%'} | {criterion} | {verdict} |")
    gates = all(r["gates"] for r in plain + ef + [r for r in noise if r])
    return lines + ["", f"Every gate passed in this configuration's runs: {'yes' if gates else 'NO'}.", ""]


def placement():
    plain, ef = load("csharp-wax-placement-w8-r1"), load("csharp-wax-ef-placement-w8-r1")
    if plain is None or ef is None:
        return ["### Placement", "", "The optional placement block did not run.", ""]
    lines = [
        "### Placement",
        "",
        "*One back-to-back pair at 8 workers and 500,000 accounts, with no noise block, so no verdict. The difference is EF Core against plain within the pair.*",
        "",
        "| Measure | Plain | EF Core | Difference |",
        "| --- | --- | --- | --- |",
    ]
    for name, key, _, _ in MEASURES:
        n = digits(key)
        lines.append(f"| {name} | {plain[key]:,.{n}f} | {ef[key]:,.{n}f} | {pct(ef[key], plain[key]):+.1f}% ({ef[key] - plain[key]:+.{n}f}) |")
    gates = plain["gates"] and ef["gates"]
    return lines + ["", f"Every gate passed in both runs: {'yes' if gates else 'NO'}.", ""]


def main():
    lines, verdicts = [], []
    for workload, diagnostic, title in CONFIGS:
        lines += judge(workload, diagnostic, title, verdicts)
    lines += placement()
    failed = [v for v in verdicts if v[2] == "fails"]
    lines += ["### Overall", "",
              f"{len(verdicts)} criterion checks; {len(failed)} fail beyond the noise floor."
              + ("" if not failed else " Failing: " + "; ".join(f"{t}, {n}" for t, n, _ in failed) + ".")]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
