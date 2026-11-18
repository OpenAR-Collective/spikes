"""Prints the figures round 2's write-up takes from the follow-up tests (plan
round2-followup) beyond the tables summarize_followup.py prints: the analysis
added after the results were in, in the order the write-up uses it, and the
figures it takes from earlier passes' records that round2-sync-rates.md does
not print. Figures carry more digits than the write-up gives, so its rounding
can be checked. Times are the laptop's local time, by the offset in the host
counters' heading, so the output does not depend on the machine that runs it.

    python -I posthoc_followup.py > round2-followup.posthoc.txt

It reads only committed records: the results folders named in the plans of
round2-followup, round2-shape, round2-diag, and round2-fix, found by each
plan's labels, and the round2-followup files beside this script.
"""

import csv
import json
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import summarize_followup as s  # noqa: E402

sys.stdout.reconfigure(newline="\n")

with s.HOST.open(encoding="utf-8", errors="replace", newline="") as handle:
    HEADING = next(csv.reader(handle))[0]
OFFSET = timedelta(minutes=int(re.search(r"\((-?\d+)\)\s*$", HEADING).group(1)))
HOST = sorted(s.host_counters(), key=lambda pair: pair[0])
BURST_BUSY, BURST_SAMPLES, BURST_VM, BURST_MERGE = 80, 3, 5, 90
DIAG_FROM, THROTTLE_CUT = "23:50", 20


def local(t):
    return (t - OFFSET).replace(tzinfo=None)


def hms(t):
    return "" if t is None else f"{local(t):%H:%M:%S}"


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def band(syncs_per_second):
    """The rate bands the write-up uses where syncs were not timed."""
    if syncs_per_second >= 340:
        return "high"
    if 280 <= syncs_per_second <= 320:
        return "fast"
    if syncs_per_second <= 200:
        return "slow"
    return "not marked"


def heading(title):
    print()
    print(f"## {title}")
    print()


def share(looks, test):
    return statistics.fmean(sum(n for k, n in sample.items() if test(k)) for _, sample in looks) if looks else None


IS_SYNC = lambda k: k.endswith("/IO/WalSync")  # noqa: E731
IS_LOCK = lambda k: k.startswith("client_backend/active/LWLock/WALWrite")  # noqa: E731
IS_CORE = lambda k: k.startswith("client_backend/active/none/")  # noqa: E731
IS_IDLE = lambda k: k.startswith("client_backend/idle_in_transaction/")  # noqa: E731


def record_of(label):
    found = sorted(s.RESULTS.glob(f"*-{label}/results.json"))
    return (found[0].parent, json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]) if found else (None, None)


def sampled(folder, record):
    """Over the side sampler's life: WAL syncs per second, the share of looks in
    a WAL sync, the time per sync that share implies, and the backends per look
    queued for the WAL write lock, on a core, and idle in a transaction."""
    waits, probes, stats = s.sampler(folder)
    t0, t1 = stats[0][0], stats[-1][0]
    looks = [(t, c) for t, c in waits if t0 < t <= t1]
    syncs = stats[-1][1]["wal_sync"] - stats[0][1]["wal_sync"]
    rate = syncs / (t1 - t0).total_seconds()
    sync_share = share(looks, IS_SYNC)
    wal = record["db_activity"]["wal"]
    return {"waits": waits, "probes": probes, "stats": stats, "life_syncs": syncs, "life_rate": rate,
            "share": sync_share, "implied_ms": 1000 * sync_share / rate, "lock": share(looks, IS_LOCK),
            "core": share(looks, IS_CORE), "idle": share(looks, IS_IDLE),
            "per_sync": record["events_written"] / syncs if record.get("events_written") else None,
            "syncs_s": wal["wal_sync"] / record["window_s"], "kb_per_sync": wal["wal_bytes"] / 1024 / wal["wal_sync"]}


