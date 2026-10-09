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

## What keeps the two sides equal

- [compose.override.yml](compose.override.yml) is applied on top of either side's own Compose file. It sets the PostgreSQL settings, including `pg_stat_statements`, and the CPU and memory limits of the `db`, `engine`, and `bench` services. Neither side sets these in its own file. A change to it is a change to the method.
- [compose.pgbouncer.yml](compose.pgbouncer.yml) puts pgBouncer in transaction pooling mode in front of the database, with `--pgbouncer`. It is tuning that either side may use, and the result records it.
- [sides.json](sides.json) is the only place the harness knows anything about a side: its Compose file, project name, profile, command-line prefix, and any environment that selects a mode. There are three sides: `python`; `csharp`, which appends events through Marten; and `csharp-direct`, the same C# image appending with plain SQL into Marten's own tables (`WAX_APPEND=direct`), which separates the language from the library on the write path. The two C# sides share one Compose project and one seed template, because their storage is identical. A side's `workloads_excluded` lists workloads it does not run, and the harness refuses them. The `csharp` side excludes the bulk payment file and both placement workloads: Marten has no set-based append inside a caller's transaction, so both C# sides would run the same writer, and WORKLOADS.md runs the C# bulk workloads once, on `csharp-direct`.
- Every run starts from a fresh copy of the seed, made with `CREATE DATABASE ... TEMPLATE`, and the engine runs for the whole run in its own container.

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

Each run directory holds `run.md` (the summary table, with medians of the measured runs), `results.json` (every measurement), `environment.json` (the machine, the PostgreSQL version and non-default settings, the repository commit, the image of every container, and any other containers running), and `raw/`, with each run's bench output, `verify-chain` output, and engine log, unedited.
