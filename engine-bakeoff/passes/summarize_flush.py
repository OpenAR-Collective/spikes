"""Summarizes round 2's flush-timing night (plan round2-flush) from the recorded
results and prints Markdown, with each reading the plan set in advance.

A run at 8 workers is marked by PostgreSQL's own mean time per WAL sync over
the harness's window, measured with track_wal_io_timing: slow at 4.3 ms or
more, high at 2.8 ms or less, and anything between not marked. Its WAL syncs
per second are printed beside every rate, with the bands the earlier nights
used as a cross-check: the high level at 340 or more, fast at 280 to 320, and
slow at 200 or less. The diagnostic with synchronous_commit off, a run at
another worker count, and a run whose database did not report
track_wal_io_timing on are never marked. The busy share is the share of the
span in which a sync was in progress: the measured sync time over the span's
length, since one process syncs at a time.

    py -3 summarize_flush.py [results folder]

The results folder defaults to the one beside this folder; the host's
counters, the storage checks, the status, and the process snapshot are read
from the round2-flush files beside this script.
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
NAME = "round2-flush"
PLAN = json.loads((HERE / f"{NAME}.plan.json").read_text(encoding="utf-8"))
HOST = HERE / f"{NAME}.host-counters.csv"
STATUS = HERE / f"{NAME}.status.json"
PROCESSES = HERE / f"{NAME}.processes.csv"
DIAGNOSTIC = "csharp-wax-rw8-sync-off-diagnostic"
SLOW_MS, HIGH_MS = 4.3, 2.8
# A move to the slow level is set against the night's median of unrelated
# processes: one core, 20 MB written, or 500 write operations a second more.
MORE_CORES, MORE_MB_S, MORE_OPS_S = 1.0, 20.0, 500.0
SNAPSHOT_S = 30


def level(sync_ms, marked=True):
    """The level a run's or an interval's measured time per sync marks."""
    if sync_ms is None or not marked:
        return "not marked"
    if sync_ms >= SLOW_MS:
        return "slow"
    if sync_ms <= HIGH_MS:
        return "high"
    return "not marked"


def band(syncs_per_second, marked=True):
    """The sync-rate band the earlier nights used, as a cross-check."""
    if syncs_per_second is None or not marked:
        return "not marked"
    if syncs_per_second >= 340:
        return "high"
    if 280 <= syncs_per_second <= 320:
        return "fast"
    if syncs_per_second <= 200:
        return "slow"
    return "not marked"


def utc(text):
    """An ISO time with Z and up to 7 fractional digits, as UTC."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", text)
    stamp = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return stamp + timedelta(microseconds=int((match.group(2) or "0")[:6].ljust(6, "0")))


def clock_utc(text):
    """The database's clock_timestamp() text, as UTC."""
    match = re.match(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)(?:\.(\d+))?([+-]\d\d)", text)
    stamp = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S")
    stamp += timedelta(microseconds=int((match.group(3) or "0")[:6].ljust(6, "0")))
    return (stamp - timedelta(hours=int(match.group(4)))).replace(tzinfo=timezone.utc)


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def sampler(folder):
    """The side sampler's lines: (time, waits by key) and (time, counters)."""
    waits, stats = [], []
    for path in sorted((folder / "raw").glob("*.sampler.txt")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0] != "NOTICE:":
                continue
            try:
                stamp = utc(parts[2])
                if parts[1] == "waits":
                    waits.append((stamp, {k: int(n) for k, n in (item.rsplit("=", 1) for item in parts[3:])}))
                elif parts[1] == "stats":
                    stats.append((stamp, {k: int(v) for k, v in (item.split("=", 1) for item in parts[3:])}))
            except (AttributeError, ValueError):
                continue  # a line cut short when the loop was stopped
    return waits, stats


# The host's counters, by the end of their typeperf column heading; the disk's
# are matched by pattern, since the plan names the disk.
HOST_COLUMNS = {
    r"Processor Information(_Total)\% Processor Performance": "perf",
    r"Process(vmmemWSL)\% Processor Time": "vm",
}
DISK_COLUMNS = {"Avg. Disk sec/Write": "write_s", "Avg. Disk sec/Transfer": "transfer_s", "Disk Writes/sec": "writes",
                "Disk Write Bytes/sec": "write_bytes", "Avg. Disk Write Queue Length": "queue", "% Idle Time": "idle"}


