"""Prints the figures round 2's write-up takes from the flush-timing night
(plan round2-flush) beyond the tables summarize_flush.py prints: the analysis
added after the results were in, in the order the write-up uses it. Figures
carry more digits than the write-up gives, so its rounding can be checked.
Times are the laptop's local time, by the offset in the host counters'
heading, so the output does not depend on the machine that runs it.

    python -I posthoc_flush.py > round2-flush.posthoc.txt

It reads only the pass's committed records: the results folders of plan
round2-flush and the round2-flush files beside this script.
"""

import csv
import json
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import summarize_flush as s  # noqa: E402

sys.stdout.reconfigure(newline="\n")

# The cuts the plan set: an interval or run is high at 2.8 ms or less per WAL
# sync and slow at 4.3 ms or more. Between them, this script also splits fast
# (under 3.6 ms) from between (3.6 to under 4.3 ms), the gap the steady
# intervals leave between the fast and slow clusters.
HIGH, SLOW, FAST_TOP = s.HIGH_MS, s.SLOW_MS, 3.6
s.HOST_COLUMNS.update({
    r"\Processor(_Total)\% Processor Time": "busy",
    r"\Processor Information(_Total)\% Performance Limit": "limit",
    r"\Processor Information(_Total)\Performance Limit Flags": "flags",
})
with s.HOST.open(encoding="utf-8", errors="replace", newline="") as handle:
    HEADING = next(csv.reader(handle))[0]
OFFSET = timedelta(minutes=int(re.search(r"\((-?\d+)\)\s*$", HEADING).group(1)))
HOST = sorted(s.host_counters(), key=lambda pair: pair[0])
STATUS = json.loads(s.STATUS.read_text(encoding="utf-8"))
PROCESSES = list(csv.DictReader(s.PROCESSES.open(encoding="utf-8", errors="replace", newline="")))


def local(t):
    return (t - OFFSET).replace(tzinfo=None)


def hms(t):
    return "" if t is None else f"{local(t):%H:%M:%S}"


def from_local(text):
    return (datetime.fromisoformat(text) + OFFSET).replace(tzinfo=timezone.utc)


def cls(ms):
    return "high" if ms <= HIGH else "fast" if ms < FAST_TOP else "between" if ms < SLOW else "slow"


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def heading(title):
    print()
    print(f"## {title}")
    print()


def intervals(stats, start, end):
    """Every 10-second sampler interval wholly inside the bench, with PostgreSQL's
    measured time per WAL sync; the steady ones are all but the first and last."""
    pairs = [(a, b) for a, b in zip(stats, stats[1:]) if start <= a[0] and b[0] <= end]
    rows = []
    for index, ((a_at, a), (b_at, b)) in enumerate(pairs):
        seconds = (b_at - a_at).total_seconds()
        syncs = b["wal_sync"] - a["wal_sync"]
        if seconds <= 0 or syncs <= 0 or "wal_sync_us" not in a or "wal_sync_us" not in b:
            continue
        sync_us = b["wal_sync_us"] - a["wal_sync_us"]
        rows.append({"start": a_at, "end": b_at, "seconds": seconds, "syncs": syncs, "rate": syncs / seconds,
                     "ms": sync_us / 1000 / syncs, "busy": sync_us / 1e6 / seconds,
                     "steady": 0 < index < len(pairs) - 1})
    return rows


RUNS = []
for entry in s.PLAN["runs"]:
    if entry.get("kind") == "idle":
        continue
    found = sorted(s.RESULTS.glob(f"*-{entry['label']}/results.json"))
    if not found:
        continue
    record = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    start, end = s.clock_utc(record["clock"]["start"]), s.clock_utc(record["clock"]["end"])
    _, stats = s.sampler(found[0].parent)
    wal = record["db_activity"]["wal"]
    rows = intervals(stats, start, end)
    RUNS.append({"entry": entry, "block": entry["label"].rsplit("-", 1)[1], "group": entry["group"],
                 "diag": entry["side"] == s.DIAGNOSTIC, "record": record, "start": start, "end": end, "stats": stats,
                 "all": rows, "steady": [r for r in rows if r["steady"]],
                 "rate": record["bench"]["succeeded"] / record["bench"]["elapsed_s"],
                 "syncs_s": wal["wal_sync"] / record["window_s"], "ms": wal["wal_sync_time"] / wal["wal_sync"],
                 "kb_per_sync": wal["wal_bytes"] / 1024 / wal["wal_sync"]})
BY = {r["block"]: r for r in RUNS}
TIMED = [r for r in RUNS if not r["diag"]]
CHECKS = []
for record in STATUS["runs"]:
    check = record.get("fsync_check")
    if check and (check.get("one_8k") or {}).get("fdatasync"):
        CHECKS.append({"label": record["label"].rsplit("-", 1)[1], "at": from_local(check["started"]),
                       "ops": check["one_8k"]["fdatasync"]})
end_check = STATUS.get("fsync_check_end")
if end_check:
    CHECKS.append({"label": "end", "at": from_local(end_check["started"]), "ops": end_check["one_8k"]["fdatasync"]})