RUNS = []
for order, entry in enumerate(s.PLAN["runs"], 1):
    folder, record = record_of(entry["label"])
    if not folder:
        continue
    loaded = s.load(entry, HOST)
    RUNS.append({"order": order, "entry": entry, "name": s.short(entry), "group": entry.get("group"), "folder": folder,
                 "record": record, "loaded": loaded, "workers": record["workers"],
                 "start": s.utc(record["clock"]["start"].replace(" ", "T").replace("+00", "Z")),
                 "end": s.utc(record["clock"]["end"].replace(" ", "T").replace("+00", "Z")),
                 **sampled(folder, record)})

print("# Round 2's follow-up tests: figures added after the results")
print()
print(f"Times are local, UTC less {OFFSET.total_seconds() / 3600:.0f} hours, from the host counters' heading. Where "
      "syncs were not timed, a run at 8 workers is high at 340 WAL syncs per second or more, fast at 280 to 320, slow at "
      "200 or less, and not marked otherwise. The implied time per sync is the share of the side sampler's "
      "once-a-second looks at which a backend waited in a WAL sync, over WAL syncs per second, both over the sampler's "
      "life; one process syncs at a time.")

heading("Every run: WAL syncs, the implied time per sync, and the queue")
print("Order, run, local start, commands per second, WAL syncs per second over the harness's window and its band, "
      "events written per WAL sync over the sampler's life, KB of WAL per sync, the share of looks in a WAL sync and the "
      "time per sync it implies, and backends per look queued for the WAL write lock and on a core.")
print()
for r in RUNS:
    mark = band(r["syncs_s"]) if r["workers"] == 8 else "not marked at 1 worker"
    print(f"- {r['order']} {r['name']} {hms(r['start'])}: {r['loaded']['rate']:.2f} commands/s; {r['syncs_s']:.2f} syncs/s, "
          f"{mark}; {r['per_sync']:.3f} events per sync; {r['kb_per_sync']:.3f} KB per sync; share {r['share']:.4f}, "
          f"implied {r['implied_ms']:.3f} ms; lock queue {r['lock']:.3f}, on a core {r['core']:.3f}")
eight = [r for r in RUNS if r["workers"] == 8]
group2 = [r for r in RUNS if r["group"] == 2]
for label, chosen in (("every run at 8 workers", eight), ("group 2", group2)):
    print(f"- {label}, {len(chosen)} runs, {hms(min(r['start'] for r in chosen))} to {hms(max(r['start'] for r in chosen))}: "
          f"commands/s {min(r['loaded']['rate'] for r in chosen):.2f} to {max(r['loaded']['rate'] for r in chosen):.2f}; "
          f"syncs/s {min(r['syncs_s'] for r in chosen):.2f} to {max(r['syncs_s'] for r in chosen):.2f}; share "
          f"{min(r['share'] for r in chosen):.4f} to {max(r['share'] for r in chosen):.4f}; implied "
          f"{min(r['implied_ms'] for r in chosen):.3f} to {max(r['implied_ms'] for r in chosen):.3f} ms; events per sync "
          f"{min(r['per_sync'] for r in chosen):.3f} to {max(r['per_sync'] for r in chosen):.3f}; lock queue "
          f"{min(r['lock'] for r in chosen):.3f} to {max(r['lock'] for r in chosen):.3f}; on a core "
          f"{min(r['core'] for r in chosen):.3f} to {max(r['core'] for r in chosen):.3f}; processor performance "
          f"{min(r['loaded']['perf'] for r in chosen):.2f} to {max(r['loaded']['perf'] for r in chosen):.2f} percent")

heading("Bursts of load on the Windows side")
print(f"A burst is {BURST_SAMPLES} or more consecutive host samples at {BURST_BUSY} percent busy or more (Processor(_Total) "
      f"% Processor Time) while Docker's virtual machine used under {BURST_VM} cores, so that most of the load was "
      f"Windows' own; bursts less than {BURST_MERGE} seconds apart count as one. For each: its span from the first such "
      "sample to the last; Windows busy, the virtual machine's cores, and processor performance over its samples, as "
      "range and median; the side sampler's CPU probes in it; and the time per WAL sync implied over the stats lines "
      "inside it, in all and the most in one 10-second pair.")
