"""Summarizes round 2's follow-up tests (plan round2-followup) from the
recorded results and prints Markdown.

First the custom-plans confirmation (group 1): each run in run order, then
each of the three settings over its three blocks. Then every run of the pass
in run order, so that the slow regime's place in the night shows. Then every
10-second interval of the C# runs with the engine, from their readers' pass
reports, set against what the side sampler and the host's counters saw in
the same interval, slow intervals against fast. An interval that applied
fewer than 2,300 events per second is in the slow regime, as in the fix
pass; the first interval that applied events and the last two, which ramp up
and drain, are left out. Last, the one-worker pair (group 3).

    py -3 summarize_followup.py [results folder]

The results folder defaults to the one beside this folder, and the host's
counters are read from round2-followup.host-counters.csv beside this script.
"""

import csv
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "results"
NAME = "round2-followup"
PLAN = json.loads((HERE / f"{NAME}.plan.json").read_text(encoding="utf-8"))
HOST = HERE / f"{NAME}.host-counters.csv"
SLOW_EVENTS_PER_SECOND = 2_300
INSERT = "INSERT INTO CORE.EVENT"

# The host's counters, by the end of their typeperf column heading.
HOST_COLUMNS = {
    r"Processor Information(_Total)\% Processor Performance": "perf",
    r"Processor Information(_Total)\% Performance Limit": "limit",
    r"Processor Information(_Total)\Performance Limit Flags": "flags",
    r"Processor Information(_Total)\% Processor Utility": "utility",
    r"Processor(_Total)\% Processor Time": "busy",
    r"Process(vmmemWSL)\% Processor Time": "vm",
    r"Hyper-V Hypervisor Logical Processor(_Total)\% Total Run Time": "hv_total",
    r"Hyper-V Hypervisor Logical Processor(_Total)\% Guest Run Time": "hv_guest",
}

# The side sampler's wait keys, gathered into a few kinds. A key is
# backend/state/wait type/wait event.
WAIT_KINDS = {
    "no wait event": lambda k: k.startswith("client_backend/active/none/"),
    "WAL lock": lambda k: k.startswith("client_backend/active/LWLock/WAL"),
    "WAL I/O": lambda k: k.startswith("client_backend/active/IO/Wal"),
    "other I/O": lambda k: k.startswith("client_backend/active/IO/") and "/IO/Wal" not in k,
    "other LWLock": lambda k: k.startswith("client_backend/active/LWLock/") and "/LWLock/WAL" not in k,
    "lock": lambda k: k.startswith("client_backend/active/Lock/"),
    "idle in transaction": lambda k: k.startswith("client_backend/idle_in_transaction/"),
    "walwriter busy": lambda k: k.startswith("walwriter/"),
}


def utc(text):
    """An ISO time with Z and up to 7 fractional digits, as UTC."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", text)
    stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return stamp + timedelta(microseconds=int((match.group(2) or "0")[:6].ljust(6, "0")))


def intervals(folder):
    """The readers' 10-second intervals that applied events, without the
    first and the last two: (start, end, passes, events) each."""
    rows = []
    for log in sorted((folder / "raw").glob("*.engine.txt")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 8 and parts[0] == "passes" and parts[4] == "passes":
                end, seconds = utc(parts[1]), float(parts[3])
                if int(parts[7]) > 0:
                    rows.append((end - timedelta(seconds=seconds), end, int(parts[5]), int(parts[7])))
    return rows[1:-2]


def sampler(folder):
    """The side sampler's lines: (time, waits by key), (time, probe ms), and (time, counters)."""
    waits, probes, stats = [], [], []
    for path in sorted((folder / "raw").glob("*.sampler.txt")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0] != "NOTICE:":
                continue
            stamp = utc(parts[2])
            if parts[1] == "waits":
                waits.append((stamp, {k: int(n) for k, n in (item.rsplit("=", 1) for item in parts[3:])}))
            elif parts[1] == "probe":
                probes.append((stamp, float(parts[3])))
            elif parts[1] == "stats":
                stats.append((stamp, {k: int(v) for k, v in (item.split("=", 1) for item in parts[3:])}))
    return waits, probes, stats