for check in CHECKS:
    check["ms"] = 1000 / check["ops"]
CHECKS.sort(key=lambda c: c["at"])


def check_after(t):
    return next((c for c in CHECKS if c["at"] >= t), None)


def check_before(t):
    return next((c for c in reversed(CHECKS) if c["at"] <= t), None)


def host_in(a, b):
    return [v for t, v in HOST if a <= t < b]


print("# Round 2's flush-timing night: figures added after the results")
print()
print(f"Times are local, UTC less {OFFSET.total_seconds() / 3600:.0f} hours, from the host counters' heading. "
      f"An interval is high at {HIGH} ms per WAL sync or less, fast above that and under {FAST_TOP}, between from "
      f"{FAST_TOP} to under {SLOW}, and slow at {SLOW} or more. Steady intervals are a bench's 10-second intervals "
      "without its first and last.")

heading("Each run's steady intervals by class, and its course")
print("Run, commands per second, WAL syncs per second, mean ms per sync over the harness's window, then steady intervals "
      "in all and by class, and the course: each block of same-class intervals (counting the first and last) with its "
      "count and local start. The diagnostic's syncs carry several times the WAL of the others', so it is not classed.")
print()
for r in RUNS:
    counts = {k: sum(1 for x in r["steady"] if cls(x["ms"]) == k) for k in ("high", "fast", "between", "slow")}
    blocks = []
    for x in r["all"]:
        c = cls(x["ms"])
        if blocks and blocks[-1][0] == c:
            blocks[-1][1] += 1
        else:
            blocks.append([c, 1, x["start"]])
    course = "; ".join(f"{c} {n} from {hms(t)}" for c, n, t in blocks)
    note = " (diagnostic, not classed)" if r["diag"] else ""
    print(f"- {r['block']}{note}: {r['rate']:.1f} commands/s, {r['syncs_s']:.1f} syncs/s, {r['ms']:.3f} ms; steady "
          f"{len(r['steady'])}: high {counts['high']}, fast {counts['fast']}, between {counts['between']}, slow "
          f"{counts['slow']}. Course: {course}")

heading("The steady intervals' clusters")
steady = [x for r in TIMED for x in r["steady"]]
middle = [(r["block"], x) for r in TIMED for x in r["steady"] if FAST_TOP <= x["ms"] < 4.4]
print(f"Steady intervals, the diagnostic aside: {len(steady)}. From {FAST_TOP} to under 4.4 ms: {len(middle)}, of them "
      f"in b2: {sum(1 for b, _ in middle if b == 'b2')} ({100 * sum(1 for b, _ in middle if b == 'b2') / len(middle):.1f} percent).")
print()
print("Steady intervals by 0.2 ms band of time per sync (lower edge, count):")
bands = {}
for x in steady:
    edge = min(int(x["ms"] / 0.2) * 0.2, 7.0)
    bands[round(edge, 1)] = bands.get(round(edge, 1), 0) + 1
print("  " + ", ".join(f"{edge:.1f}: {n}" for edge, n in sorted(bands.items())))
for name, chosen in (("high", [x["ms"] for x in steady if x["ms"] <= HIGH]),
                     ("fast", [x["ms"] for x in steady if HIGH < x["ms"] < FAST_TOP]),
                     ("slow", [x["ms"] for x in steady if x["ms"] >= SLOW])):
    chosen.sort()
    q = statistics.quantiles(chosen, n=20, method="inclusive")
    print(f"- {name}: {len(chosen)} intervals, 5th to 95th percentile {q[0]:.3f} to {q[-1]:.3f} ms")

heading("Medians by interval level")
group4 = [x for r in TIMED if r["group"] == 4 for x in r["steady"]]
for name, chosen in (("high", [x for x in steady if x["ms"] <= HIGH]),
                     ("between the cuts, whole night", [x for x in steady if HIGH < x["ms"] < SLOW]),
                     ("between the cuts, group 4", [x for x in group4 if HIGH < x["ms"] < SLOW]),
                     ("between the cuts, group 4, fast class only", [x for x in group4 if HIGH < x["ms"] < FAST_TOP]),
                     ("slow", [x for x in steady if x["ms"] >= SLOW])):
    perf = [median(v.get("perf") for v in host_in(x["start"], x["end"])) for x in chosen]
    print(f"- {name}: {len(chosen)} intervals, median {median(x['ms'] for x in chosen):.3f} ms per sync, median busy share "
          f"{median(x['busy'] for x in chosen):.3f}, median processor performance {median(perf):.2f} percent")
pairs = []
for x in steady:
    samples = [v["perf"] for v in host_in(x["start"], x["end"]) if "perf" in v]
    if samples:
        pairs.append((statistics.fmean(samples), x["ms"]))
print(f"- correlation of processor performance (mean of the interval's samples) with ms per sync over {len(pairs)} "
      f"intervals: {statistics.correlation([p for p, _ in pairs], [m for _, m in pairs]):.3f}")
