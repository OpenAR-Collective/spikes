# Harness

One harness drives both sides of the [engine bake-off](../README.md), only through the commands [WORKLOADS.md](../WORKLOADS.md) specifies, and measures them from outside the process under test, so neither side reports its own numbers. It is standard-library Python and runs on the host. It is Foundation-written, adapted from a private harness built for an early look at Andy Troiano's prototype.

## What it measures

- **Throughput and command latency**, from the bench command's own JSON line.
- **CPU per command** for the database, the engine, and the bench driver, from each container's cgroup counter (`cpu.stat`, `usage_usec`), read before and after the whole bench command and divided by `sent_total`, every command the bench sent, the warm-up's included. A run whose bench JSON lacks `sent_total` fails rather than falling back to `sent`. The engine's CPU while it catches up afterward is recorded separately.
- **Statements and commits per command**, from `pg_stat_statements` and `pg_stat_database`, for the run's database only, divided by `sent_total` in the same way.
- **Read-model and rule lag**, from `core.bakeoff_progress`, sampled every second, and catch-up: the time from the end of the bench command until every reader has reached the head as it stood at that end. Events written afterward, such as scheduled reviews still firing, do not count. The harness then waits, unmeasured, until nothing is left and every register entry due five seconds before the end has fired, so the gates see a finished state.
- **Rule latency**, from `core.bakeoff_event_log`: each action event's `recorded_at` minus its triggering event's.
- **Register lateness**, from `core.pending_event`: `fired_at` minus `due_at`.
- **The correctness gates** in WORKLOADS.md, in SQL over the tables both sides share, plus the side's own `verify-chain`. The batch payment gate, which the bulk payment file shares, recomputes every payment key with WORKLOADS.md's `derived_uuid`, written in SQL, so it also checks that the side derives keys as specified. The placement gate does the same for every placed account's key, from the `run_pk` in the bench JSON.
- **Transactions and fallback chunks**, from the bench JSON, beside commits per command from `pg_stat_database`. In the bulk workloads, latency is per transaction, that is, per chunk.
- **The top statements**, from `pg_stat_statements`: the 25 statements (`--top-statements N`) with the most total time in the run's database, each with its calls, total and mean milliseconds, rows, and the first 300 characters of its text. They are taken at the end of the bench command, with the statement count, and kept in each run's results and in `raw/`.
- **CPU throttling**, from the same `cpu.stat`: for each container, how many of its scheduler periods hit its CPU limit (`nr_throttled` of `nr_periods`) and how long it was held back (`throttled_usec`), over the bench command. A container throttled for much of the run was capped by its limit, so its throughput measures the limit, not the code.
- **A host probe**: just before the bench, outside the measured window, a fixed CPU-bound statement (the sum of 20 million integers, streamed, with JIT off) is timed five times in the run's database, and the median is recorded in milliseconds. The machine's speed can drift over a night, from its clock, its temperature, or the cores Docker lands on, so a probe well above the night's usual marks a run to doubt or repeat.
- **Steal time**, from the aggregate `cpu` line of `/proc/stat` as the database container sees it, before and after the bench command: the share of the kernel's CPU time the hypervisor gave to other tenants, and the share spent busy. On a Linux host this is the host's kernel, where a cloud vCPU's lost time shows; on Docker Desktop it is the utility VM's, and on the laptop under WSL2 it has read 0.0 in every run, so there it says only that no steal was reported.
- **Write activity in the database**, before and after the bench command: `pg_stat_wal` (records, full-page images, bytes, syncs), `pg_stat_checkpointer`, `pg_stat_bgwriter`, and, for every table that changed, its inserts, updates, HOT updates, deletes, autovacuums, and dead tuples at the end. `run.md` shows WAL kilobytes and full-page images per command. Each view is read on its own, so one a PostgreSQL version lacks is recorded as null.

## What keeps the two sides equal