print()
bursts, run_ = [], []
for t, v in HOST:
    if v.get("busy", 0) >= BURST_BUSY and v.get("vm", 0) / 100 < BURST_VM:
        run_.append((t, v))
    else:
        if len(run_) >= BURST_SAMPLES:
            bursts.append(run_)
        run_ = []
if len(run_) >= BURST_SAMPLES:
    bursts.append(run_)
merged = []
for samples in bursts:
    if merged and (samples[0][0] - merged[-1][-1][0]).total_seconds() < BURST_MERGE:
        merged[-1] = merged[-1] + samples
    else:
        merged.append(samples)
bursts = merged
for samples in bursts:
    a, b = samples[0][0], samples[-1][0]
    inside = [r for r in RUNS if r["start"] - timedelta(minutes=5) < b and r["end"] + timedelta(minutes=5) > a]
    probes = [p for r in RUNS for t, p in r["probes"] if a <= t <= b]
    stats = sorted((t, v) for r in RUNS for t, v in r["stats"] if a <= t <= b)
    looks = [(t, c) for r in RUNS for t, c in r["waits"] if a <= t <= b]
    whole = None
    peaks = []
    if len(stats) > 1:
        rate = (stats[-1][1]["wal_sync"] - stats[0][1]["wal_sync"]) / (stats[-1][0] - stats[0][0]).total_seconds()
        in_span = [x for x in looks if stats[0][0] < x[0] <= stats[-1][0]]
        whole = 1000 * share(in_span, IS_SYNC) / rate if rate and in_span else None
        for (ta, x), (tb, y) in zip(stats, stats[1:]):
            pair_rate = (y["wal_sync"] - x["wal_sync"]) / (tb - ta).total_seconds()
            pair_looks = [c for c in looks if ta < c[0] <= tb]
            if pair_rate > 0 and pair_looks:
                peaks.append(1000 * share(pair_looks, IS_SYNC) / pair_rate)
    busy = [v["busy"] for _, v in samples]
    vm = [v["vm"] / 100 for _, v in samples if "vm" in v]
    perf = [v["perf"] for _, v in samples if "perf" in v]
    print(f"- {hms(a)} to {hms(b)} ({len(samples)} samples; near runs {[r['order'] for r in inside]}): busy "
          f"{min(busy):.2f} to {max(busy):.2f} percent, median {median(busy):.2f}; VM {min(vm):.2f} to {max(vm):.2f} "
          f"cores, median {median(vm):.2f}; processor performance {min(perf):.2f} to {max(perf):.2f} percent, median "
          f"{median(perf):.2f}; probes {len(probes)}, most {max(probes) if probes else None} ms; "
          f"implied time per sync over the span {whole if whole is None else round(whole, 2)} ms, most in one 10-second "
          f"pair {round(max(peaks), 2) if peaks else None} ms")
print(f"- bursts: {len(bursts)}")