print()
print("Events written per WAL sync over the sampler's life, runs grouped by the plan's mark on their mean:")
for mark in ("high", "slow"):
    chosen = [r for r in TIMED if s.level(r["ms"]) == mark]
    values = [r["record"]["events_written"] / (r["stats"][-1][1]["wal_sync"] - r["stats"][0][1]["wal_sync"]) for r in chosen]
    print(f"- {mark}: " + ", ".join(f"{r['block']} {v:.3f}" for r, v in zip(chosen, values)) + f"; median {median(values):.3f}")

# The slow stretches. A stretch starts at the first of three or more
# consecutive slow intervals (counting each bench's first and last), or at a
# block of slow intervals that runs to a bench's end and is followed by a slow
# check. A slow block at a bench's start after a slow check continues the
# stretch before it. A stretch ends at its last slow interval before three
# consecutive intervals under the fast level's top, or at a bench's end that an
# ordinary check follows.
heading("The slow stretches")
print("Start rule: the first of three or more consecutive slow intervals, counting each bench's first and last, or a "
      "slow block that runs to a bench's end and is followed by a check at 4.3 ms or more; a slow block at a bench's "
      "start after such a check continues the stretch before it. The diagnostic's intervals are left out.")
print()
STRETCHES, ISOLATED = [], []
current = None
for r in TIMED:
    rows = r["all"]
    after = check_after(r["end"])
    if current is not None and not (rows and rows[0]["ms"] >= SLOW):
        current = None
    i = 0
    while i < len(rows):
        if current is None:
            if rows[i]["ms"] < SLOW:
                i += 1
                continue
            j = i
            while j < len(rows) and rows[j]["ms"] >= SLOW:
                j += 1
            if j - i >= 3 or (j == len(rows) and after is not None and after["ms"] >= SLOW):
                current = {"start": rows[i]["start"], "first_run": r["block"], "group": r["group"], "rows": [], "runs": []}
                STRETCHES.append(current)
            else:
                ISOLATED.append((r["block"], rows[i:j]))
                i = j
                continue
        if r["block"] not in current["runs"]:
            current["runs"].append(r["block"])
        under = 0
        while i < len(rows):
            current["rows"].append(rows[i])
            under = under + 1 if rows[i]["ms"] < FAST_TOP else 0
            i += 1
            if under >= 3:
                current = None
                break
    if current is not None and (after is None or after["ms"] < SLOW):
        current = None
for st in STRETCHES:
    slow_rows = [x for x in st["rows"] if x["ms"] >= SLOW]
    st["end"] = slow_rows[-1]["end"]
    st["load_min"] = sum(x["seconds"] for x in st["rows"] if x["start"] < st["end"]) / 60
    st["slow_min"] = sum(x["seconds"] for x in slow_rows) / 60
for n, st in enumerate(STRETCHES, 1):
    print(f"{n}. starts {hms(st['start'])} in {st['first_run']} (group {st['group']}), runs {', '.join(st['runs'])}; last "
          f"slow interval ends {hms(st['end'])}; under load from start to that end {st['load_min']:.2f} min, of it slow "
          f"{st['slow_min']:.2f} min")
print(f"- isolated slow blocks: " + "; ".join(f"{b} {len(x)} at {hms(x[0]['start'])}" for b, x in ISOLATED))
starts4 = [st["start"] for st in STRETCHES if st["group"] == 4]
spacing = [(b - a).total_seconds() / 60 for a, b in zip(starts4, starts4[1:])]
print(f"- group 4 starts: {len(starts4)}; spacing {min(spacing):.4f} to {max(spacing):.4f} min")
early = [st["start"] for st in STRETCHES if st["group"] < 4]
print(f"- spacing of the starts before group 4: " + ", ".join(f"{(b - a).total_seconds() / 60:.2f}"
                                                             for a, b in zip(early, early[1:] + starts4[:1])) + " min")
print(f"- under load, whole night: {min(st['load_min'] for st in STRETCHES):.2f} to {max(st['load_min'] for st in STRETCHES):.2f} min; "
      f"group 4: {min(st['load_min'] for st in STRETCHES if st['group'] == 4):.2f} to "
      f"{max(st['load_min'] for st in STRETCHES if st['group'] == 4):.2f} min")


# The cumulative work counters through the night: disk 0's bytes and write
# operations, integrated from typeperf's per-second rates, and PostgreSQL's WAL
# syncs from every sampler stats line, which count on across the runs.
def integrate(key):
    out, total, previous = [], 0.0, None
    for t, v in HOST:
        if previous is not None and key in v:
            total += v[key] * (t - previous).total_seconds()
        out.append((t, total))
        previous = t
    return out


def at(series, t):
    for (ta, a), (tb, b) in zip(series, series[1:]):
        if ta <= t <= tb:
            return a + (b - a) * (t - ta).total_seconds() / max(1e-9, (tb - ta).total_seconds())
    return None