def host_counters():
    """The host's samples as (UTC time, values by short name): physical disk
    columns start pd_, logical disk columns ld_, and thermal zones tz<n>_."""
    if not HOST.exists():
        return []
    with HOST.open(encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return []
    bias = re.search(r"\((-?\d+)\)\s*$", rows[0][0])
    offset = timedelta(minutes=int(bias.group(1))) if bias else timedelta(0)
    names, zones = [], {}
    for heading in rows[0]:
        name = next((short for end, short in HOST_COLUMNS.items() if heading.endswith(end)), None)
        disk = re.search(r"(Physical|Logical)Disk\([^)]*\)\\(.+)$", heading)
        if disk and disk.group(2) in DISK_COLUMNS:
            name = f"{'pd' if disk.group(1) == 'Physical' else 'ld'}_{DISK_COLUMNS[disk.group(2)]}"
        zone = re.search(r"Thermal Zone Information\(([^)]*)\)\\(Temperature|% Passive Limit|Throttle Reasons)$", heading)
        if zone:
            number = zones.setdefault(zone.group(1), len(zones))
            name = f"tz{number}_" + {"Temperature": "kelvin", "% Passive Limit": "passive", "Throttle Reasons": "reasons"}[zone.group(2)]
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


def processes():
    """The committed process snapshot, by snapshot: the CPU in cores, MB
    written per second, and write operations per second of every process not
    named, taken as the snapshot's total (_all) less every named process, so a
    process too small to get a row of its own still counts."""
    if not PROCESSES.exists():
        return {}
    stamps = {}
    with PROCESSES.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                stamp, interval = utc(row["utc"]), float(row["interval_s"])
                cpu, written, ops = float(row["cpu_s"]), float(row["write_bytes"]), float(row["write_ops"])
            except (KeyError, ValueError, AttributeError, TypeError):
                continue
            if row["name"] in ("_snapshot", "_error") or interval <= 0:
                continue
            totals = stamps.setdefault(stamp, {"interval": interval, "all": [0.0, 0.0, 0.0], "named": [0.0, 0.0, 0.0]})
            key = "all" if row["name"] == "_all" else "named" if row["name"] != "unrelated-process" else None
            if key:
                totals[key] = [a + b for a, b in zip(totals[key], (cpu, written, ops))]
    return {stamp: {"cores": (t["all"][0] - t["named"][0]) / t["interval"],
                    "mb_s": (t["all"][1] - t["named"][1]) / t["interval"] / 2**20,
                    "ops_s": (t["all"][2] - t["named"][2]) / t["interval"]}
            for stamp, t in stamps.items() if t["all"][0] > 0}


def host_window(host, start, end):
    """The host's figures over a window: the disk's write latency in ms as a
    mean weighted by writes, its writes and MB written per second, its write
    queue and idle percent, the hottest thermal zone in Celsius, the
    processor's speed, and the VM's CPU in cores."""
    chosen = [value for stamp, value in host if start <= stamp < end]
    weighted = [(v["pd_write_s"], v["pd_writes"]) for v in chosen if "pd_write_s" in v and "pd_writes" in v]
    writes = sum(w for _, w in weighted)
    zones = sorted({key[:-7] for v in chosen for key in v if key.startswith("tz") and key.endswith("_kelvin")})
    vm = mean(v.get("vm") for v in chosen)
    return {
        "disk_write_ms": 1000 * sum(s * w for s, w in weighted) / writes if writes else None,
        "disk_writes_s": mean(v.get("pd_writes") for v in chosen),
        "disk_mb_s": mean(v["pd_write_bytes"] / 2**20 if "pd_write_bytes" in v else None for v in chosen),
        "disk_queue": mean(v.get("pd_queue") for v in chosen),
        "disk_idle": mean(v.get("pd_idle") for v in chosen),
        "hottest_c": max((v[f"{zone}_kelvin"] - 273.15 for v in chosen for zone in zones if f"{zone}_kelvin" in v),
                         default=None),
        "perf": median(v.get("perf") for v in chosen),
        "vm_cores": vm / 100 if vm is not None else None,
    }


def process_window(procs, start, end):
    """The unrelated processes over the snapshots whose interval overlaps a
    window: mean and most in one snapshot, for cores, MB/s, and write ops/s."""
    chosen = [v for stamp, v in procs.items() if start < stamp and stamp - timedelta(seconds=SNAPSHOT_S) < end]
    out = {}
    for key in ("cores", "mb_s", "ops_s"):
        out[f"unrelated_{key}"] = mean(v[key] for v in chosen)
        out[f"unrelated_{key}_max"] = max((v[key] for v in chosen), default=None)
    return out


def intervals(stats, waits, start, end, timed):
    """The sampler's 10-second intervals that fall wholly within the bench,
    without the first and the last, which ramp up and down: their sync rate,
    PostgreSQL's time per sync and per write in ms and the busy share (when the
    run was timed), and the time per sync the WalSync share implies. The
    catch-up after the bench is left out, since its rate is the engine's alone."""
    rows = []
    pairs = [(a, b) for a, b in zip(stats, stats[1:]) if start <= a[0] and b[0] <= end][1:-1]
    for (a_at, a), (b_at, b) in pairs:
        seconds = (b_at - a_at).total_seconds()
        syncs, writes = b["wal_sync"] - a["wal_sync"], b["wal_write"] - a["wal_write"]
        if seconds <= 0 or syncs <= 0:
            continue
        has = timed and "wal_sync_us" in a and "wal_sync_us" in b
        sync_us = b["wal_sync_us"] - a["wal_sync_us"] if has else None
        looks = [c for t, c in waits if a_at < t <= b_at]
        share = mean(sum(n for k, n in c.items() if k.endswith("/IO/WalSync")) for c in looks)
        rate = syncs / seconds
        sync_ms = sync_us / 1000 / syncs if has else None
        rows.append({"start": a_at, "end": b_at, "seconds": seconds, "sync_us": sync_us, "syncs_s": rate,
                     "sync_ms": sync_ms, "level": level(sync_ms), "busy": sync_us / 1e6 / seconds if has else None,
                     "write_ms": (b["wal_write_us"] - a["wal_write_us"]) / 1000 / writes if has and writes else None,
                     "implied_ms": 1000 * share / rate if share is not None else None,
                     "commits_s": (b["commits"] - a["commits"]) / seconds})
    return rows


def load(entry, host, procs, checks):
    found = sorted(RESULTS.glob(f"*-{entry['label']}/results.json"))
    if not found:
        return None
    folder = found[0].parent
    run = json.loads(found[0].read_text(encoding="utf-8"))["runs"][0]
    bench, wal = run["bench"], (run.get("db_activity") or {}).get("wal") or {}
    window = run["window_s"]
    start, end = clock_utc(run["clock"]["start"]), clock_utc(run["clock"]["end"])
    timing = (run.get("server_settings") or {}).get("track_wal_io_timing") or "not recorded"
    timed = timing == "on"
    marked = timed and entry["workers"] == 8 and entry["side"] != DIAGNOSTIC
    syncs = wal.get("wal_sync")
    sync_ms = wal["wal_sync_time"] / syncs if timed and syncs and wal.get("wal_sync_time") is not None else None
    waits, stats = sampler(folder)
    # Over the side sampler's life, from its first stats line to its last,
    # the measured time per sync and the time the WalSync share implies.
    life = len(stats) > 1
    life_syncs = stats[-1][1]["wal_sync"] - stats[0][1]["wal_sync"] if life else 0
    life_s = (stats[-1][0] - stats[0][0]).total_seconds() if life else 0
    looks = [c for t, c in waits if life and stats[0][0] < t <= stats[-1][0]]
    share = mean(sum(n for k, n in c.items() if k.endswith("/IO/WalSync")) for c in looks)
    life_ms = ((stats[-1][1]["wal_sync_us"] - stats[0][1]["wal_sync_us"]) / 1000 / life_syncs
               if timed and life and life_syncs and "wal_sync_us" in stats[0][1] and "wal_sync_us" in stats[-1][1] else None)
    implied = 1000 * share / (life_syncs / life_s) if share is not None and life_syncs and life_s else None
    sessions = {connection.get("synchronous_commit") for connection in bench.get("plan_choices") or []}
    r = {
        "folder": folder, "start": start, "end": end, "window_s": window, "timing": timing,
        "rate": bench["succeeded"] / bench["elapsed_s"] if bench.get("elapsed_s") else None,
        "syncs_s": syncs / window if syncs is not None and window else None,
        "sync_ms": sync_ms, "level": level(sync_ms, marked),
        "busy": wal["wal_sync_time"] / 1000 / window if timed and window and wal.get("wal_sync_time") is not None else None,
        "write_ms": wal["wal_write_time"] / wal["wal_write"] if timed and wal.get("wal_write") else None,
        "life_ms": life_ms, "implied_ms": implied,
        "agreement": implied / life_ms if implied is not None and life_ms else None,
        "events_per_sync": run["events_written"] / life_syncs if run.get("events_written") and life_syncs else None,
        "db_cpu": (run.get("cpu_ms_per_command") or {}).get("db"),
        "catchup_s": run.get("catchup_s"), "max_lag": run.get("max_lag_events"),
        "sessions": ", ".join(sorted(s for s in sessions if s)) or "not read",
        "gates": run.get("gates_pass"),
        "failed": [name for name, passed in (run.get("gates") or {}).items() if passed is False],
        "check": checks.get(entry["label"]),
        "intervals": intervals(stats, waits, start, end, timed) if marked else [],
        **host_window(host, start, end), **process_window(procs, start, end),
    }
    r["band"] = band(r["syncs_s"], entry["workers"] == 8 and entry["side"] != DIAGNOSTIC)
    # The busy share over the run's steady intervals, without the ramps at
    # either end of the bench, which the readings use.
    steady = [row for row in r["intervals"] if row["sync_us"] is not None]
    seconds = sum(row["seconds"] for row in steady)
    r["steady_busy"] = sum(row["sync_us"] for row in steady) / 1e6 / seconds if seconds else None
    return r


def f(value, digits=1):
    return "" if value is None else f"{value:,.{digits}f}"


def local(stamp):
    return "" if stamp is None else f"{stamp.astimezone():%H:%M}"


def short(entry):
    return entry["label"].replace(f"{NAME}-", "").replace("-batch-payments", "")


def ms(r):
    """A run's measured time per sync for a reading, or why there is none."""
    return "no run" if r is None else "untimed" if r["sync_ms"] is None else f"{r['sync_ms']:.2f} ms"


def fdatasync(check):
    return ((check or {}).get("one_8k") or {}).get("fdatasync")


def main():
    host, procs = host_counters(), processes()
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {"runs": []}
    checks = {record["label"]: record.get("fsync_check") for record in status["runs"]}
    loaded = []
    for entry in PLAN["runs"]:
        if entry.get("kind") == "idle":
            loaded.append((entry, None))
            continue
        r = load(entry, host, procs, checks)
        if r:
            loaded.append((entry, r))
    runs = [(entry, r) for entry, r in loaded if r]
    marked = [(entry, r) for entry, r in runs if r["level"] != "not marked"]
    out, readings = [], []

    out += ["### Every run in run order", "",
            "*Each run's start (local time); commands per second over the bench; WAL syncs per second over the harness's "
            "window and the earlier nights' band they fall in; over the same window, PostgreSQL's mean time per WAL sync "
            "in milliseconds (measured), the level it marks, the busy share (the share of the window in which a sync was "
            "in progress, measured) and the same over the bench's 10-second intervals without the first and last "
            "(the steady busy share, which the readings use), and the mean time per WAL write in milliseconds (measured); "
            "over the side sampler's "
            "life, the measured time per sync, the time per sync its share of looks in a WAL sync implies (inferred, as "
            "the follow-up pass inferred it), and the implied over the measured; events written per WAL sync; database "
            "CPU in milliseconds per command; the bench sessions' synchronous_commit; the database's track_wal_io_timing; "
            "and the gates.*", "",
            "| Order | Group | Run | Start | Commands/s | WAL syncs/s | Rate band | Sync ms | Level | Busy share "
            "| Steady busy share | Write ms | Sampler span sync ms | Implied sync ms | Implied over measured | Events per sync "
            "| DB CPU | Bench synchronous_commit | WAL timing | Gates |", "| --- " * 20 + "|"]
    for entry, r in runs:
        gates = "pass" if r["gates"] else "FAIL: " + ", ".join(str(name) for name in r["failed"])
        out.append(f"| {entry['order']} | {entry['group']} | {short(entry)} | {local(r['start'])} | {f(r['rate'], 0)} "
                   f"| {f(r['syncs_s'], 0)} | {r['band']} | {f(r['sync_ms'], 2)} | {r['level']} | {f(r['busy'], 2)} "
                   f"| {f(r['steady_busy'], 2)} | {f(r['write_ms'], 3)} | {f(r['life_ms'], 2)} | {f(r['implied_ms'], 2)} | {f(r['agreement'], 2)} "
                   f"| {f(r['events_per_sync'], 2)} | {f(r['db_cpu'], 2)} | {r['sessions']} | {r['timing']} | {gates} |")

    out += ["", "### The storage and the host beside each run", "",
            "*In run order: WAL syncs per second and the level the run's time per sync marks; pg_test_fsync's fdatasync "
            "rate for one 8 kB write in the idle database just before the run, in operations per second; over the run's "
            "bench, the named disk's mean write latency in milliseconds (weighted by writes, in Windows' count, which, "
            "as far as its documentation says, leaves out flush requests), its writes per second and MB written per second, its mean write queue and idle "
            "percent; the hottest thermal zone in Celsius; the processor's performance against nominal (median percent); "
            "the CPU of the virtual machine Docker runs in, in cores; and, from the 30-second process snapshots that "
            "overlap the bench, every process not named taken together: its cores, MB written per second, and write "
            "operations per second, the mean and the most in one snapshot.*", "",
            "| Order | Run | WAL syncs/s | Level | fdatasync ops/s before | Disk write ms | Disk writes/s | Disk MB/s "
            "| Disk queue | Disk idle % | Hottest zone C | Processor performance % | VM cores | Unrelated cores "
            "| Unrelated cores max | Unrelated MB/s | Unrelated MB/s max | Unrelated write ops/s | Unrelated write ops/s max |",
            "| --- " * 19 + "|"]
    for entry, r in runs:
        out.append(f"| {entry['order']} | {short(entry)} | {f(r['syncs_s'], 0)} | {r['level']} | {f(fdatasync(r['check']), 0)} "
                   f"| {f(r['disk_write_ms'], 3)} | {f(r['disk_writes_s'], 0)} | {f(r['disk_mb_s'], 1)} "
                   f"| {f(r['disk_queue'], 2)} | {f(r['disk_idle'], 0)} | {f(r['hottest_c'], 1)} | {f(r['perf'], 0)} "
                   f"| {f(r['vm_cores'], 2)} | {f(r['unrelated_cores'], 2)} | {f(r['unrelated_cores_max'], 2)} "
                   f"| {f(r['unrelated_mb_s'], 1)} | {f(r['unrelated_mb_s_max'], 1)} | {f(r['unrelated_ops_s'], 0)} "
                   f"| {f(r['unrelated_ops_s_max'], 0)} |")

    # The low sync rate: is it long syncs? This one reading picks its runs by
    # the earlier nights' low band, 200 syncs a second or less, which is where
    # the follow-up tests' slow runs fell, so a low-rate run with short syncs,
    # the case that would refute their inference, is not left out by the
    # marking.
    low = [(entry, r) for entry, r in runs if r["band"] == "slow" and r["sync_ms"] is not None]
    high = [r for _, r in marked if r["level"] == "high"]
    time, busy = median(r["sync_ms"] for _, r in low), median(r["steady_busy"] for _, r in low)
    if not low:
        readings.append("- Sync time: no timed run fell in the low band of 200 syncs a second or less, so the reading is not made.")
    else:
        verdict = ("the low sync rate comes from long syncs, measured" if time >= SLOW_MS and busy is not None and busy >= 0.9 else
                   "the low rate is less demand, not a slower flush, and the follow-up tests' inference fails"
                   if busy is not None and busy < 0.7 else "the night does not decide")
        counter = [short(entry) for entry, r in low if r["sync_ms"] <= HIGH_MS]
        readings.append(f"- Sync time: {len(low)} runs in the low band, median time per sync {f(time, 2)} ms and steady busy share "
                        f"{f(busy, 2)}: {verdict}."
                        + (f" Low-rate runs with a time per sync of {HIGH_MS} ms or less: {', '.join(counter)}." if counter else "")
                        + (f" {len(high)} runs marked high: median {f(median(r['sync_ms'] for r in high), 2)} ms, steady busy "
                           f"share {f(median(r['steady_busy'] for r in high), 2)}." if high else " No run was marked high."))

    # The follow-up's method.
    compared = [r for entry, r in runs if r["agreement"] is not None and entry["workers"] == 8 and entry["side"] != DIAGNOSTIC]
    agree = [r for r in compared if 0.8 <= r["agreement"] <= 1.2]
    readings.append("- The follow-up's method: no run had both figures, so the reading is not made." if not compared else
                    f"- The follow-up's method: implied within 20 percent of measured in {len(agree)} of {len(compared)} runs "
                    "at 8 workers, the diagnostic aside: "
                    + ("its inferred figures stand as estimates." if len(agree) >= 0.8 * len(compared)
                       else "its write-up withdraws them."))

    # The storage with no load.
    before = {name: [fdatasync(r["check"]) for _, r in marked if r["level"] == name and fdatasync(r["check"])]
              for name in ("slow", "high")}
    if min(len(before["slow"]), len(before["high"])) < 2:
        readings.append(f"- Idle fdatasync: {len(before['slow'])} checks before slow runs and {len(before['high'])} before "
                        "high runs, fewer than two on a side, so the night does not decide.")
    else:
        a, b = median(before["slow"]), median(before["high"])
        verdict = ("the storage path is slower even with no load" if a <= 0.75 * b else
                   "the storage path idles at the same speed, so the slowdown comes with load" if abs(a / b - 1) <= 0.10
                   else "the night does not decide")
        readings.append(f"- Idle fdatasync: median {f(a, 0)} ops/s before {len(before['slow'])} slow runs against {f(b, 0)} "
                        f"before {len(before['high'])} high runs: {verdict}.")

    gaps = [(n, entry) for n, (entry, r) in enumerate(loaded) if entry.get("kind") == "idle"]
    gap_records = {record["label"]: record for record in status["runs"] if record.get("kind") == "idle"}
    eligible = faster = 0
    if gaps:
        out += ["", "### The idle gaps", "",
                "*Each gap: the run just before it and the run just after it, each with its commands per second, WAL "
                "syncs per second, measured time per sync in milliseconds, and level; pg_test_fsync's fdatasync rate for "
                "one 8 kB write at the start of the gap and again after it, just before the next run, in operations per "
                "second; and the reading. A gap counts only after a run marked slow, and it brings a faster level back "
                "if the run after it takes at least 25 percent less time per sync.*", "",
                "| Gap | Minutes | Run before | Its commands/s | Its WAL syncs/s | Its sync ms | Its level "
                "| fdatasync at gap start | fdatasync after gap | Run after | Its commands/s | Its WAL syncs/s | Its sync ms "
                "| Its level | Reading |", "| --- " * 15 + "|"]
        for n, entry in gaps:
            record = gap_records.get(entry["label"]) or {}
            if not record or record.get("state", "").startswith("skipped"):
                out.append(f"| {entry['suffix']} | {entry['minutes']} | {record.get('state', 'not run')} |" + " |" * 12)
                continue
            before_entry, b = next(((e, r) for e, r in reversed(loaded[:n]) if r and e["side"] != DIAGNOSTIC), (None, None))
            after_entry, a = next(((e, r) for e, r in loaded[n + 1:] if r), (None, None))
            if not (a and b) or b["level"] != "slow":
                reading = "not counted: the run before is not marked slow"
            elif a["sync_ms"] is None:
                reading = "not counted: the run after was not timed"
            else:
                eligible += 1
                came_back = a["sync_ms"] <= 0.75 * b["sync_ms"]
                faster += came_back
                reading = "faster level back" if came_back else "no faster level"
            out.append(f"| {entry['suffix']} | {entry['minutes']} | {short(before_entry) if b else ''} | {f(b and b['rate'], 0)} "
                       f"| {f(b and b['syncs_s'], 0)} | {f(b and b['sync_ms'], 2)} | {b['level'] if b else ''} "
                       f"| {f(fdatasync(record.get('fsync_check')), 0)} | {f(fdatasync(a and a['check']), 0)} "
                       f"| {short(after_entry) if a else ''} | {f(a and a['rate'], 0)} | {f(a and a['syncs_s'], 0)} "
                       f"| {f(a and a['sync_ms'], 2)} | {a['level'] if a else ''} | {reading} |")
    readings.append(f"- Idle gaps: counted {eligible}, with a faster level back after {faster}: "
                    + ("fewer than three counted, so the night does not decide." if eligible < 3 else
                       "a return after 25 idle minutes is shown." if faster >= eligible - 1 else
                       "a return is not shown." if faster == 0 else "the night does not decide."))

    # The high level: the first two runs.
    first = {entry["order"]: r for entry, r in runs if entry["group"] == 1}
    one, two = first.get(1), first.get(2)
    if one and one["level"] == "high":
        readings.append(f"- The high level: the first run, csharp-wax-rw8, took {ms(one)} per sync, marked high: "
                        "the high level comes with being first after an idle stretch, not with custom plans.")
    elif two and two["level"] == "high":
        readings.append(f"- The high level: the first run took {ms(one)} per sync, not marked high, and "
                        f"csharp-wax-custom-plans second took {ms(two)}, marked high: custom plans stay a candidate.")
    else:
        readings.append(f"- The high level: neither of the first two runs was marked high (time per sync: first {ms(one)}, "
                        f"second {ms(two)}), so the night does not decide.")

    trio = [(entry, r) for entry, r in runs if entry["group"] == 3]
    diagnostic = next((r for entry, r in trio if entry["side"] == DIAGNOSTIC), None)
    if diagnostic:
        place = next(entry["order"] for entry, r in trio if entry["side"] == DIAGNOSTIC)
        out += ["", "### The diagnostic: synchronous_commit off for the bench's sessions", "",
                "*Group 3 in run order: commands per second, WAL syncs per second, the measured time per sync in "
                "milliseconds and the level it marks (the diagnostic is never marked, since its bench's commits do not "
                "wait for a sync), the busy share, the engine's catch-up after the bench in seconds and its largest lag in "
                "events, and the bench sessions' synchronous_commit as each connection reported it; then the diagnostic's "
                "commands per second against each run on either side of it, as a percent change. The engine still commits "
                "synchronously and its syncs now carry the bench's WAL too, so a faster bench can leave the engine further "
                "behind; the change is not sustained throughput. It is a diagnostic only, never a configuration to "
                "compare.*", "",
                "| Order | Run | Commands/s | WAL syncs/s | Sync ms | Level | Busy share | Catch-up s | Largest lag "
                "| Bench synchronous_commit |", "| --- " * 10 + "|"]
        for entry, r in trio:
            out.append(f"| {entry['order']} | {short(entry)} | {f(r['rate'], 0)} | {f(r['syncs_s'], 0)} | {f(r['sync_ms'], 2)} "
                       f"| {r['level']} | {f(r['busy'], 2)} | {f(r['catchup_s'], 1)} | {f(r['max_lag'], 0)} | {r['sessions']} |")
        out += [""] + [f"- Against the run {'before' if entry['order'] < place else 'after'} it ({f(o['sync_ms'], 2)} ms per "
                       f"sync, {o['level']}, {f(o['syncs_s'], 0)} WAL syncs per second): "
                       f"{f(100 * (diagnostic['rate'] / o['rate'] - 1), 0)} percent."
                       for entry, o in trio if entry["side"] != DIAGNOSTIC and o["rate"] and diagnostic["rate"]]

    rows = [row | {"run": entry["order"]} for entry, r in runs for row in r["intervals"]]
    if rows:
        out += ["", "### The 10-second intervals by level", "",
                "*The side sampler's 10-second intervals of every marked run, within each bench and without its first and "
                "last intervals, each marked by its own measured time per sync with the runs' cuts: how many intervals, "
                "and the median of each figure over them. An interval's counts carry each backend's lag of up to about a "
                "second in reporting to pg_stat, so its level is noisier than a run's. The host's disk and thermal "
                "figures are typeperf's samples within the interval.*", "",
                "| Level | Intervals | WAL syncs/s | Sync ms | Busy share | Write ms | Implied sync ms | Commits/s "
                "| Disk write ms | Disk writes/s | Hottest zone C |", "| --- " * 11 + "|"]
        for row in rows:
            row.update({key: value for key, value in host_window(host, row["start"], row["end"]).items()
                        if key in ("disk_write_ms", "disk_writes_s", "hottest_c")})
        for name in ("high", "not marked", "slow"):
            chosen = [row for row in rows if row["level"] == name]
            if chosen:
                out.append(f"| {name} | {len(chosen)} | {f(median(r['syncs_s'] for r in chosen), 0)} "
                           f"| {f(median(r['sync_ms'] for r in chosen), 2)} | {f(median(r['busy'] for r in chosen), 2)} "
                           f"| {f(median(r['write_ms'] for r in chosen), 3)} | {f(median(r['implied_ms'] for r in chosen), 2)} "
                           f"| {f(median(r['commits_s'] for r in chosen), 0)} | {f(median(r['disk_write_ms'] for r in chosen), 3)} "
                           f"| {f(median(r['disk_writes_s'] for r in chosen), 0)} | {f(median(r['hottest_c'] for r in chosen), 1)} |")

    # Moves to the slow level: a run marked slow whose predecessor among the
    # timed runs at 8 workers, the diagnostic aside, took at least 25 percent
    # less time per sync, marked or not, over the whole span from the end of
    # one bench to the start of the next; and within a run, an interval
    # marked slow after one that took at least 25 percent less.
    timed = [(entry, r) for entry, r in runs if r["sync_ms"] is not None and entry["workers"] == 8
             and entry["side"] != DIAGNOSTIC]
    moves = []
    for (ea, a), (eb, b) in zip(timed, timed[1:]):
        if b["level"] == "slow" and a["sync_ms"] <= 0.75 * b["sync_ms"]:
            moves.append((f"between {short(ea)} and {short(eb)}", a["end"], b["start"]))
    for entry, r in timed:
        for x, y in zip(r["intervals"], r["intervals"][1:]):
            if y["level"] == "slow" and x["sync_ms"] is not None and x["sync_ms"] <= 0.75 * y["sync_ms"]:
                moves.append((f"within {short(entry)} at {local(y['start'])}", x["start"], y["end"]))
    night = {key: median(v[key] for v in procs.values()) for key in ("cores", "mb_s", "ops_s")}
    if moves:
        out += ["", "### Moves to the slow level", "",
                "*Each move to the slow level, a run or interval marked slow after one that took at least 25 percent less "
                "time per sync: the most that every process "
                "not named took together in any 30-second snapshot overlapping the move, in cores, MB written per second, "
                "and write operations per second, above the night's median of each (here "
                f"{f(night['cores'], 2)} cores, {f(night['mb_s'], 1)} MB/s, and {f(night['ops_s'], 0)} write ops/s), and "
                "whether any of the three reached the plan's margin. As far as Windows' documentation says, it does not "
                "count the flush calls a sync makes among write operations.*", "",
                "| Move | Cores above median | MB/s above median | Write ops/s above median | At the margin |",
                "| --- " * 5 + "|"]
    flagged = 0
    for name, start, end in moves:
        window = process_window(procs, start, end)
        above = {key: (window[f"unrelated_{key}_max"] - night[key]) if window[f"unrelated_{key}_max"] is not None
                 and night[key] is not None else None for key in ("cores", "mb_s", "ops_s")}
        hit = any(value is not None and value >= limit
                  for value, limit in zip(above.values(), (MORE_CORES, MORE_MB_S, MORE_OPS_S)))
        flagged += hit
        out.append(f"| {name} | {f(above['cores'], 2)} | {f(above['mb_s'], 1)} | {f(above['ops_s'], 0)} | {'yes' if hit else 'no'} |")
    readings.append("- Competing load: no move to the slow level was seen, so the reading is not made."
                    if not moves else
                    f"- Competing load: {flagged} of {len(moves)} moves to the slow level came with unrelated load at the margin: "
                    + ("competing load is the likelier cause." if flagged == len(moves) else
                       "competing load is not shown." if flagged == 0 else "the night does not decide."))

    out += ["", "### The readings set in the plan", ""] + readings
    print("\n".join(out))


if __name__ == "__main__":
    main()
