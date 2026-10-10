# third-pass-csharp-batch-accrual-w16

Recorded run. Side: csharp (BAKEOFF_APPEND=marten, WAX_APPEND=marten). Workload: batch-accrual. Accounts: 500,000. Engine arguments: `--rule-workers 16`. pgBouncer: no. Warm-up runs per worker count: 0; measured runs: 1. Started 2026-10-10T14:23:20, finished 2026-10-10T14:32:31.

Machine: Windows-11-10.0.26300-SP0; Docker 29.7.2 on Docker Desktop, 16 CPUs, 31 GiB. Host: 13th Gen Intel(R) Core(TM) i7-13620H, 16 logical cores, 64 GiB RAM; disks: NVMe Samsung SSD 990 PRO 4TB (NVMe SSD), NVMe WD PC SN740 SDDQNQD-512G-1014 (NVMe SSD); power plan Acer, on AC power. Docker Desktop 4.90.0 (238679). PostgreSQL: PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit. Repository commit 70aff5996112057c081f79f969de79ba5dccd084. Other containers running: 0.

Medians of the measured runs. CPU is milliseconds per command, from each container's cgroup counter over the whole bench command, divided by every command it sent, the warm-up's included (sent_total); engine CPU spent catching up afterward is in the raw results. Statements and commits per command are divided the same way, and count everything the run's database executed in that window, the engine's reads and rules included. Catch-up is the time from the end of the bench command until every reader reached the head as it stood at that end; events written afterward do not count. Rule latency and register lateness are in milliseconds. Transactions are those the bench driver reports committing; in a bulk workload, latency is per transaction, that is, per chunk. Throttled is the percentage of each container's scheduler periods in which it hit its CPU limit; a container throttled for much of the run was capped by its limit. Host probe is the median of five timings of a fixed CPU-bound statement in the run's database just before the bench, outside the measured window; a probe well above the night's usual marks a run during which the machine itself ran slow.

| Workers | Succeeded/s | p50 ms | p99 ms | Bench CPU | Engine CPU | DB CPU | Statements/cmd | Commits/cmd | Transactions (fallback chunks) | Max lag (events) | Catch-up s | Rule p50 / p99 | Lateness p50 / p99 | Throttled % db / engine / bench | Host probe ms | Gates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 16 | 1046.393 | 14.316 | 29.027 | 1.019 | 0.103 | 4.458 | 10.11 | 2.09 | 500000 (0) | 1235 | 3.1 | None / None | None / None | 4.3 / 0.0 / 0.0 | 739.2 | pass |

Each run's top statements by total time, from pg_stat_statements, are in its `raw/` `.statements.json`.

Raw output, one set per run, is in `raw/`: the bench command's stdout and stderr, verify-chain's output, the engine's log, and the full measurements as JSON. `environment.json` holds the PostgreSQL settings and the image of every container.