def when(series, target, after):
    for (ta, a), (tb, b) in zip(series, series[1:]):
        if tb >= after and a <= target <= b:
            return ta + (tb - ta) * ((target - a) / (b - a) if b > a else 0)
    return None


SYNCS = sorted((t, v["wal_sync"]) for r in RUNS for t, v in r["stats"])
SERIES = {"disk 0 GiB": ([(t, v / 2**30) for t, v in integrate("pd_write_bytes")], 3),
          "disk 0 write operations, millions": ([(t, v / 1e6) for t, v in integrate("pd_writes")], 6),
          "WAL syncs, thousands": ([(t, v / 1e3) for t, v in SYNCS], 3)}
b2_stretch = next(st for st in STRETCHES if st["first_run"] == "b2")
b2_left = next(x["end"] for x in reversed([x for x in BY["b2"]["all"] if x["start"] < b2_stretch["start"]])
               if x["ms"] <= HIGH)
CHOICES = {"each start at its stretch's start": [st["start"] for st in STRETCHES],
           "b2's from the end of its last high interval": [b2_left if st is b2_stretch else st["start"] for st in STRETCHES]}

heading("Cycles between stretch starts")
print(f"b2's last interval at the high level ended {hms(b2_left)}; its stretch starts {hms(b2_stretch['start'])}.")
for name, starts in CHOICES.items():
    first4 = next(i for i, st in enumerate(STRETCHES) if st["group"] == 4)
    print()
    print(f"### Starts: {name}")
    print()
    print("Starts: " + ", ".join(hms(t) for t in starts))
    minutes = [(b - a).total_seconds() / 60 for a, b in zip(starts, starts[1:])]
    g4 = minutes[first4:]
    mean = statistics.fmean(g4)
    print(f"- minutes: before group 4 " + ", ".join(f"{v:.3f} ({v / mean:.3f})" for v in minutes[:first4])
          + f"; group 4 {min(g4):.3f} to {max(g4):.3f}, mean {mean:.3f}, coefficient of variation "
          f"{100 * statistics.stdev(g4) / mean:.2f} percent; inside group 4's range: "
          f"{sum(1 for v in minutes[:first4] if min(g4) <= v <= max(g4))} of {first4}")
    for label, (series, digits) in SERIES.items():
        cycles = [at(series, b) - at(series, a) for a, b in zip(starts, starts[1:])]
        g4 = cycles[first4:]
        mean = statistics.fmean(g4)
        lo, hi = min(g4), max(g4)
        print(f"- {label}: before group 4 " + ", ".join(f"{v:.{digits}f} ({v / mean:.4f})" for v in cycles[:first4])
              + f"; group 4 {lo:.{digits}f} to {hi:.{digits}f} ({lo / mean:.4f} to {hi / mean:.4f}), mean {mean:.{digits}f}, "
              f"coefficient of variation {100 * statistics.stdev(g4) / mean:.2f} percent; inside group 4's range: "
              f"{sum(1 for v in cycles[:first4] if lo <= v <= hi)} of {first4}")
        if label.startswith("disk 0 write"):
            exact = [round(v * 1e6) for v in cycles]
            print(f"  exact write operations: before group 4 {exact[:first4]}; group 4 smallest {min(exact[first4:])}, "
                  f"largest {max(exact[first4:])}")
        for a, b in zip(starts[:first4], starts[1:first4 + 1]):
            base = at(series, a)
            p0, p1 = when(series, base + lo, a), when(series, base + hi, a)
            verdict = "inside" if p0 is not None and p1 is not None and p0 <= b <= p1 else "outside"
            print(f"  from {hms(a)}: group 4's range puts the next start at {hms(p0)} to {hms(p1)}; it came at "
                  f"{hms(b)}, {verdict}")

heading("A clock with group 4's spacing")
print("For each cycle before group 4, the window in which one more start would have to fall for every spacing to lie "
      "within group 4's shortest and longest, and the same with a minute of slack at each end; then the benches the "
      "window meets.")
for name, starts in CHOICES.items():
    first4 = next(i for i, st in enumerate(STRETCHES) if st["group"] == 4)
    g4 = [(b - a) for a, b in zip(starts[first4:], starts[first4 + 1:])]
    shortest, longest = min(g4), max(g4)
    print()
    print(f"### Starts: {name} (group 4's spacing {shortest.total_seconds() / 60:.3f} to {longest.total_seconds() / 60:.3f} min)")
    print()
    for a, b in zip(starts[:first4], starts[1:first4 + 1]):
        length = (b - a).total_seconds() / 60
        for slack in (timedelta(0), timedelta(minutes=1)):
            lo = max(a + shortest - slack, b - longest - slack)
            hi = min(a + longest + slack, b - shortest + slack)
            if lo <= hi:
                met = [r["block"] for r in RUNS if r["start"] < hi and r["end"] > lo]
                text = f"{hms(lo)} to {hms(hi)}, meeting benches {met or 'none'}"
            else:
                text = "empty"
            print(f"- cycle {hms(a)} to {hms(b)} ({length:.3f} min; twice the longest spacing is "
                  f"{2 * longest.total_seconds() / 60:.3f}), slack {slack.total_seconds() / 60:.0f} min: {text}")