heading("The one-worker pair")
pair = [r for r in RUNS if r["workers"] == 1]
for r in pair:
    stats, waits = r["stats"], r["waits"]
    windows = []
    for i in range(0, len(stats) - 60, 60):
        (ta, a), (tb, b) = stats[i], stats[i + 60]
        looks = [(t, c) for t, c in waits if ta < t <= tb]
        rate = (b["wal_sync"] - a["wal_sync"]) / (tb - ta).total_seconds()
        windows.append(1000 * share(looks, IS_SYNC) / rate)
    rate = r["loaded"]["rate"]
    plan = [st for c in r["record"]["bench"].get("plan_choices") or [] for st in c["statements"]
            if st["statement"].upper().startswith(s.INSERT)]
    print(f"- {r['name']}: {rate:.3f} commands/s; implied {r['implied_ms']:.3f} ms per sync, by 10-minute window "
          + ", ".join(f"{w:.3f}" for w in windows)
          + f"; {r['kb_per_sync']:.3f} KB per sync; per command, ms in a WAL sync {1000 * r['share'] / rate:.3f}, idle in a "
          f"transaction {1000 * r['idle'] / rate:.3f}, on a core {1000 * r['core'] / rate:.3f}; WAL syncs per command "
          f"{r['life_syncs'] / r['record']['bench']['succeeded']:.4f}; insert plans "
          + ", ".join(f"generic {st['generic_plans']} custom {st['custom_plans']}" for st in plan))
if len(pair) == 2:
    a, b = pair
    for key, label in (("idle", "idle in a transaction"), ("core", "on a core"), ("share", "in a WAL sync")):
        print(f"- {label}: {b['name']} less {a['name']}: "
              f"{1000 * b[key] / b['loaded']['rate'] - 1000 * a[key] / a['loaded']['rate']:+.3f} ms per command")
    print(f"- time per command: {a['name']} {1000 / a['loaded']['rate']:.3f} ms, {b['name']} {1000 / b['loaded']['rate']:.3f} ms, "
          f"difference {1000 / b['loaded']['rate'] - 1000 / a['loaded']['rate']:+.3f} ms")
first = RUNS[0]
print(f"- the night's first run, {first['name']}: {first['kb_per_sync']:.3f} KB per sync, implied {first['implied_ms']:.3f} ms")

# The diagnostic pass's one-worker pair, read with this pair's time outside the
# sync: time per command less the time in WAL syncs per command.
diag_plan = json.loads((HERE / "round2-diag.plan.json").read_text(encoding="utf-8"))
print()
print("The diagnostic pass's pair, if each side's time outside the WAL sync per command was as in this pass: the implied "
      "time per sync is its time per command less that outside time, over its WAL syncs per command.")
for r in pair:
    side = r["record"]["side"]
    outside = 1000 / r["loaded"]["rate"] - 1000 * r["share"] / r["loaded"]["rate"]
    match = [e for e in diag_plan["runs"] if e["side"] == side and e.get("workers") == 1]
    for e in match:
        folder, record = record_of(e["label"])
        if not record:
            continue
        rate = record["bench"]["succeeded"] / record["bench"]["elapsed_s"]
        wal = (record.get("db_activity") or {}).get("wal") or {}
        per_command = wal.get("wal_sync", 0) / record["bench"]["succeeded"] if wal else None
        start = s.utc(record["clock"]["start"].replace(" ", "T").replace("+00", "Z"))
        implied = (1000 / rate - outside) / per_command if per_command else None
        print(f"- {side}: outside the sync here {outside:.3f} ms per command; diagnostic pass {e['label']} at "
              f"{hms(start)}, {rate:.3f} commands/s, {per_command if per_command is None else round(per_command, 4)} "
              f"syncs per command, implied {implied if implied is None else round(implied, 3)} ms per sync")

def slow_share(folder):
    """The share of the readers' 10-second pass reports under the slow cut, in percent."""
    rows = s.intervals(folder)
    rates = [ev / (b - a).total_seconds() for a, b, p, ev in rows if (b - a).total_seconds() > 0]
    return f"{100 * sum(x < s.SLOW_EVENTS_PER_SECOND for x in rates) / len(rates):.1f} percent of {len(rates)}" if rates else "none"


heading("The diagnostic night before its one-worker pair")
print(f"Every run of plan round2-diag that started from {DIAG_FROM} local time until its one-worker pair began, in run "
      "order: local start, WAL syncs per second over the harness's window, and the percent of scheduler periods in "
      "which each container was throttled at its CPU limit. A run held at a CPU limit cannot show the flush level, so "
      f"the last run whose containers were all throttled in under {THROTTLE_CUT} percent of periods bounds the switch "
      "from below; the pair's span follows.")