def host_counters():
    """The host's samples as (UTC time, values by short name); none if the file is missing."""
    if not HOST.exists():
        return []
    with HOST.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return []
    bias = re.search(r"\((-?\d+)\)\s*$", rows[0][0])
    offset = timedelta(minutes=int(bias.group(1))) if bias else timedelta(0)
    names = []
    for heading in rows[0]:
        name = next((short for end, short in HOST_COLUMNS.items() if heading.endswith(end)), None)
        # Each logical processor's load and speed, as cpu<N>_busy and cpu<N>_perf.
        each = re.search(r"Processor Information\(\d+,(\d+)\)\\% Processor (Time|Performance)$", heading)
        if each:
            name = f"cpu{each.group(1)}_{'busy' if each.group(2) == 'Time' else 'perf'}"
        names.append(name)
    samples = []
    for row in rows[1:]:
        try:
            local = datetime.strptime(row[0], "%m/%d/%Y %H:%M:%S.%f")
        except (ValueError, IndexError):
            continue
        values = {}
        for name, cell in zip(names[1:], row[1:]):
            if name is None or name in values:
                continue
            try:
                values[name] = float(cell)
            except ValueError:
                continue
        samples.append(((local + offset).replace(tzinfo=timezone.utc), values))
    return samples


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def within(samples, start, end):
    return [value for stamp, value in samples if start <= stamp < end]


def load(entry, host):
    found = sorted(RESULTS.glob(f"*-{entry['label']}/results.json"))
    if not found:
        return None
    folder = found[0].parent
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, cpu = run["bench"], run.get("cpu_ms_per_command") or {}
    commands = bench["succeeded"]
    insert_ms = insert_rows = 0
    for statement in run.get("top_statements") or []:
        if " ".join(statement["query"].split()).upper().startswith(INSERT):
            insert_ms += statement["total_ms"]
            insert_rows += statement["rows"]
    # The bench's own prepared insert: plan choices summed over its connections,
    # and the range of custom plans per connection.
    generic = custom = 0
    per_connection = []
    for connection in bench.get("plan_choices") or []:
        for statement in connection["statements"]:
            if statement["statement"].upper().startswith(INSERT):
                generic += statement["generic_plans"]
                custom += statement["custom_plans"]
                per_connection.append(statement["custom_plans"])
    waits, probes, stats = sampler(folder)
    rows = []
    for start, end, passes, events in intervals(folder):
        seconds = (end - start).total_seconds()
        in_waits = within(waits, start, end)
        kinds = {kind: mean(sum(n for k, n in sample.items() if test(k)) for sample in in_waits) for kind, test in WAIT_KINDS.items()}
        pairs = [(a, b) for (a, b) in zip(stats, stats[1:]) if start <= b[0] < end]
        rate = lambda key: mean((b[1][key] - a[1][key]) / (b[0] - a[0]).total_seconds() for a, b in pairs)
        in_host = within(host, start, end)
        rows.append({
            "start": start, "events_s": events / seconds, "slow": events / seconds < SLOW_EVENTS_PER_SECOND,
            "events_pass": events / passes, "probe": mean(within(probes, start, end)), **kinds,
            "commits_s": rate("commits") if pairs else None, "wal_syncs_s": rate("wal_sync") if pairs else None,
            "wal_mb_s": rate("wal_bytes") / 2**20 if pairs else None,
            **{name: mean(sample.get(name) for sample in in_host)
               for name in {*HOST_COLUMNS.values(), *(key for sample in in_host for key in sample)}},
        })
    fast = [r["events_s"] for r in rows if not r["slow"]]
    run_start = utc(run["clock"]["start"].replace(" ", "T").replace("+00", "Z")) if run.get("clock") else None
    return {
        "folder": folder, "rows": rows, "start": run_start,
        "rate": commands / bench["elapsed_s"],
        "p99": (bench.get("latency_ms") or {}).get("p99"),
        "db": cpu.get("db"), "app": (cpu.get("bench") or 0) + (cpu.get("engine") or 0),
        "statements": run.get("statements_per_command"),
        "insert": insert_ms / insert_rows if insert_rows else None,
        "generic": generic, "custom": custom, "connections": len(bench.get("plan_choices") or []),
        "custom_range": (min(per_connection), max(per_connection)) if per_connection else None,
        "reset": bench.get("reset_on_return"),
        "fast_events_s": median(fast), "slow_share": 100 * sum(r["slow"] for r in rows) / len(rows) if rows else None,
        "probe": median(value for _, value in probes), "host_probe": run.get("host_probe_ms"),
        "sampler_cores": (run.get("side_sampler") or {}).get("cores"),
        "db_cores": (run.get("cpu_s") or {}).get("db") / run["window_s"] if (run.get("cpu_s") or {}).get("db") else None,
        "perf": median(value.get("perf") for stamp, value in host if run_start and run_start <= stamp),
        "gates": run.get("gates_pass"),
        "failed": [name for name, passed in (run.get("gates") or {}).items() if passed is False],
    }