heading("Bytes and write operations between benches, and over the gaps")
benches = [(r["start"], r["end"]) for r in RUNS]
a, b = starts4[0], starts4[-1]
for label, key in (("bytes", "pd_write_bytes"), ("write operations", "pd_writes")):
    series = integrate(key)
    total = at(series, b) - at(series, a)
    inside = sum(at(series, min(e, b)) - at(series, max(x, a)) for x, e in benches if e > a and x < b)
    print(f"- group 4, {hms(a)} to {hms(b)}: share of disk 0's {label} between benches {100 * (total - inside) / total:.2f} percent")
gib = integrate("pd_write_bytes")
ops = integrate("pd_writes")
for record in STATUS["runs"]:
    if record.get("kind") == "idle":
        x, e = from_local(record["started"]), from_local(record["finished"])
        print(f"- gap {record['label'].rsplit('-', 1)[1]}, {hms(x)} to {hms(e)}: {(at(gib, e) - at(gib, x)) / 2**30:.3f} GiB, "
              f"{at(ops, e) - at(ops, x):,.0f} write operations")

heading("The storage checks")
print("Each check: local start, what it preceded, fdatasync operations per second, and 1000 over that rate in ms; for a "
      "check at 4.3 ms or more, the last bench interval at 4.3 ms or more before it (counting each bench's first and "
      "last), and the minutes between that interval's end and the check.")
print()
every = sorted((x for r in TIMED for x in r["all"]), key=lambda x: x["start"])
for c in CHECKS:
    line = f"- {hms(c['at'])} before {c['label']}: {c['ops']:.3f} ops/s, {c['ms']:.3f} ms"
    if c["ms"] >= SLOW:
        last = next((x for x in reversed(every) if x["end"] <= c["at"] and x["ms"] >= SLOW), None)
        line += f"; last slow interval ended {hms(last['end'])}, {(c['at'] - last['end']).total_seconds() / 60:.2f} min before"
    print(line)
slow_checks = [c for c in CHECKS if c["ms"] >= SLOW]
print(f"- checks at 4.3 ms or more: {len(slow_checks)}, {min(c['ops'] for c in slow_checks):.3f} to "
      f"{max(c['ops'] for c in slow_checks):.3f} ops/s")
print()
print("Bench ends during a stretch: runs whose last bench interval is slow, and the check that followed.")
ends = [r for r in TIMED if r["all"] and r["all"][-1]["ms"] >= SLOW]
for r in ends:
    c = check_after(r["end"])
    print(f"- {r['block']}: last interval {r['all'][-1]['ms']:.3f} ms at {hms(r['all'][-1]['start'])}; next check "
          f"{hms(c['at'])}, {c['ms']:.3f} ms")
print(f"- {len(ends)} bench ends; followed by a check under 4.3 ms: {[r['block'] for r in ends if check_after(r['end'])['ms'] < SLOW]}")
print()
d1_stretch = next(st for st in STRETCHES if st["first_run"] in ("b4", "d1"))
d2 = BY["d2"]
before, after = check_before(d2["start"]), check_after(d2["end"])
print(f"The stretch on by {hms(d1_stretch['start'])}: the check before the diagnostic, {hms(before['at'])}, read "
      f"{before['ms']:.3f} ms, {(before['at'] - d1_stretch['start']).total_seconds() / 60:.2f} min after the start; the "
      f"check after it, {hms(after['at'])}, read {after['ms']:.3f} ms, {(after['at'] - d1_stretch['start']).total_seconds() / 60:.2f} min after.")
print()
print("Checks by level: the median of 1000 over each check's rate against the median loaded time per sync.")
d3_start = BY["d3"]["start"]
first_g4 = min(r["start"] for r in TIMED if r["group"] == 4)
gap1 = next((c for c in CHECKS if c["label"] == "g1"), None)
high_checks = [c for c in CHECKS if c["at"] <= d3_start and c["ms"] < SLOW and c is not gap1]
fast_checks = [c for c in CHECKS if c["at"] >= first_g4 - timedelta(minutes=2) and c["ms"] < SLOW and c["label"] != "end"]
print(f"- high: {len(high_checks)} checks from the start to d3's, outside the stretches, gap 1's start left out "
      f"({gap1['ms']:.3f} ms): median {median(c['ms'] for c in high_checks):.3f} ms, against "
      f"{median(x['ms'] for x in steady if x['ms'] <= HIGH):.3f} loaded")
print(f"- fast: {len(fast_checks)} group 4 checks outside the stretches, the end check left out "
      f"({CHECKS[-1]['ms']:.3f} ms): median {median(c['ms'] for c in fast_checks):.3f} ms, against "
      f"{median(x['ms'] for x in group4 if HIGH < x['ms'] < SLOW):.3f} loaded (group 4 between the cuts)")
print(f"- slow: {len(slow_checks)} checks: median {median(c['ms'] for c in slow_checks):.3f} ms, against "
      f"{median(x['ms'] for x in steady if x['ms'] >= SLOW):.3f} loaded")

