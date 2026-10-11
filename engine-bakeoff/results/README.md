# Results

Each recorded run has its own folder, named for the date it started, the pass, the side, the workload, and the worker count, for example `2026-10-10-third-pass-csharp-wax-batch-payments-w4`. A diagnostic run's folder name ends with what it changed, such as `-hash-off` or `-no-engine`, and an untuned run's ends with `-untuned`. The first pass's folders each hold two worker counts, 4 and 16, because the harness ran them in one invocation; the later passes have one worker count per folder. A folder holds:

- `run.md`, which records the machine, the operating system, the PostgreSQL version and settings, the repository commit, the workload and worker count, the summary numbers, and whether every correctness gate passed.
- `results.json`, with every measurement the harness took.
- `environment.json`, which records the machine and software the run used, including the image of every container and any other containers that were running.
- `raw/`, the harness output for each run, unedited: the bench command's stdout and stderr, `verify-chain`'s output, the engine's log, the top statements, and the full measurements as JSON.

A run that fails a correctness gate is recorded too, marked as failed, and does not count toward the result. All 100 runs of round 1 passed.

The `repository_commit` in `environment.json` cites the commit the run used, by its ID in the repository's original history. [COMMIT-MAP.md](../COMMIT-MAP.md) maps each of those IDs to the published history.

[Appendix A of the round 1 report](../REPORT-ROUND-1.md#appendix-a-every-run) lists every run with a link to its folder.