def f(value, digits=1):
    return "" if value is None else f"{value:,.{digits}f}"


def local(stamp):
    return "" if stamp is None else f"{stamp.astimezone():%H:%M}"


def gate_cell(r):
    return "pass" if r["gates"] else "FAIL: " + ", ".join(str(name) for name in r["failed"])


def short(entry):
    return entry["label"].replace(f"{NAME}-", "").replace("-batch-payments", "")


def main():
    host = host_counters()
    loaded = [(entry, load(entry, host)) for entry in PLAN["runs"]]
    loaded = [(entry, r) for entry, r in loaded if r]
    out = []

    out += ["### Custom plans: group 1, payment file at 8 workers", "",
            "*Each run in run order: its start (local time), commands per second, events per second over its fast "
            "intervals (median), the share of its 10-second intervals in the slow regime, the event insert's execution "
            "time in milliseconds per row (trigger and foreign-key check included), database CPU in milliseconds per "
            "command, statements per command, and the bench's prepared event insert: generic and custom plans summed "
            "over its connections, the connections read, and the fewest and most custom plans on one connection.*", "",
            "| Order | Run | Start | Commands/s | Fast events/s | Slow-regime share % | Event insert ms per row | DB CPU "
            "| Statements | Insert generic plans | Insert custom plans | Connections | Custom per connection | Gates |",
            "| --- " * 14 + "|"]
    for entry, r in loaded:
        if entry["group"] != 1:
            continue
        span = r["custom_range"]
        out.append(f"| {entry['order']} | {short(entry)} | {local(r['start'])} | {f(r['rate'], 0)} | {f(r['fast_events_s'], 0)} "
                   f"| {f(r['slow_share'], 0)} | {f(r['insert'], 3)} | {f(r['db'], 2)} | {f(r['statements'])} "
                   f"| {r['generic']:,} | {r['custom']:,} | {r['connections']} | {'' if not span else f'{span[0]:,} to {span[1]:,}'} "
                   f"| {gate_cell(r)} |")
    arms = {}
    for entry, r in loaded:
        if entry["group"] == 1:
            arms.setdefault(entry["side"], []).append((entry, r))
    out += ["", "*Each setting over its blocks: the event insert's milliseconds per row in each block, in block order, "
            "and the median of each figure; units as above.*", "",
            "| Setting | Runs | Insert ms per row by block | Insert median | Fast events/s median | Slow-regime share median % "
            "| DB CPU median | Statements median |", "| --- " * 8 + "|"]
    for side, runs in arms.items():
        runs.sort(key=lambda pair: pair[0]["suffix"])
        values = [r for _, r in runs]
        out.append(f"| {side} | {len(values)} | {', '.join(f(r['insert'], 3) for r in values)} | {f(median(r['insert'] for r in values), 3)} "
                   f"| {f(median(r['fast_events_s'] for r in values), 0)} | {f(median(r['slow_share'] for r in values), 0)} "
                   f"| {f(median(r['db'] for r in values), 2)} | {f(median(r['statements'] for r in values))} |")

    out += ["", "### Every run in run order", "",
            "*Each run's start (local time), commands per second, the share of its intervals in the slow regime (C# runs "
            "with the engine only), the side sampler's CPU probe (median milliseconds for 4 million rows, every 10 seconds "
            "through the run), the side sampler's own CPU (measured from its backend and its psql, as a percentage of one "
            "core and of the database container's CPU over the bench), the host probe before the run (median milliseconds "
            "for 20 million rows), the host's processor performance against nominal (median percent through the run), and "
            "the gates.*", "",
            "| Order | Group | Run | Start | Commands/s | Slow-regime share % | Sampler probe ms | Sampler CPU % of a core "
            "| Sampler share of DB CPU % | Host probe ms | Processor performance % | Gates |", "| --- " * 12 + "|"]
    for entry, r in loaded:
        out.append(f"| {entry['order']} | {entry['group']} | {short(entry)} | {local(r['start'])} | {f(r['rate'], 0)} "
                   f"| {f(r['slow_share'], 0)} | {f(r['probe'])} "
                   f"| {f(r['sampler_cores'] * 100 if r['sampler_cores'] is not None else None, 1)} "
                   f"| {f(100 * r['sampler_cores'] / r['db_cores'] if r['sampler_cores'] and r['db_cores'] else None, 2)} "
                   f"| {f(r['host_probe'])} | {f(r['perf'], 0)} | {gate_cell(r)} |")

    columns = [("events_s", "Events/s", 0), ("probe", "Probe ms", 1), ("perf", "Processor performance %", 0),
               ("limit", "Performance limit %", 0), ("flags", "Limit flags", 0), ("busy", "Host busy %", 0),
               ("vm", "VM CPU % of a core", 0), ("commits_s", "Commits/s", 0), ("wal_syncs_s", "WAL syncs/s", 0),
               ("wal_mb_s", "WAL MB/s", 1), *[(kind, kind[0].upper() + kind[1:], 2) for kind in WAIT_KINDS]]
    for title, select in (("csharp-wax-rw8 runs (groups 2 and 4)", lambda e: e["side"] == "csharp-wax-rw8"),
                          ("group 1 runs", lambda e: e["group"] == 1)):
        rows = [row for entry, r in loaded if select(entry) for row in r["rows"]]
        if not rows:
            continue
        out += ["", f"### The slow regime: {title}", "",
                "*The 10-second intervals of these runs, slow against fast: how many there are, and the median of each "
                "figure over them. Events per second are the readers'. The probe is the side sampler's (4 million rows). "
                "The host's figures are typeperf's 5-second samples in the interval, averaged: processor performance "
                "against nominal, the share a power or thermal limit allows, the limit flags, Windows' busy percent, and the "
                "CPU of the virtual machine Docker runs in. Commits, WAL syncs, and WAL are rates over the interval. Each "
                "wait kind is the mean number of client backends in it at the side sampler's once-a-second looks. No wait "
                "event means a backend reported none, which includes one on a core and one waiting for a core. Walwriter "
                "busy counts the WAL writer when it waits on anything but its main loop.*", "",
                "| Regime | Intervals | " + " | ".join(label for _, label, _ in columns) + " |",
                "| --- | --- |" + " --- |" * len(columns)]
        for regime, slow in (("fast", False), ("slow", True)):
            chosen = [row for row in rows if row["slow"] == slow]
            out.append(f"| {regime} | {len(chosen)} | " + " | ".join(f(median(row[key] for row in chosen), digits)
                                                                for key, _, digits in columns) + " |")

    rows = [row for entry, r in loaded for row in r["rows"]]
    processors = sorted({key[3:-5] for row in rows for key in row if re.fullmatch(r"cpu\d+_busy", key)}, key=int)
    if processors:
        out += ["", "### The slow regime: each logical processor", "",
                "*Over the 10-second intervals of every C# run with the engine, slow against fast: each logical "
                "processor's busy percent and its performance against nominal, the medians of typeperf's samples in "
                "the intervals.*", "",
                "| Processor | Busy % fast | Busy % slow | Performance % fast | Performance % slow |", "| --- " * 5 + "|"]
        for n in processors:
            cells = [f(median(row.get(f"cpu{n}_{kind}") for row in rows if row["slow"] == slow), 0)
                     for kind in ("busy", "perf") for slow in (False, True)]
            out.append(f"| {n} | " + " | ".join(cells) + " |")

    pair = [(entry, r) for entry, r in loaded if entry["group"] == 3]
    if pair:
        out += ["", "### The one-worker pair: group 3, payment file at 1 worker with the engine paused", "",
                "*In run order: commands per second, latency at the 99th percentile in milliseconds, database and "
                "application CPU in milliseconds per command, statements per command, the event insert's milliseconds "
                "per row, and the side sampler's probe (median milliseconds).*", "",
                "| Order | Run | Start | Commands/s | p99 ms | DB CPU | App CPU | Statements | Event insert ms per row "
                "| Sampler probe ms | Gates |", "| --- " * 11 + "|"]
        for entry, r in pair:
            out.append(f"| {entry['order']} | {short(entry)} | {local(r['start'])} | {f(r['rate'])} | {f(r['p99'], 2)} "
                       f"| {f(r['db'], 2)} | {f(r['app'], 2)} | {f(r['statements'])} | {f(r['insert'], 3)} "
                       f"| {f(r['probe'])} | {gate_cell(r)} |")
    print("\n".join(out))


if __name__ == "__main__":
    main()