heading("Docker's virtual machine and disk 0 by interval level")
print("Medians over the steady intervals of the mean of the host's samples in each: the virtual machine's cores, and "
      "disk 0's writes in MiB per second and in millions of bytes per second.")
print()
for name, test in (("high", lambda m: m <= HIGH), ("between the cuts", lambda m: HIGH < m < SLOW), ("slow", lambda m: m >= SLOW)):
    vm, written = [], []
    for x in steady:
        if not test(x["ms"]):
            continue
        samples = host_in(x["start"], x["end"])
        if samples:
            vm.append(statistics.fmean(v["vm"] for v in samples if "vm" in v) / 100)
            written.append(statistics.fmean(v["pd_write_bytes"] for v in samples if "pd_write_bytes" in v))
    print(f"- {name}: {len(vm)} intervals; VM {median(vm):.3f} cores; disk 0 {median(written) / 2**20:.3f} MiB/s, "
          f"{median(written) / 1e6:.3f} million bytes/s")

heading("The host's counters at each stretch onset")
print("Rule for a clean onset: the three bench intervals before the stretch's first slow interval are all under "
      f"{SLOW} ms, the earliest of them is under {FAST_TOP}, and the bench runs at least 40 seconds past the slow "
      "interval's start, so that its winding down is not read as a move. For each clean onset, each counter's level before is "
      "its median over the samples from 60 to 10 seconds before the slow interval's start, and its level after its "
      "median over the samples from that start to 40 seconds after it, ending no later than 5 seconds after the bench; "
      "the counter moves at the first sample, from 30 seconds before the slow interval's start, that is past halfway "
      "between the two levels together with the two samples after it. Moves are given in samples from the first "
      "sample at or after the slow interval's start. Below that, each onset's samples: local time, processor "
      "performance, disk 0's write latency in ms, and its writes per second.")
COUNTERS = (("clock", "perf", -1), ("latency", "pd_write_s", 1), ("write rate", "pd_writes", -1))
clean_count = 0
TALLY = []
for st in STRETCHES:
    run = next(r for r in TIMED if any(x is st["rows"][0] for x in r["all"]))
    rows = run["all"]
    i = next(k for k, x in enumerate(rows) if x is st["rows"][0])
    prior = rows[max(0, i - 3):i]
    t0 = st["rows"][0]["start"]
    clean = (len(prior) == 3 and all(x["ms"] < SLOW for x in prior) and prior[0]["ms"] < FAST_TOP
             and run["end"] >= t0 + timedelta(seconds=40))
    print()
    print(f"### {hms(t0)} in {run['block']}: {'clean' if clean else 'not clean'}")
    window = [(t, v) for t, v in HOST if t0 - timedelta(seconds=60) <= t < min(t0 + timedelta(seconds=40), run["end"] + timedelta(seconds=5))]
    first = next((k for k, (t, _) in enumerate(window) if t >= t0), None)
    if clean:
        clean_count += 1
        moved = {}
        for name, key, direction in COUNTERS:
            pre = median(v.get(key) for t, v in window if t0 - timedelta(seconds=60) <= t < t0 - timedelta(seconds=10))
            post = median(v.get(key) for t, v in window if t >= t0)
            half = (pre + post) / 2
            past = [v.get(key) is not None and (v[key] < half if direction < 0 else v[key] > half) for _, v in window]
            k = next((k for k in range(len(window) - 2)
                      if window[k][0] >= t0 - timedelta(seconds=30) and past[k] and past[k + 1] and past[k + 2]), None)
            moved[name] = None if k is None or first is None else k - first
            print(f"- {name}: before {pre:.4g}, after {post:.4g}; moves at sample {moved[name]}")
        order = "same sample" if moved["clock"] == moved["write rate"] else (
            "write rate first" if (moved["write rate"] or 0) < (moved["clock"] or 0) else "clock first")
        lag = "none" if moved["latency"] is None or moved["clock"] is None else f"{moved['latency'] - moved['clock']:+d} samples"
        print(f"- clock against write rate: {order}; latency against clock: {lag}")
        TALLY.append((hms(t0), moved))
    for t, v in window:
        mark = " <" if first is not None and t == window[first][0] else ""
        print(f"    {hms(t)} perf {v.get('perf', float('nan')):7.2f} latency {1000 * v.get('pd_write_s', float('nan')):6.3f} "
              f"writes {v.get('pd_writes', float('nan')):8.1f}{mark}")
print()
print(f"Clean onsets: {clean_count} of {len(STRETCHES)}. The clock against disk 0's write rate, in samples (negative "
      "when the clock moved first):")
for name, moved in TALLY:
    found = [v for v in moved.values() if v is not None]
    print(f"- {name}: clock {moved['clock']}, write rate {moved['write rate']}, latency {moved['latency']} "
          f"(None: no step by the rule); widest spread {max(found) - min(found)} samples")
