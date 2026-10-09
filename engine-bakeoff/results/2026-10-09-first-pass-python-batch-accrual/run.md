# first-pass-python-batch-accrual

Recorded run. Side: python. Workload: batch-accrual. Accounts: 500,000. Engine arguments: `--rule-workers 4`. pgBouncer: no. Warm-up runs per worker count: 0; measured runs: 1. Started 2026-10-09T04:57:03, finished 2026-10-09T05:57:49.

Machine: Windows-11-10.0.26300-SP0; Docker 29.7.2 on Docker Desktop, 16 CPUs, 31 GiB. PostgreSQL: PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit. Repository commit c7d08ffe0794db805b100a05ac1aeec4480d6f8c. Other containers running: 13.

Medians of the measured runs. CPU is milliseconds per command sent, from each container's cgroup counter over the bench command's run; engine CPU spent catching up afterward is in the raw results. Statements per command count everything the run's database executed in that window, the engine's reads and rules included. Rule latency and register lateness are in milliseconds.

| Workers | Succeeded/s | p50 ms | p99 ms | Bench CPU | Engine CPU | DB CPU | Statements/cmd | Max lag (events) | Catch-up s | Rule p50 / p99 | Lateness p50 / p99 | Gates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | 264.131 | 14.33 | 28.3 | 5.019 | 2.504 | 4.55 | 15.17 | 730 | 1.5 | None / None | None / None | pass |
| 16 | 488.831 | 25.26 | 70.8 | 8.189 | 1.51 | 5.666 | 12.01 | 234659 | 548.0 | None / None | None / None | pass |

Raw output, one set per run, is in `raw/`: the bench command's stdout and stderr, verify-chain's output, the engine's log, and the full measurements as JSON. `environment.json` holds the PostgreSQL settings and the image of every container.