- [compose.override.yml](compose.override.yml) is applied on top of either side's own Compose file. It sets the PostgreSQL settings, including `pg_stat_statements`, and the CPU and memory limits of the `db`, `engine`, and `bench` services. Neither side sets these in its own file. A change to it is a change to the method. It is the `standard` caps profile, the default. With `--caps scaled`, on `selftest`, `prepare`, `run`, and `teardown`, [compose.scale.yml](compose.scale.yml) is applied in its place, not layered on it: a complete file with larger limits and memory settings for a bigger host. Each result records the profile's name and the sha256 of its file, in `environment.json`, `results.json`, and the header of `run.md`. Switching profiles recreates the database container with the new limits; its data volume, and so every seed template, survives.
- [compose.pgbouncer.yml](compose.pgbouncer.yml) puts pgBouncer in transaction pooling mode in front of the database, with `--pgbouncer`. It is tuning that either side may use, and the result records it. `--pool-size N` sets its `DEFAULT_POOL_SIZE`, 60 when not given, and the pool size in effect is recorded with every result that used pgBouncer. Its container joins the CPU and throttle accounting: it runs single-threaded with 1 CPU, so `run.md` shows its CPU per command and how often it hit that limit, which would mean it was the ceiling.
- [sides.json](sides.json) is the only place the harness knows anything about a side: its Compose file, project name, profile, command-line prefix, and any environment that selects a mode. There are four sides: `python`; `csharp`, which appends events through Marten; `csharp-direct`, the same C# image appending with plain SQL into Marten's own tables (`WAX_APPEND=direct`), which separates the language from the library on the write path; and `csharp-wax`, C# with no Marten at all, writing WAX-0001's `core.event` and reading it with a port of the Python side's reader. The `csharp` and `csharp-direct` sides share one Compose project and one seed template, because their storage is identical. The `csharp-wax` side has its own, because its storage is not. A side's `workloads_excluded` lists workloads it does not run, and the harness refuses them. The `csharp` side excludes the bulk payment file and both placement workloads: Marten has no set-based append inside a caller's transaction, so both C# sides would run the same writer, and WORKLOADS.md runs the C# bulk workloads once, on `csharp-direct`.
- Every run starts from a fresh copy of the seed, made with `CREATE DATABASE ... TEMPLATE`, and the engine runs for the whole run in its own container.
- The engine must finish starting before anything is measured. After `--engine-settle` seconds (5 by default), the harness waits until the engine's container has used less than half a core in two 2-second windows in a row, up to `--engine-ready-timeout` (180 seconds), and only then runs the host probe and starts the bench. A Python engine spends about half a minute at its CPU limit while its processes import Django; without the wait, that start-up fell inside the measured window and slowed the host probe. Each run records how long the engine took (`engine_ready_s`) and its CPU once settled.

## Diagnostics

Two flags change what a run does, to show where the time goes. Each is named in the run's label and in `run.md`, and neither is a result on its own.

- `--no-engine` runs the bench without the engine, so no projection, rule, or register poller reads the stream while the bench writes. There is no catch-up to measure. The read-model gates are skipped, and so, for the real-time workload, are the register and first-contact gates. `verify-chain` and the workload's own data gates still run.
- `--hash off` sets `WAX_HASH=off` on the engine and bench containers. Each side then stores a fixed placeholder hash instead of reading the aggregate's previous hash and computing a new one, and everything else stays the same. `verify-chain` does not run, and the chain gate is skipped.

`run.md` lists every gate a diagnostic skipped, and `results.json` records each run's skipped gates beside the ones that ran.

## Using it

From this folder, with Docker, on the benchmark machine. On Windows, run it with `py`; it calls Docker directly, so Git Bash path rewriting does not apply.

```
py bakeoff_harness.py selftest --side python
py bakeoff_harness.py prepare --side python --accounts 500000
py bakeoff_harness.py run --side python --workload batch-payments --workers 1,2,4,8,16,32 --accounts 500000
py bakeoff_harness.py run --side python --workload batch-accrual --workers 1,2,4,8,16,32 --accounts 500000
py bakeoff_harness.py run --side python --workload realtime --workers 8 --accounts 500000 --warmup 15 --duration 120 --engine-args "--rule-workers 4"
py bakeoff_harness.py run --side python --workload batch-payments-bulk --workers 1,2,4,8,16,32 --accounts 500000
py bakeoff_harness.py run --side python --workload placement-bulk --workers 1,2,4,8,16,32 --accounts 500000 --placements 100000
py bakeoff_harness.py teardown --side python
```

A placement run places `--placements` new accounts, 100,000 by default, on top of the seed, which `--accounts` still names.

`selftest` checks that the Compose files merge, that `pg_stat_statements` is loaded, that the SQL `derived_uuid` matches the shared vectors, and that cgroup counters are readable. `prepare` builds the side's image, migrates a template database, and seeds it with the side's own loader; seeding is not measured. `run` makes one run per worker count, each on a fresh copy of the template.

A recorded result uses one warm-up run and three measured runs per worker count, with `--warmup-runs 1 --repeat 3 --record`, and only when Rob has said the machine is idle for the night. Run one side at a time, never both at once. Without `--record`, output goes to `out/`, which git ignores.

Each run directory holds `run.md` (the summary table, with medians of the measured runs), `results.json` (every measurement), `environment.json` (the machine: the host's CPU model, logical cores, RAM, and physical disks, with the power plan and power source on Windows and the kernel and CPU governor on Linux; on a cloud sandbox, the provider, instance type, instance ID, region, and availability zone from `/etc/openar-sandbox/host-facts.json`, which the harness reads instead of calling the metadata service, and which on any other host is absent and leaves those keys null; the Docker platform and Compose versions; the PostgreSQL version and non-default settings; the repository commit, the image of every container, and any other containers running), and `raw/`, with each run's bench output, `verify-chain` output, engine log, and top statements (`.statements.json`), unedited.
