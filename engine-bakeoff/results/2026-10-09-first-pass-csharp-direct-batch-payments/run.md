# first-pass-csharp-direct-batch-payments

Recorded run. Side: csharp-direct (BAKEOFF_APPEND=direct, WAX_APPEND=direct). Workload: batch-payments. Accounts: 500,000. Engine arguments: `--rule-workers 4`. pgBouncer: no. Warm-up runs per worker count: 0; measured runs: 1. Started 2026-10-09T01:05:12, finished 2026-10-09T01:42:25.

Machine: Windows-11-10.0.26300-SP0; Docker 29.7.2 on Docker Desktop, 16 CPUs, 31 GiB. PostgreSQL: PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit. Repository commit c7d08ffe0794db805b100a05ac1aeec4480d6f8c. Other containers running: 13.

Medians of the measured runs. CPU is milliseconds per command sent, from each container's cgroup counter over the bench command's run; engine CPU spent catching up afterward is in the raw results. Statements per command count everything the run's database executed in that window, the engine's reads and rules included. Rule latency and register lateness are in milliseconds.

| Workers | Succeeded/s | p50 ms | p99 ms | Bench CPU | Engine CPU | DB CPU | Statements/cmd | Max lag (events) | Catch-up s | Rule p50 / p99 | Lateness p50 / p99 | Gates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | 320.717 | 11.337 | 23.008 | 1.549 | 0.424 | 5.382 | 17.49 | 826 | 1.7 | None / None | None / None | pass |
| 16 | 975.184 | 14.632 | 50.656 | 1.062 | 0.281 | 5.716 | 17.29 | 2702 | 1.5 | None / None | None / None | pass |

Raw output, one set per run, is in `raw/`: the bench command's stdout and stderr, verify-chain's output, the engine's log, and the full measurements as JSON. `environment.json` holds the PostgreSQL settings and the image of every container.
