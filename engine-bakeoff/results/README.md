# Results

Each recorded run has its own folder, named for the date it started, the pass, the side, the workload, and the worker count, for example `2026-10-10-third-pass-csharp-wax-batch-payments-w4`. A diagnostic run's folder name ends with what it changed, such as `-hash-off` or `-no-engine`, and an untuned run's ends with `-untuned`. The first pass's folders each hold two worker counts, 4 and 16, because the harness ran them in one invocation; the later passes have one worker count per folder. The folders of the EF Core mini-pass and of round 2's passes end with the block a run belongs to: `-r1` to `-r3` for a block that runs two or more sides or modes back to back, `-f1` or `-f2` for the diagnostic pass's fair batched blocks, and `-noise1` or `-noise2` for the two runs of a plain-against-plain block. The diagnostic pass's follow-up repeats, which ran outside its plan, are labeled `round2-diag-followup` and end with `-k1` to `-k17`. A folder holds:

- `run.md`, which records the machine, the operating system, the PostgreSQL version and settings, the repository commit, the workload and worker count, the summary numbers, and whether every correctness gate passed.
- `results.json`, with every measurement the harness took.
- `environment.json`, which records the machine and software the run used, including the image of every container and any other containers that were running.
- `raw/`, the harness output for each run, unedited: the bench command's stdout and stderr, `verify-chain`'s output, the engine's log, the top statements, and the full measurements as JSON.

A run that fails a correctness gate is recorded too, marked as failed, and does not count toward the result. All 100 runs of round 1 passed, and so did all 34 of the EF Core mini-pass, all 31 of round 2's laptop pass, and all 41 of round 2's code-shape pass. Round 2's diagnostic pass recorded one failed run of its 55, and one of its 17 follow-up repeats failed.

The `repository_commit` in `environment.json` cites the commit the run used, by its ID in the repository's original history. [COMMIT-MAP.md](../COMMIT-MAP.md) maps each of those IDs to the published history. The EF Core mini-pass's and round 2's laptop pass's records cite IDs from before a second rewrite, and round 2's code-shape pass's records cite IDs from before a third, which COMMIT-MAP.md maps as well. Round 2's diagnostic pass's records cite its plan commit, 4333ce1, which is in the published history unchanged.

[Appendix A of the round 1 report](../REPORT-ROUND-1.md#appendix-a-every-run) lists every run of round 1 with a link to its folder. The later passes' plans and logs in [passes](../passes/) list theirs, and the [engine bake-off README](../README.md#the-ef-core-mini-pass) summarizes their results.