both = [m for _, m in TALLY if m["clock"] is not None and m["write rate"] is not None]
found = [v for _, m in TALLY for v in m.values() if v is not None]
print(f"- every move found falls from sample {min(found)} to sample {max(found)}")
print(f"- clock and write rate in the same sample: {sum(1 for m in both if m['clock'] == m['write rate'])}; write rate "
      f"first: {sum(1 for m in both if m['write rate'] < m['clock'])}; clock first: "
      f"{sum(1 for m in both if m['clock'] < m['write rate'])}")
lat = [m for _, m in TALLY if m["latency"] is not None and m["clock"] is not None]
print(f"- latency in the clock's sample: {sum(1 for m in lat if m['latency'] == m['clock'])}; before it: "
      f"{sum(1 for m in lat if m['latency'] < m['clock'])}; after it: {sum(1 for m in lat if m['latency'] > m['clock'])}; "
      f"no step: {sum(1 for _, m in TALLY if m['latency'] is None)}")

heading("Limits and the clock at the checks")
print(f"- samples with % Performance Limit under 100: {sum(1 for _, v in HOST if v.get('limit', 100) < 100)}; with "
      f"Performance Limit Flags not 0: {sum(1 for _, v in HOST if v.get('flags', 0) != 0)}; with a thermal zone's "
      f"passive limit under 100: {sum(1 for _, v in HOST for k, x in v.items() if k.endswith('_passive') and x < 100)}; "
      f"with throttle reasons not 0: {sum(1 for _, v in HOST for k, x in v.items() if k.endswith('_reasons') and x != 0)}; "
      f"of {len(HOST)} samples")
for c in CHECKS:
    samples = host_in(c["at"], c["at"] + timedelta(seconds=6))
    perf = median(v.get("perf") for v in samples)
    print(f"- check {hms(c['at'])} ({c['ms']:.3f} ms): processor performance {perf if perf is None else round(perf, 2)}")

heading("Database CPU per command and the clock")
print("Runs with at least 95 percent of their steady intervals at one level, high or slow: database CPU in ms per "
      "command, the median processor performance over the bench, and the CPU scaled by it.")
print()
groups = {}
for r in TIMED:
    for name, test in (("high", lambda m: m <= HIGH), ("slow", lambda m: m >= SLOW)):
        if r["steady"] and sum(1 for x in r["steady"] if test(x["ms"])) / len(r["steady"]) >= 0.95:
            perf = median(v.get("perf") for v in host_in(r["start"], r["end"]))
            db = r["record"]["cpu_ms_per_command"]["db"]
            groups.setdefault(name, []).append((r["block"], db, perf, db * perf / 100))
for name, rows in groups.items():
    print(f"- {name}: " + "; ".join(f"{b} {db:.3f} ms at {p:.2f} percent, scaled {sc:.3f}" for b, db, p, sc in rows))
hi_db, lo_db = median(x[1] for x in groups["high"]), median(x[1] for x in groups["slow"])
hi_sc, lo_sc = median(x[3] for x in groups["high"]), median(x[3] for x in groups["slow"])
raw_rise, scaled_rise = 100 * (lo_db / hi_db - 1), 100 * (lo_sc / hi_sc - 1)
print(f"- rise from high to slow: {raw_rise:.2f} percent; scaled by the clock {scaled_rise:.2f} percent; the clock's "
      f"share of the rise {100 * (1 - scaled_rise / raw_rise):.1f} percent")

heading("Commands per second by level")
print("csharp-wax-rw8 at 8 workers: the runs with at least 95 percent of their steady intervals high, the runs with no "
      "steady interval high or slow (the fast level), and the runs with at least 95 percent slow; each group's median "
      "commands per second, and each faster group's median over the slow one's.")
print()
rw8 = [r for r in TIMED if r["entry"]["side"] == "csharp-wax-rw8"]
level_runs = {
    "high": [r for r in rw8 if r["steady"] and sum(x["ms"] <= HIGH for x in r["steady"]) / len(r["steady"]) >= 0.95],
    "fast": [r for r in rw8 if r["steady"] and not any(x["ms"] <= HIGH or x["ms"] >= SLOW for x in r["steady"])],
    "slow": [r for r in rw8 if r["steady"] and sum(x["ms"] >= SLOW for x in r["steady"]) / len(r["steady"]) >= 0.95],
}
for name, chosen in level_runs.items():
    print(f"- {name}: {len(chosen)} runs ({', '.join(r['block'] for r in chosen)}), commands/s "
          f"{min(r['rate'] for r in chosen):.2f} to {max(r['rate'] for r in chosen):.2f}, median "
          f"{median(r['rate'] for r in chosen):.2f}")
slow_rate = median(r["rate"] for r in level_runs["slow"])
for name in ("high", "fast"):
    print(f"- {name} over slow: {median(r['rate'] for r in level_runs[name]) / slow_rate:.3f}")