print()
def started(folder):
    """The run's start as its run.md records it, in local time, as round2-sync-rates.md prints it."""
    found = re.search(r"Started (\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)", (folder / "run.md").read_text(encoding="utf-8"))
    return found.group(1).replace("T", " ") if found else None


diag_rows = []
for e in json.loads((HERE / "round2-diag.plan.json").read_text(encoding="utf-8"))["runs"]:
    folder, record = record_of(e["label"])
    if not record:
        continue
    start = s.utc(record["clock"]["start"].replace(" ", "T").replace("+00", "Z"))
    end = s.utc(record["clock"]["end"].replace(" ", "T").replace("+00", "Z"))
    diag_rows.append((start, end, e["label"].replace("round2-diag-", ""), record, started(folder)))
pair_runs = [x for x in diag_rows if x[3].get("workers") == 1]
pair_start, pair_end = min(x[0] for x in pair_runs), max(x[1] for x in pair_runs)
first_day = min(local(x[0]) for x in diag_rows).date()
cut = datetime.combine(first_day, datetime.strptime(DIAG_FROM, "%H:%M").time())
last_free = None
for start, end, label, record, run_md in sorted(diag_rows, key=lambda x: x[0]):
    if local(start) < cut or start >= pair_start:
        continue
    wal = (record.get("db_activity") or {}).get("wal") or {}
    syncs = wal["wal_sync"] / record["window_s"] if wal.get("wal_sync") else None
    throttled = {name: v.get("throttled_pct") for name, v in (record.get("cpu_throttling") or {}).items()}
    free = all(pct is not None and pct < THROTTLE_CUT for pct in throttled.values())
    if free:
        last_free = (start, label, throttled, run_md)
    print(f"- started {run_md} (bench {local(start):%H:%M:%S}) {label}: {syncs if syncs is None else round(syncs, 2)} "
          "syncs/s; throttled " + ", ".join(f"{name} {pct}" for name, pct in throttled.items()))
if last_free:
    print(f"- last run with every container under {THROTTLE_CUT} percent: {last_free[1]}, started {last_free[3]} (bench "
          f"{local(last_free[0]):%H:%M:%S}), " + ", ".join(f"{name} {pct}" for name, pct in last_free[2].items()))
print(f"- the one-worker pair: started {min(x[4] for x in pair_runs)}; benches {local(pair_start):%m-%d %H:%M:%S} to "
      f"{local(pair_end):%m-%d %H:%M:%S}")

heading("The diagnostic night after its one-worker pair")
print("The runs recorded under the pass prefix round2-diag-followup, which has no plan file of its own, found by that "
      "prefix: local start and end, commands per second, WAL syncs per second over the harness's window, and band.")
print()
later = []
for path in s.RESULTS.glob("*-round2-diag-followup-*/results.json"):
    record = json.loads(path.read_text(encoding="utf-8"))["runs"][0]
    wal = (record.get("db_activity") or {}).get("wal") or {}
    if not wal.get("wal_sync"):
        continue
    start = s.utc(record["clock"]["start"].replace(" ", "T").replace("+00", "Z"))
    end = s.utc(record["clock"]["end"].replace(" ", "T").replace("+00", "Z"))
    syncs = wal["wal_sync"] / record["window_s"]
    later.append((start, end, path.parent.name.split("round2-diag-followup-", 1)[1],
                  record["bench"]["succeeded"] / record["bench"]["elapsed_s"], syncs))
for start, end, label, rate, syncs in sorted(later):
    print(f"- {local(start):%m-%d %H:%M} to {local(end):%H:%M} {label}: {rate:.2f} commands/s, {syncs:.2f} syncs/s, {band(syncs)}")