heading("Reading 1 by rate")
low = [(r["block"], x) for r in TIMED for x in r["steady"] if x["rate"] <= 200]
print(f"Steady intervals at 200 WAL syncs per second or less, the diagnostic aside: {len(low)}, from "
      f"{len({b for b, _ in low})} runs ({', '.join(sorted({b for b, _ in low}, key=lambda b: [r['block'] for r in RUNS].index(b)))}); "
      f"least {min(x['ms'] for _, x in low):.3f} ms per sync, median {median(x['ms'] for _, x in low):.3f}; median busy "
      f"share {median(x['busy'] for _, x in low):.3f}; at {HIGH} ms or less: {sum(1 for _, x in low if x['ms'] <= HIGH)}; "
      f"busy under 0.7: {sum(1 for _, x in low if x['busy'] < 0.7)}")

heading("The diagnostic and its neighbors")
for r in RUNS:
    t = r["record"]["cpu_throttling"]["db"]
    print(f"- {r['block']}: throttled {t['throttled_pct']} percent ({t['nr_throttled']} of {t['nr_periods']} periods), "
          f"database {r['record']['cpu_s']['db'] / r['record']['window_s']:.3f} cores, {r['kb_per_sync']:.3f} KB of WAL "
          f"per sync, sampler probe median {r['record']['side_sampler']['probe_ms_median']} ms, sampler "
          f"{100 * r['record']['side_sampler']['cores']:.3f} percent of a core")
others = [r for r in RUNS if not r["diag"]]
print(f"- every run but the diagnostic: throttled at most {max(r['record']['cpu_throttling']['db']['throttled_pct'] for r in others)} "
      f"percent; probe at most {max(r['record']['side_sampler']['probe_ms_median'] for r in others)} ms; sampler "
      f"{min(100 * r['record']['side_sampler']['cores'] for r in others):.3f} to "
      f"{max(100 * r['record']['side_sampler']['cores'] for r in others):.3f} percent of a core; WAL per sync "
      f"{min(r['kb_per_sync'] for r in others):.3f} to {max(r['kb_per_sync'] for r in others):.3f} KB")
for block in ("d2", "b1", "b3", "b4"):
    r = BY[block]
    samples = host_in(r["start"], r["end"])
    print(f"- {block} over its bench: host busy median {median(v.get('busy') for v in samples):.2f} percent, VM median "
          f"{median(v.get('vm') for v in samples) / 100:.3f} cores, processor performance median "
          f"{median(v.get('perf') for v in samples):.2f} percent")
d2 = BY["d2"]
for block in ("d1", "d3", "b1", "b3", "b4"):
    print(f"- d2 against {block}: {100 * (d2['rate'] / BY[block]['rate'] - 1):+.2f} percent "
          f"({d2['rate']:.1f} against {BY[block]['rate']:.1f} commands/s)")

heading("Runs and onsets the text names")
for block in ("a1", "b1", "b2", "b3", "b4", "d3"):
    rows = BY[block]["all"]
    print(f"- {block}: bench intervals from {hms(rows[0]['start'])}: " + ", ".join(f"{x['ms']:.2f}" for x in rows[:6])
          + " ... " + ", ".join(f"{x['ms']:.2f}" for x in rows[-3:]))
a1_high = [x["ms"] for x in BY["a1"]["steady"] if x["ms"] <= HIGH]
a1_last_high = max(x["end"] for x in BY["a1"]["steady"] if x["ms"] <= HIGH)
print(f"- a1's high steady intervals: {min(a1_high):.3f} to {max(a1_high):.3f} ms, through {hms(a1_last_high)}, "
      f"{(a1_last_high - BY['a1']['start']).total_seconds() / 60:.2f} min into its bench; its bench ended "
      f"{(BY['a1']['end'] - a1_last_high).total_seconds() / 60:.2f} min later")
print(f"- b2 left the high level {(b2_left - BY['b2']['start']).total_seconds():.1f} s into its bench")
c11 = BY["c11"]["all"]
k = next(i for i, x in enumerate(c11) if x["ms"] >= SLOW)
print(f"- c11 around its onset: " + ", ".join(f"{hms(x['start'])} {x['ms']:.3f}" for x in c11[k - 3:k + 2])
      + f"; last step {100 * (c11[k]['ms'] / c11[k - 1]['ms'] - 1):.1f} percent")
d3_high = [x for x in BY["d3"]["all"] if x["ms"] <= HIGH]
print(f"- d3 high through the interval from {hms(d3_high[-1]['start'])}; next interval {BY['d3']['all'][len(d3_high)]['ms']:.3f} ms "
      f"from {hms(BY['d3']['all'][len(d3_high)]['start'])}")

heading("The process snapshot's own cost")
own = [float(x["cpu_s"]) for x in PROCESSES if x["name"].lower() == "powershell.exe" and x["ours"] == "1"]
query = [float(x["cpu_s"]) for x in PROCESSES if x["name"] == "_snapshot"]
print(f"- its own process: mean {statistics.fmean(own):.4f} CPU seconds per interval over {len(own)} intervals")
print(f"- its query: mean {statistics.fmean(query):.4f} seconds per interval over {len(query)} intervals")