heading("Earlier passes on the payment file at 8 workers with the engine")
print("From each plan's labels: every run's local start and end, commands per second, WAL syncs per second over the "
      "harness's window, and its band; then, per pass, the runs in each band and the span of its slow runs. For the "
      "fix pass's csharp-wax run, the share of its readers' 10-second intervals under "
      f"{s.SLOW_EVENTS_PER_SECOND:,} events per second, the cut that pass used.")
summary = {}
for name in ("round2-shape", "round2-diag", "round2-fix", "round2-followup"):
    plan = json.loads((HERE / f"{name}.plan.json").read_text(encoding="utf-8"))
    rows = []
    for e in plan["runs"]:
        if e.get("kind") == "idle":
            continue
        folder, record = record_of(e["label"])
        if not record or record.get("workers") != 8 or record.get("workload") != "batch-payments" or record.get("engine_paused"):
            continue
        wal = (record.get("db_activity") or {}).get("wal") or {}
        if not wal.get("wal_sync"):
            continue
        start = s.utc(record["clock"]["start"].replace(" ", "T").replace("+00", "Z"))
        end = s.utc(record["clock"]["end"].replace(" ", "T").replace("+00", "Z"))
        rate = record["bench"]["succeeded"] / record["bench"]["elapsed_s"]
        syncs = wal["wal_sync"] / record["window_s"]
        rows.append((start, end, e["label"].replace(name + "-", ""), rate, syncs, band(syncs), folder, record))
    summary[name] = rows
    print()
    print(f"### {name}")
    print()
    for start, end, label, rate, syncs, mark, folder, record in rows:
        print(f"- {local(start):%m-%d %H:%M} to {local(end):%H:%M} {label}: {rate:.2f} commands/s, {syncs:.2f} syncs/s, "
              f"{mark}; readers' intervals under {s.SLOW_EVENTS_PER_SECOND:,} events/s: {slow_share(folder)}")
    counts = {m: sum(1 for x in rows if x[5] == m) for m in ("high", "fast", "not marked", "slow")}
    slow = [x for x in rows if x[5] == "slow"]
    print(f"- runs by band: {counts}; slow runs from {local(slow[0][0]):%m-%d %H:%M} to {local(slow[-1][1]):%m-%d %H:%M}"
          if slow else f"- runs by band: {counts}")
fix = summary["round2-fix"]
rw8_fast = [x for x in fix if x[5] == "fast" and "rw8" in x[2]]
rw8_slow = [x for x in summary["round2-followup"] if x[5] == "slow" and "rw8" in x[2]]
print()
print(f"- csharp-wax-rw8, fix pass's fast runs ({len(rw8_fast)}): median {median(x[4] for x in rw8_fast):.2f} syncs/s, "
      f"{median(x[3] for x in rw8_fast):.2f} commands/s; follow-up's slow runs ({len(rw8_slow)}): median "
      f"{median(x[4] for x in rw8_slow):.2f} syncs/s, {median(x[3] for x in rw8_slow):.2f} commands/s; ratio of commands "
      f"per second {median(x[3] for x in rw8_fast) / median(x[3] for x in rw8_slow):.3f}")
for start, end, label, rate, syncs, mark, folder, record in fix:
    if label.startswith("csharp-wax-batch-payments-w8"):
        rows = s.intervals(folder)
        rates = [ev / (b - a).total_seconds() for a, b, p, ev in rows]
        slow_share = 100 * sum(x < s.SLOW_EVENTS_PER_SECOND for x in rates) / len(rates) if rates else None
        print(f"- fix pass {label}: {syncs:.2f} syncs/s; readers' intervals under {s.SLOW_EVENTS_PER_SECOND:,} events/s: "
              f"{slow_share if slow_share is None else round(slow_share, 1)} percent")
firsts = [(name, rows[0]) for name, rows in summary.items() if rows]
for name, x in firsts:
    print(f"- {name}'s first run at 8 workers on the payment file: {x[2]}, {x[4]:.2f} syncs/s")
