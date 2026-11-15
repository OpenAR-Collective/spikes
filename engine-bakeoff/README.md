# Engine bake-off

This experiment runs the same Wax workloads in several builds, on the same machine and the same PostgreSQL configuration, and compares how fast each can push work through the Wax write path while keeping Wax's invariants. In round 1 the champion was C# on .NET with Marten as the event store, the stack adopted in [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md). Two C# variants separated the library from the language, and the challenger was Python, the stack proposed in Andy Troiano's pull request on his fork of platform-design. Three later variants each change one thing: python-direct runs the Python side without Django's object layer, the EF Core mode of the C# side without Marten routes its authoritative state through EF Core, and the batched mode of that side writes its SQL in python-direct's statement shapes. Round 2's diagnostic pass adds modes of that side that change two settings of its database driver or the number of its rule workers, and a fair batched mode.

Status: round 1 is complete. It made 100 measured runs on one laptop from 2026-10-08 to 2026-10-10, and every run passed every correctness gate that applied to it. The findings, their limits, and the Foundation's direction are in [REPORT-ROUND-1.md](REPORT-ROUND-1.md). On the same laptop, the EF Core mini-pass made 34 recorded runs on 2026-10-11, round 2's laptop pass made 31 on the night of 2026-11-12, and round 2's code-shape pass made 41 on the evening of 2026-11-13, and every run of the three passed every gate. Round 2's diagnostic pass made 55 on the night of 2026-11-14, and all but one passed every gate. Their results are below, in [The EF Core mini-pass](#the-ef-core-mini-pass), [Round 2's laptop pass](#round-2s-laptop-pass), [Round 2's code-shape pass](#round-2s-code-shape-pass), and [Round 2's diagnostic pass](#round-2s-diagnostic-pass). Passes on cloud hardware are planned, and their run lists are the `aws-*` plans in [passes](passes/).

## What is here

*Each row is a path in this folder and what it holds.*

| Path | What it holds |
| --- | --- |
| [REPORT-ROUND-1.md](REPORT-ROUND-1.md) | The round 1 report: results, what they mean, and their limits |
| [WORKLOADS.md](WORKLOADS.md) | Exactly what runs and the correctness gates, so that every side does the same work |
| [harness](harness/README.md) | The program that drives every side and measures it from outside |
| [csharp-marten](csharp-marten/README.md) | The C# side, appending through Marten or with plain SQL |
| [csharp-wax](csharp-wax/README.md) | The C# side without Marten |
| [python](python/README.md) | The Python side, adapted from Andy Troiano's prototype |
| [passes](passes/) | Each measured pass's plan, log, and status, the script that writes plans, and the scripts that tabulate the later passes |
| [results](results/README.md) | Every recorded run, with its raw output unedited |
| [shared](shared/) | The hash vectors every side must reproduce |
| [COMMIT-MAP.md](COMMIT-MAP.md) | Maps the commit IDs that result records cite to this repository's published history |

## Background

WAX-0025 chose C# on .NET, with Marten as the event store, in August 2026. It weighed four criteria (platform-agnostic deployment, performance, ecosystem maturity, and the contributor community the project can realistically attract) and counted the founding team's fluency in C# as a further advantage in the first year.

On 2026-10-08 Andy Troiano proposed replacing that decision with Python on Django, in [atroiano/platform-design#1](https://github.com/atroiano/platform-design/pull/1). He backed the proposal with a working prototype: an event store written directly against PostgreSQL, a projection reader, a HiveAR slice that posts payments, and a blue-green release drill. Most of what separates the two stacks is a judgment call for the Foundation, which decides under its governance process. One question can be measured instead of argued: whether each stack can move work through the Wax write path fast enough. This experiment measures that, and nothing else.

## Why throughput is the question

[WAX-0024 High Availability, Disaster Recovery, and Distributed Workloads](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0024-high-availability-disaster-recovery-and-distributed-workloads.md) requires batch jobs to run through the normal command handler path, spread across instances and threads. Every command in a batch therefore does the full work of a single write: it locks the aggregate's state row, checks the aggregate's version, computes the event's hash and links it to the prior event, writes the event, and updates the authoritative state, all in one transaction. The same path carries the daytime load: payments and disputes arriving from people and files, and the workflow rules and scheduled events that react to them.

The largest collection database seen in practice holds 200 million accounts, and the heaviest nightly job touches every one of them. One pass over 200 million accounts in an eight-hour overnight window is about 6,900 commands per second, sustained. A six-hour window would make it about 9,300. The experiment uses 6,900 as a reference point for reading results. It is not a pass line.

Wax v1 defers hash computation, multi-instance batch work, and the Pending Event Register to v2. The bake-off includes them anyway, because the language has to carry the full design, not only the first release.

## The sides

*Each row is one side, with its folder, role, stack, and event store.*

| Side | Folder | Role | Stack | Event store |
| --- | --- | --- | --- | --- |
| C# through Marten | [csharp-marten](csharp-marten/README.md) | Champion | C# on .NET 10 | Marten on PostgreSQL |
| C# with direct append | [csharp-marten](csharp-marten/README.md), with `WAX_APPEND=direct` | Variant | C# on .NET 10 | Marten's tables, appended with plain SQL; Marten's daemon for read models and rules |
| C# without Marten | [csharp-wax](csharp-wax/README.md) | Variant | C# on .NET 10 | No Marten: WAX-0001's `core.event`, with a reader ported from Andy Troiano's prototype for read models and rules; since 2026-11-15, Npgsql keeps each pooled session and prepares frequent statements on the server |
| Python | [python](python/README.md) | Challenger | Python 3.13 | Andy Troiano's own event store, written directly against PostgreSQL |
| Python without Django's object layer | [python](python/README.md), as the harness side `python-direct` (`WAX_PATH=direct`) | Variant | Python 3.13 | Andy Troiano's event store and reader logic, in plain SQL through psycopg |
| C# without Marten, with state through EF Core | [csharp-wax](csharp-wax/README.md), as the harness side `csharp-wax-ef` (`WAX_STATE=efcore`, with Npgsql's defaults) | Variant | C# on .NET 10 | WAX-0001's `core.event`, as on the side without Marten, with authoritative state read and written through EF Core |
| C# without Marten, with batched statements | [csharp-wax](csharp-wax/README.md), as the harness side `csharp-wax-batched` (`WAX_STATE=batched`, with Npgsql's defaults) | Variant | C# on .NET 10 | WAX-0001's `core.event`, as on the side without Marten, with a command's events written in one multi-row insert and its account and bucket changes in set-based statements |
| C# without Marten, with other driver settings | [csharp-wax](csharp-wax/README.md), as the harness sides `csharp-wax-npgsql-defaults` (`DATABASE_NO_RESET_ON_CLOSE=false`, `DATABASE_MAX_AUTO_PREPARE=0`), `csharp-wax-noreset` (`DATABASE_NO_RESET_ON_CLOSE=true` alone), `csharp-wax-autoprepare` (`DATABASE_MAX_AUTO_PREPARE=20` alone), `csharp-wax-prepared` (both), and `csharp-wax-custom-plans` (both, and `DATABASE_PLAN_CACHE_MODE=force_custom_plan`) | Variant | C# on .NET 10 | WAX-0001's `core.event`, as on the side without Marten, with Npgsql's defaults as every csharp-wax run before 2026-11-15 used them, or with one or both of the settings csharp-wax now uses |
| C# without Marten, with fair batched statements | [csharp-wax](csharp-wax/README.md), as the harness side `csharp-wax-fair-batched` (`WAX_STATE=fair-batched`, with Npgsql's defaults), and with both driver settings as `csharp-wax-fair-prepared` | Variant | C# on .NET 10 | WAX-0001's `core.event`, as on the side without Marten, with the batched mode's multi-row event insert and each state update sized to what changed |

The first two variants separate the cost of the language from the cost of the library. The direct-append variant runs the champion's code with one change: it appends events with plain SQL instead of through Marten, as the Python side's own event store does, while Marten's daemon still runs the read models and rules. The variant without Marten drops it altogether: events go to WAX-0001's `core.event`, and a reader ported from Andy Troiano's prototype runs the read models and rules. Together they bear on whether Wax should use Marten for its event store at all.

The later variants each change one thing and keep everything else. python-direct keeps Andy Troiano's event store and reader logic and replaces Django's object layer with plain SQL through psycopg, in the bench and in every engine process; Django still runs the migrations, the seed loader, and `verify-chain`. The EF Core mode runs the side without Marten with its authoritative state read and written through EF Core, as [WAX-0003 Database Platform](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0003-database-platform.md) would have it, while its event store, reader, and rules stay in plain SQL. The batched mode changes no library: it runs the side without Marten with python-direct's statement shapes, a command's events in one multi-row insert and the account and bucket rows it changes in one set-based update per table, to separate the effect of statement shape from the effect of language. Round 2's diagnostic pass adds more modes of the side without Marten. csharp-wax-noreset and csharp-wax-prepared change only Npgsql's settings: the first keeps each pooled session instead of discarding it, and the second also prepares frequent statements on the server. csharp-wax-rw8 runs 8 rule workers instead of 16, and the fair batched mode keeps the batched mode's multi-row event insert while sizing each state update to what changed. Since 2026-11-15, csharp-wax itself runs with both settings. csharp-wax-npgsql-defaults keeps Npgsql's defaults, as every earlier csharp-wax run used them, and the other modes keep the settings they were measured with: the EF Core, batched, fair batched, and rw8 modes run with Npgsql's defaults. csharp-wax-autoprepare sets only Max Auto Prepare, which also switches Npgsql to a lighter reset that keeps cached plans once a connection has a statement prepared, and csharp-wax-custom-plans adds PostgreSQL's `force_custom_plan` to both settings. The [python README](python/README.md) and the [csharp-wax README](csharp-wax/README.md) describe them.

The Foundation builds and tunes every side to the best of its ability. Now that the results are published, Andy Troiano and anyone else is invited to review any side and submit improvements. An accepted improvement means the affected runs are repeated and the results updated.

## Where Django fits

Andy's proposal is Python on Django, but Django is not the event store. His prototype writes events with its own SQL, and Django supplies the database connection, transactions, migrations, and the HTTP API. The bake-off drives the command handlers directly rather than over HTTP, so the web layer is outside the measured path on every side. The Python side starts from Andy's code as written, with Django's database layer in the path, because that is what his proposal would ship. Replacing that layer with plain psycopg is allowed as tuning, and each result records which variant ran. The `python-direct` side is that replacement, built by the Foundation for round 2 and measured in [Round 2's laptop pass](#round-2s-laptop-pass).

The sides are not symmetrical, and the test does not pretend otherwise. On the Marten sides the event store is a library the Foundation would adopt; on the others it is code the Foundation would own.

## Invariants every side keeps

Every measured command does all of the following. A run that skips any of them does not count.

1. It locks the aggregate's authoritative state row with `SELECT FOR UPDATE` before computing anything, as [WAX-0013 Event Chain Integrity and Tamper-Evidence](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0013-event-chain-integrity-and-tamper-evidence.md) requires.
2. It rejects the write if the aggregate's version has moved since the command loaded it, the optimistic concurrency check in [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md).
3. It computes `event_hash` and `prior_event_hash` for each event, over the fields WAX-0013 listed until its change on 2026-10-08 (the note below says what changed), inside the same transaction as the event write. In Wax this is the Wax Security Module's job; the bake-off implements the same computation in each language.
4. It writes the event and updates the authoritative state in one atomic transaction, as [WAX-0002 CQRS Model](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0002-cqrs-model.md) requires.
5. It hashes every event individually and chains it to its own aggregate's previous event. The single-command workloads write one command per transaction through the command handler, as WAX-0024 requires of batch work today. The bulk workloads write a chunk of aggregates per transaction with set-based statements, the change to WAX-0024 that [platform-design pull request 26](https://github.com/OpenAR-Collective/platform-design/pull/26) drafts for designated bulk operations; their events are still hashed and chained one by one. A bulk insert that bypasses the per-event hash is not allowed.

Every side hashes the list of fields that WAX-0013 named until 2026-10-08, when it moved to a hash over the event's full payload, including `recorded_at` and the two sequence numbers, which the store assigns and the hashing code must read inside the transaction before it computes the hash. No side does that yet, so the comparison between the sides holds, but the cost of the chain is understated by whatever that read adds to each write.

## Method

[WORKLOADS.md](WORKLOADS.md) specifies exactly what runs, so that every side does the same work: the seed, the commands added for the test, the batch jobs, the real-time mix and its rules, the command each side's runtime answers to, and the correctness gates. In outline, three kinds of workload are measured.

**Batch.** A nightly job runs one command against every account in the database, split into chunks of 1,000 that workers claim with `SELECT FOR UPDATE SKIP LOCKED`, as WAX-0024 describes. Two batch jobs are measured: a payment file, which Andy's prototype already implements, and interest accrual, which every side gains for this test.

**Bulk.** The Foundation added bulk workloads after the first pass showed that database CPU per command sets every side's ceiling. The same payment file, and a mass placement of new accounts, are written a chunk of 1,000 aggregates per transaction with set-based statements, beside the same work done one command per transaction. They measure how much of the ceiling is the transaction per command. WORKLOADS.md says exactly what each does.

**Real time.** Many workers at once simulate a working day. Payments, disputes, and new accounts arrive as commands, as they would from collectors, a payment portal, and inbound files. Each committed event is offered to event-triggered workflow rules, the "when X happens, do Y" rules of [WAX-0023 Workflow Automation Engine](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0023-workflow-automation-engine.md), and a rule that matches performs its action as a new command. Some actions schedule a future-dated event in the Pending Event Register of [WAX-0027 Pending Event Register](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0027-pending-event-register.md); workers claim entries as they come due, fire them, and run their rules in turn. The rule set is the same on every side. The mix warms up for 60 seconds and is measured for 180.

A plan sets which worker counts run. Round 1 used 4, 8, 16, and 32 workers and a seed of 500,000 accounts. A result at 500,000 accounts shows how fast each stack runs the command path while its working set fits in memory; it does not show what happens at 200 million, where it would not. Every result records its data size. The seed is loaded by each side's own bulk loader, in its own event store format, with valid hash chains, and loading it is not measured.

Read models are updated by a separate reader that catches up after the commands commit, the design WAX-0002 describes. Marten's asynchronous projection daemon does it on the two Marten sides, and a reader ported from Andy's projection reader does it on the other two. The in-transaction mode that WAX-0002 also allows in v1 is not measured.

One harness drives every side. It measures CPU from each container's kernel cgroup counters, statements per command from PostgreSQL's `pg_stat_statements`, and catch-up, rule delay, and the correctness gates from the database, all from outside the process under test. Throughput and command latency come from each side's bench driver, a small program that runs the workload in process against the side's own command handler and counts what it sent and what succeeded; the gates check those counts against the database. The measurements are:

- Sustained commands per second over the measured run, and command latency at the 50th, 95th, and 99th percentiles.
- Application CPU time and database CPU time per command. CPU per command is each container's CPU over the whole bench command, divided by every command sent in it, the warm-up's included. Statements and commits per command are divided the same way.
- In the real-time workload, the time from an event committing to its rule's action committing, and from a scheduled event coming due to its firing.
- How far the read models fall behind, and how long they take to catch up once the bench command ends: the time until every projection and the rule runner have reached the head as it stood at that moment. Events written afterward, such as scheduled reviews still firing, do not count.
- The 25 statements with the most database time, from `pg_stat_statements`.

The harness has gained records over the rounds, and the report says which runs have which. The third pass records a host-speed probe, a fixed CPU-bound statement timed in the run's database, and each container's CPU-throttle counters. After the third pass the harness also records steal time, write-ahead-log and checkpoint activity, and how long the engine took to start, and it waits for the engine's CPU to go quiet before it measures. The 100 round 1 runs predate those last four changes. The [harness README](harness/README.md) describes every record.

After every run, the correctness gates in WORKLOADS.md are checked. In short: every command was applied exactly once, every hash chain verifies, and once the reader has caught up, every read model agrees with the authoritative state.

The benchmark machine for round 1 is a laptop: an Intel Core i7-13620H with 10 cores and 16 threads, 64 GB of memory, and Windows 11 with Docker Desktop. Each result's `environment.json` records the rest, including the disks and the power plan. Runs happen only when the laptop is otherwise idle, one side at a time and never in parallel, against the same PostgreSQL container with the same configuration. The containers' CPU limits, 6 for the database, 4 for the bench driver, and 2 for the engine, add up to 12 of the laptop's 16 logical processors, leaving 4 for Windows, the Docker VM, and the harness. A limit caps CPU time; it does not reserve cores. The laptop's 16 logical processors come from 6 performance cores with two threads each and 4 efficiency cores, so when every container is busy at once, a logical processor does less than a performance core's full work. Connection pooling is available to every side: each side may use its driver's own pool, and any may put pgBouncer in front of PostgreSQL, as tuning that the result records.

The EF Core mini-pass and round 2's passes ran on the same laptop in the same way, with two differences that their records show. Containers and development services unrelated to the bake-off stayed up through them, 13 containers in the mini-pass and 14 in each of round 2's passes, and each run's `environment.json` counts them. Docker Desktop was also updated between them: the mini-pass ran on Docker Engine 29.7.2 and round 2 on 29.9.2. Each of those passes therefore compares its sides only with each other.

## Passes

A pass is one session of recorded runs. From the second pass on, each pass has a plan that is committed before it starts: it lists every run, its priority group, and its place in an order shuffled from a recorded seed, and a run that would not finish before the pass's deadline is skipped, so the lowest group drops first. The plans come from `make_plan.py`, the runner is `run_pass.py`, and the runner records each pass in a log and a status file.

- **First pass,** 2026-10-08 and 2026-10-09: 18 runs. Its scope was fixed in this README before the pass began, in original commit 66e919b (see [COMMIT-MAP.md](COMMIT-MAP.md)), and it has no plan file or runner log.
- **Second pass,** 2026-10-09 and 2026-10-10: 61 of 82 planned runs. See the [plan](passes/second-pass.plan.md) and the [log](passes/second-pass.log).
- **Third pass,** 2026-10-10, group 1 only: 21 runs. See the [plan](passes/third-pass.plan.md) and the [log](passes/third-pass.log).
- **EF Core mini-pass,** 2026-10-11: all 34 planned runs. The side without Marten against the same side with its state reads and writes through EF Core (`csharp-wax-ef`, a mode of csharp-wax), to test whether EF Core earns its keep on the command path, as [WAX-0003 Database Platform](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0003-database-platform.md) would have it: 32 runs at 8 workers in back-to-back pairs, with a plain-against-plain pair per configuration that measures the noise, and one placement pair. The result is judged against criteria the Foundation approved before any run. See the [plan](passes/efcore-pass.plan.md), which lists the runs, the criteria, and the commit measured, and the [log](passes/efcore-pass.log). [judge_efcore.py](passes/judge_efcore.py) applies the criteria to the recorded results.
- **Round 2's laptop pass,** 2026-11-12 to 2026-11-13: all 31 planned runs. python-direct, the Python command path in plain SQL, against csharp-wax, the C# side without Marten, run back to back for each workload and worker count, with Django's Python at 8 workers as a same-night anchor, at 500,000 accounts and the standard caps. See the [plan](passes/round2-laptop.plan.md), which lists the runs and the tree measured, and the [log](passes/round2-laptop.log). [summarize_round2.py](passes/summarize_round2.py) builds the tables from the recorded results.
- **Round 2's code-shape pass,** 2026-11-13: all 41 planned runs. csharp-wax-batched, a mode of csharp-wax that writes its SQL the way python-direct does (a command's events in one multi-row insert, and its account and bucket changes in set-based statements), against plain csharp-wax and python-direct, run back to back, to separate the effect of statement shape from the effect of language, and placement for the two C# modes, normally and with the engine paused, at 500,000 accounts and the standard caps. See the [plan](passes/round2-shape.plan.md), which lists the runs and the tree measured, and the [log](passes/round2-shape.log). [round2-shape.tests.txt](passes/round2-shape.tests.txt) records the test suite on that tree in both C# modes, and [summarize_shape.py](passes/summarize_shape.py) builds the tables from the recorded results.
- **Round 2's diagnostic pass,** 2026-11-14 to 2026-11-15: all 55 planned runs, one of which failed a gate. It asks why csharp-wax's event insert costs about twice python-direct's per row. It ran csharp-wax on the payment file at 8 workers unchanged, without the connection pool's reset, without it and with statement preparation, and with 8 rule workers instead of 16, against python-direct, in three back-to-back blocks. Extras followed in priority order: a fair batched mode, 16 and 32 workers, the accrual and real time at 8 workers, and one worker with the engine paused, all at 500,000 accounts and the standard caps. See the [plan](passes/round2-diag.plan.md), which lists the runs and the tree measured, and the [log](passes/round2-diag.log). [round2-diag.tests.txt](passes/round2-diag.tests.txt) records the test suite on that tree in each configuration, [round2-diag.reset-check.txt](passes/round2-diag.reset-check.txt) holds the output of the check that times the connection pool's reset, and [summarize_diag.py](passes/summarize_diag.py) builds the tables from the recorded results. Seventeen repeats of the configuration that failed ran after the pass, outside its plan, and [round2-diag.seq-race.txt](passes/round2-diag.seq-race.txt) holds the output of [seq_race.sh](passes/seq_race.sh), the test of the readers' sequence read that ran during the first of them.

[Appendix B of the report](REPORT-ROUND-1.md#appendix-b-notes-on-each-pass) says what each round 1 pass ran and what is particular to it. The four sections below do the same for the later passes.

## The EF Core mini-pass

[WAX-0003 Database Platform](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0003-database-platform.md) routes all non-event data access through EF Core, and no side of round 1 used it. The mini-pass asks whether EF Core earns its keep on the command path. It ran the side without Marten in back-to-back pairs: in its plain mode, where every statement is written by hand, and in its EF Core mode (`csharp-wax-ef`), where the command handlers read and write the authoritative state through EF Core 9 and everything else stays the same. The EF Core mode was tuned as far as the test's design allows, with DbContext pooling, compiled queries, a compiled model, and no-tracking reads, and without a third-party bulk library or raw-SQL state updates. The [csharp-wax README](csharp-wax/README.md#the-ef-core-mode-harness-side-csharp-wax-ef) describes the mode and its tuning.

The pass measured commit 054af7d at 8 workers and 500,000 accounts, with `--projection-workers 3 --rule-workers 16` in both modes, on 2026-10-11 from 11:27 to 17:17. Four configurations each ran three plain-and-EF blocks and one plain-against-plain block that measures the noise: the payment file, real time, the interest accrual, and the payment file with the engine paused. One placement pair ran as well, and all 34 runs passed every gate. Before any run, the Foundation approved the criteria EF Core had to meet in every configuration, each judged on the median of three blocks and only beyond the noise: database CPU per command within +10 percent of plain, application CPU per command within +50 percent or +1.0 ms, commands per second within -10 percent, and command latency at the 99th percentile within +25 percent.

*Each row is one configuration at 8 workers and 500,000 accounts. Each cell is the median of the configuration's three EF-against-plain differences, as a percentage of plain; the last row gives the criteria.*

| Configuration | Database CPU per command | Application CPU per command | Commands per second | p99 latency | Statements per command | Criteria missed |
| --- | --- | --- | --- | --- | --- | --- |
| Payment file | +27.2% | +108.1% | -22.9% | +25.4% | +19.8% | 4 of 4 |
| Real time | +20.4% | +99.5% | -21.5% | +10.4% | +18.3% | 3 of 4 |
| Interest accrual | +19.4% | +96.6% | -17.5% | +25.6% | +21.0% | 4 of 4 |
| Payment file, engine paused | +17.0% | +127.7% | -18.7% | +12.2% | +16.4% | 3 of 4 |
| Criterion | +10% | +50% or +1.0 ms | -10% | +25% | Reported only | |

EF Core missed 14 of the 16 checks, each by more than the noise its configuration measured. In every configuration it used about twice the application CPU per command, 1.5 to 2.0 ms more, 17 to 27 percent more database CPU per command, and 16 to 21 percent more statements, and it ran 17 to 23 percent fewer commands per second. It met the p99 criterion on real time and with the engine paused, and missed it narrowly on the payment file and the accrual. The plain-against-plain noise was 2 percent or less on every measure for the payment file, with the engine running or paused. The accrual's was 6.6 percent on p99 and under 1 percent otherwise, and real time's was up to 6.0 percent on throughput and 13.8 percent on p99. `judge_efcore.py` prints the full tables, with each mode's median run and each configuration's noise floor.

The placement pair has no noise pair, so it has no verdict, and it is not a like-for-like comparison. The EF Core run placed 439 accounts per second against plain's 336, with 25 percent less database CPU per command. Its engine, whose rule actions are commands and so also ran through EF Core, hit its CPU limit in 90 percent of its scheduler periods, against 0.4 percent for plain, and fell behind. When the bench finished, the EF Core run's event stream was 85,669 events shorter than the plain run's, about a fifth of the 400,000 events each run wrote, and its engine took 84 seconds to catch up, against 2.9 seconds for plain. The work done in that time fell outside the measured window, so the placement pair does not show that EF Core is faster at placement.

Thirteen containers and development services unrelated to the bake-off ran through the pass, so its figures compare only with each other. The result covers one laptop, one worker count, and data that fits in memory.

## Round 2's laptop pass

Round 2 compares the two sides that remain once each language's library is out of the command path: python-direct, the Python side in plain SQL with no Django in the bench or in any engine process, and csharp-wax, the C# side without Marten. Django's Python ran once per workload at 8 workers as a same-night anchor. The Marten sides and the EF Core mode were not in the pass, and the bulk workloads and placement were left out because python-direct does not implement them.

The pass ran from 22:05 on 2026-11-12 to 04:17 on 2026-11-13, at 500,000 accounts and the standard caps. It measured the tree of original commit d31d7f1 (see [COMMIT-MAP.md](COMMIT-MAP.md)), whose Python code is 08dd8c5's and whose C# code is the EF Core mini-pass's, run in plain mode with EF Core's packages present but unused. python-direct used the Python side's engine settings, `--projection-workers 3 --rule-workers 8`, and csharp-wax its own, `--projection-workers 3 --rule-workers 16`. Each workload and worker count ran as a block of the two sides back to back, at 4, 8, and 16 workers; every 8-worker block ran twice, and the payment file and real time also ran at 32 workers. All 31 runs passed every gate.

*Commands per second for every run of the pass, at 500,000 accounts. Where a configuration ran twice, both samples are shown in the order they ran.*

| Workload | Workers | python-direct | csharp-wax | Django's Python |
| --- | --- | --- | --- | --- |
| Payment file | 4 | 458 | 438 | |
| Payment file | 8 | 951, 961 | 895, 931 | 441 |
| Payment file | 16 | 1,583 | 910 | |
| Payment file | 32 | 1,397 | 759 | |
| Interest accrual | 4 | 412 | 446 | |
| Interest accrual | 8 | 853, 833 | 921, 901 | 592 |
| Interest accrual | 16 | 1,652 | 1,736 | |
| Real time | 4 | 477 | 440 | |
| Real time | 8 | 841, 840 | 933, 936 | 427 |
| Real time | 16 | 1,397 | 656 | |
| Real time | 32 | 1,413 | 597 | |

*python-direct against csharp-wax within each back-to-back block, as a percentage of csharp-wax; a positive figure means python-direct was higher.*

| Workload | Workers | Sample | Commands per second | Application CPU per command | Database CPU per command | Statements per command |
| --- | --- | --- | --- | --- | --- | --- |
| Payment file | 4 | 1 | +5% | +39% | -27% | -18% |
| Payment file | 8 | 1 | +6% | +104% | -20% | -15% |
| Payment file | 8 | 2 | +3% | +109% | -18% | -15% |
| Payment file | 16 | 1 | +74% | +106% | -46% | -17% |
| Payment file | 32 | 1 | +84% | +153% | -50% | -12% |
| Interest accrual | 4 | 1 | -7% | +17% | -25% | -17% |
| Interest accrual | 8 | 1 | -7% | +76% | -20% | -16% |
| Interest accrual | 8 | 2 | -8% | +79% | -20% | -16% |
| Interest accrual | 16 | 1 | -5% | +178% | -17% | -15% |
| Real time | 4 | 1 | +8% | +37% | -24% | -16% |
| Real time | 8 | 1 | -10% | +123% | -5% | -19% |
| Real time | 8 | 2 | -10% | +123% | -5% | -20% |
| Real time | 16 | 1 | +113% | +70% | -52% | -29% |
| Real time | 32 | 1 | +137% | +90% | -57% | -24% |

What the pass shows:

- At 4 and 8 workers the two sides were within about 10 percent of each other on throughput in every workload. python-direct led the payment file by 3 to 6 percent and real time at 4 workers by 8; csharp-wax led the accrual by 7 to 8 percent and real time at 8 workers by 10. The two samples of each 8-worker configuration agreed within 4 percent.
- In every block, python-direct used 5 to 57 percent less database CPU per command and 12 to 29 percent fewer statements, and 1.2 to 2.8 times the application CPU. The two sides shape their writes differently: python-direct inserts a command's events in one multi-row statement, where csharp-wax sends one insert statement per event in its batch. The pass does not isolate how much of the difference in database CPU that accounts for, so the difference describes the two sides as written, not the two languages.
- At 16 and 32 workers the two sides reached different limits. csharp-wax's database reached its 6-CPU cap on the payment file and real time, in 94 to 99 percent of its scheduler periods, and its database CPU per command rose with concurrency: on the payment file from about 4.4 ms at 8 workers to 6.5 ms at 16 and 7.9 ms at 32, and on real time from 5.0 ms to 8.2 ms and 9.7 ms. python-direct reached the caps of its bench driver and its engine. On the payment file, where no rule actions fire, it ran 1,583 and 1,397 commands per second against csharp-wax's 910 and 759. On real time its rules fell more than a minute behind at p99, and its readers took about two and a half minutes to catch up, so its real-time rates at 16 and 32 workers overstate what it sustained.
- Django's Python, the same-night anchor, ran the payment file at 441 commands per second at 8 workers, against python-direct's 951 and 961, with about 3.7 times the application CPU per command and twice the database CPU, and its readers took 932 seconds to catch up after the bench finished, against 3.5 seconds for python-direct. python-direct differs from it both in its database layer and in its statement shapes, so the gap measures the two together.

Fourteen containers and development services unrelated to the bake-off ran through the pass, so its figures compare only with each other. The result covers one laptop, data that fits in memory, and one sample of each configuration except the 8-worker blocks. `summarize_round2.py` prints the full tables, with latency, CPU by container, catch-up, rule delay, throttling, and the host probe for every run.

## Round 2's code-shape pass

The laptop pass left one question open. python-direct inserts a command's events in one multi-row statement and changes account and bucket rows with set-based statements, where csharp-wax sends one statement per row, and the laptop pass could not say how much of python-direct's lower database CPU came from that. The code-shape pass adds csharp-wax-batched, a mode of csharp-wax (`WAX_STATE=batched`) that writes a command's events in one multi-row insert and the account and bucket rows it changes in one `UPDATE ... FROM unnest(...)` per table, and runs it back to back with plain csharp-wax and python-direct. A twin test requires the batched mode to write the same events and state as the plain mode. The [csharp-wax README](csharp-wax/README.md#the-batched-mode-harness-side-csharp-wax-batched) describes the mode.

The pass ran from 18:00 to 23:50 on 2026-11-13, at 500,000 accounts and the standard caps. It measured the tree of original commit c8fa969 (see [COMMIT-MAP.md](COMMIT-MAP.md)): the laptop pass's tree with the batched mode added, in which python-direct's code and the statements plain csharp-wax sends are unchanged. Both C# modes used `--projection-workers 3 --rule-workers 16`, and python-direct used `--projection-workers 3 --rule-workers 8`. The payment file, the accrual, and real time each ran as a block of the three back to back at 8 and 16 workers, and every 8-worker block ran twice. Placement, which python-direct does not implement, ran for the two C# modes at 8 workers in three blocks with the engine running and three with it paused, and a pair of plain runs back to back measured the noise. All 41 runs passed every gate.

The laptop ran slower during this pass than during the laptop pass the night before, and the cause was not found. python-direct's code did not change, yet it ran the payment file at 8 workers at 710 and 718 commands per second, against 951 and 961 the night before, and each of its main statements took 6 to 13 percent longer per call. The seed templates were identical in rows and sizes, the host probe read 703 to 804 ms against 700 to 816 the night before, and the unrelated containers' logs showed nothing unusual. Windows Update installed updates from 19:17 to 19:23, during two of the runs, and the laptop's temperature and clock speed under load were not recorded. Within a back-to-back block the runs were steady: the two runs of the noise pair agreed within 0.3 percent on throughput and on database CPU per placement. Between blocks they were not, and the same run differed by up to 30 percent from one block to another. This section therefore compares sides only within a block and reports a difference only when it held in every block. The pass's absolute rates do not compare with the laptop pass's.

*Commands per second for every run of the pass, at 500,000 accounts; for placement, placements per second. Where a configuration ran more than once, the samples are listed by block, r1 first; the [plan](passes/round2-shape.plan.md) gives the order in which they ran.*

| Workload | Workers | python-direct | csharp-wax | csharp-wax-batched |
| --- | --- | --- | --- | --- |
| Payment file | 8 | 710, 718 | 647, 653 | 601, 630 |
| Payment file | 16 | 1,242 | 1,183 | 1,085 |
| Interest accrual | 8 | 793, 571 | 750, 637 | 888, 618 |
| Interest accrual | 16 | 1,173 | 1,328 | 1,307 |
| Real time | 8 | 853, 694 | 948, 684 | 668, 660 |
| Real time | 16 | 1,036 | 757 | 833 |
| Placement, engine running | 8 | | 385, 430, 446 | 413, 422, 531 |
| Placement, engine paused | 8 | | 617, 626, 620 | 636, 624, 638 |

*csharp-wax-batched against plain csharp-wax within each back-to-back block, as a percentage of plain; a positive figure means batched was higher. The last row compares the two plain runs of the noise pair.*

| Workload | Workers | Sample | Commands per second | Application CPU per command | Database CPU per command | Statements per command |
| --- | --- | --- | --- | --- | --- | --- |
| Payment file | 8 | 1 | -7% | +3% | +9% | -4% |
| Payment file | 16 | 1 | -8% | +8% | +11% | -6% |
| Payment file | 8 | 2 | -3% | -1% | +4% | -7% |
| Interest accrual | 8 | 1 | +18% | -11% | -5% | -1% |
| Interest accrual | 16 | 1 | -2% | -3% | +4% | 0% |
| Interest accrual | 8 | 2 | -3% | +1% | +8% | +1% |
| Real time | 8 | 1 | -30% | +26% | +24% | +1% |
| Real time | 16 | 1 | +10% | -7% | -9% | -4% |
| Real time | 8 | 2 | -3% | -1% | +5% | -6% |
| Placement, engine running | 8 | 1 | +7% | -12% | -8% | -1% |
| Placement, engine running | 8 | 2 | -2% | -5% | +2% | -1% |
| Placement, engine running | 8 | 3 | +19% | -17% | -14% | -1% |
| Placement, engine paused | 8 | 1 | +3% | -2% | -8% | 0% |
| Placement, engine paused | 8 | 2 | 0% | +4% | -1% | 0% |
| Placement, engine paused | 8 | 3 | +3% | -5% | -8% | 0% |
| Placement noise pair, plain against plain | 8 | | 0% | -2% | 0% | 0% |

*The three statements the batched mode changes, on the payment file, from `pg_stat_statements`: calls per run and mean milliseconds per call, as the range over the pass's three payment-file blocks. Each run makes 500,000 payments.*

| Statement | csharp-wax | csharp-wax-batched | python-direct |
| --- | --- | --- | --- |
| Event insert | 1,000,000 calls of one row, 0.306 to 0.314 ms | 500,000 calls of two rows, 0.573 to 0.625 ms | 500,000 calls of two rows, 0.323 to 0.337 ms |
| Account update | 500,000 single-row calls setting five columns, 0.053 to 0.055 ms | 500,000 set-based calls setting the same five columns on one row, 0.113 to 0.123 ms | 500,000 set-based calls setting the version on one row, 0.056 to 0.061 ms |
| Bucket update | 583,334 single-row calls, 0.061 to 0.065 ms | 500,000 set-based calls for 583,334 rows, 0.087 to 0.093 ms | 500,000 set-based calls in two forms for 583,334 rows: 476,666 at 0.068 to 0.072 ms and 23,334 at 0.136 to 0.149 ms |

What the pass shows:

- The batched mode wrote the statements it was built to write: on the payment file it halved the event insert calls and replaced the single-row account and bucket updates with set-based ones. It did not lower the cost. On the payment file, the only workload whose blocks all agreed, it ran 3 to 8 percent fewer commands per second than plain csharp-wax, with 4 to 11 percent more database CPU per command.
- Each payment changes one account, so the account update touches one row in either form, and its set-based form cost about twice the single-row update of the same five columns. python-direct's set-based update sets only the version and costs about what the single-row update does. The set-based bucket update cost more per call than it saved in calls, and the multi-row event insert saved little, because a two-row insert from csharp-wax cost about twice a one-row insert.
- csharp-wax's event insert cost 0.29 to 0.31 ms per row in either mode, against 0.16 to 0.17 ms for python-direct's insert of the same 18 columns into the same table. The gap is in the laptop pass's records too, at 4 workers as at 8: there csharp-wax's insert cost 0.300 ms per row at 4 workers and 0.256 to 0.262 at 8, against python-direct's 0.158 and 0.147 to 0.148. The recorded runs do not show why csharp-wax's event row costs about twice as much, and the cause is open.
- The accrual writes one event per command, so the batched mode has nothing to combine, and its blocks disagreed, as real time's did; neither shows a consistent difference between the two modes. With the engine paused, the batched mode used 1 to 8 percent less database CPU per placement in every block, and its rate was 3 percent higher in two blocks and level in the third. With the engine running, the blocks disagreed on rate and database CPU, and the batched mode's engine finished further behind in every block, with 5.6 to 8.2 seconds of catch-up against 3.4 and register p99 of 11.2 to 16.2 seconds against 7.9 to 9.5.
- Within each block, python-direct against batched csharp-wax showed the trade the laptop pass showed against plain csharp-wax. On the payment file python-direct ran 14 to 18 percent more commands per second, with 30 to 36 percent less database CPU per command and 1.5 to 2.4 times the application CPU. On the accrual it ran 8 to 11 percent fewer, with 19 to 26 percent less database CPU and 1.8 to 2.4 times the application CPU. On real time it ran 5 to 28 percent more, but its engine hit its CPU limit in 70 percent of its scheduler periods in the first 8-worker block and 91 percent at 16 workers, where its rules fell 33 seconds behind at p99 and its readers took 81 seconds to catch up, so its real-time rate at 16 workers overstates what it sustained.

As built, the batched mode does not show that statement shape explains python-direct's lower database CPU. It is not an exact copy of python-direct's statements, since its account update sets five columns where python-direct's sets one. The largest difference it leaves in place is the per-row cost of the event insert, whose cause is open. [Round 2's diagnostic pass](#round-2s-diagnostic-pass) takes it up.

Fourteen containers and development services unrelated to the bake-off ran through the pass, as in the laptop pass. The result covers one laptop, which ran slower than the night before for a reason not found, data that fits in memory, and two samples of each 8-worker configuration and one of each other. `summarize_shape.py` prints the full tables, with latency, CPU by container, event insert calls and rows, catch-up, rule and register delay, throttling, and the host probe for every run.

## Round 2's diagnostic pass

The code-shape pass left one cost unexplained: csharp-wax's event insert cost about twice python-direct's per row, for the same 18 columns in the same table. Two driver defaults differ between the sides. Npgsql returns each pooled connection with `DISCARD ALL`, which also discards the session's cached plans, including those of the event trigger and the foreign-key check, and it prepares no statement unless asked, where psycopg prepares a statement on the server after five uses. csharp-wax also runs 16 rule workers to python-direct's 8. The diagnostic pass tests each suspect with a mode of csharp-wax that changes it: csharp-wax-noreset sets Npgsql's No Reset On Close, so a returned connection keeps its session; csharp-wax-prepared also sets Max Auto Prepare to 20; and csharp-wax-rw8 runs 8 rule workers. The pass also adds csharp-wax-fair-batched, a fair batched mode that keeps the batched mode's multi-row event insert and sizes each state update to what changed, as python-direct does, and it records the MultiXact caches of `pg_stat_slru` in every run. The csharp-wax README describes the [driver settings](csharp-wax/README.md#driver-settings) and the [fair batched mode](csharp-wax/README.md#the-fair-batched-mode-harness-side-csharp-wax-fair-batched).

The pass ran from 17:30 on 2026-11-14 to 04:00 on 2026-11-15, at 500,000 accounts and the standard caps. It measured the tree of commit 66abf26: the published code-shape pass with the two settings, the fair batched mode, and the counters added, in which python-direct's code and the statements plain csharp-wax sends are unchanged. The C# modes used `--projection-workers 3 --rule-workers 16`, except csharp-wax-rw8, which used `--rule-workers 8` as python-direct did. The core block, the payment file at 8 workers in all five configurations back to back, ran three times, the second time in exactly the reverse order of the first. The extras ran the fair batched mode against csharp-wax-prepared, both with the two settings, in two blocks on the payment file and two on placement with the engine paused. They ran csharp-wax, csharp-wax-prepared, and python-direct back to back in two blocks each at 16 and 32 workers on the payment file and on real time, and in one block each at 8 workers on the accrual and on real time. Last, csharp-wax-prepared ran against python-direct with one worker and the engine paused. All 55 planned runs ran. Run 2, csharp-wax-rw8 in the first core block, failed the gate that every account summary matches its account and its buckets, as described below. Every other run passed every gate, the configuration's two later runs included, and the tables and findings count only runs that passed.

The host probe read 709 to 830 ms before every run, about the range of the earlier round 2 passes, and 14 containers and development services unrelated to the bake-off stayed up throughout, as before. The same configuration varied less between blocks than in the code-shape pass: unchanged csharp-wax ran the payment file at 8 workers at 891, 895, and 900 commands per second in the three core blocks, and python-direct at 882 to 938. As in the code-shape pass, this section compares sides only within a back-to-back block and reports a difference only when it held in every block, and a configuration that ran in one block is a single sample. The pass's absolute rates do not compare with the other passes'.

The plan placed a check before group 1 that times one event insert in csharp-wax's database, straight after `DISCARD ALL` and with the session's plans warm, in a rolled-back transaction on a throwaway copy of the seed template. It could not run at 17:30, because csharp-wax's database had been left stopped, so it ran after the pass, from 04:00:27 to 04:00:41, while a throwaway PostgreSQL container for a separate test was starting. The check alternates its two kinds of insert in rounds of 25, so both saw the same conditions, but it did not run where the plan put it, and it needs a clean rerun in a quiet window.

*The payment file at 8 workers: means over the three core blocks, counting only runs that passed every gate. Database CPU is milliseconds per command; statements and `DISCARD ALL` calls are per command; the event insert is the execution time of `INSERT INTO core.event` per row written, from `pg_stat_statements`, which includes its trigger and foreign-key check.*

| Configuration | Runs | Commands per second | Database CPU | Statements | `DISCARD ALL` calls | Event insert, ms per row |
| --- | --- | --- | --- | --- | --- | --- |
| csharp-wax | 3 | 895 | 4.52 | 17.9 | 1.71 | 0.265 |
| csharp-wax-noreset | 3 | 894 | 4.40 | 16.3 | 0.00 | 0.186 |
| csharp-wax-prepared | 3 | 884 | 3.73 | 16.7 | 0.00 | 0.234 |
| csharp-wax-rw8 | 2 | 891 | 4.38 | 17.1 | 1.48 | 0.268 |
| python-direct | 3 | 912 | 3.76 | 15.2 | 0.00 | 0.155 |

*Each configuration against unchanged csharp-wax within each core block, as the range over the blocks of the percentage difference; a positive figure means the configuration was higher. csharp-wax-rw8's range covers the two blocks in which it passed.*

| Configuration | Commands per second | Database CPU per command | Statements per command | Event insert per row |
| --- | --- | --- | --- | --- |
| csharp-wax-noreset | -2% to +1% | -4% to 0% | -10% to -8% | -30% to -29% |
| csharp-wax-prepared | -3% to 0% | -18% to -16% | -7% | -13% to -11% |
| csharp-wax-rw8 | -1% to 0% | -3% | -5% to -4% | +1% to +2% |
| python-direct | -1% to +5% | -18% to -15% | -15% | -42% to -41% |

*Commands per second in each back-to-back block beyond the core block, at 500,000 accounts. For real time, the rules' p99 delay in seconds follows in parentheses, since it shows whether the rules kept up.*

| Workload | Workers | Block | csharp-wax | csharp-wax-prepared | python-direct |
| --- | --- | --- | --- | --- | --- |
| Payment file | 16 | r1 | 983 | 1,728 | 1,443 |
| Payment file | 16 | r2 | 905 | 1,760 | 1,409 |
| Payment file | 32 | r1 | 776 | 1,362 | 1,376 |
| Payment file | 32 | r2 | 763 | 1,428 | 1,330 |
| Real time | 8 | r1 | 910 (0.5) | 975 (0.5) | 799 (1.3) |
| Real time | 16 | r1 | 661 (0.7) | 1,338 (0.5) | 1,299 (57.9) |
| Real time | 16 | r2 | 685 (0.7) | 1,386 (0.5) | 1,337 (55.2) |
| Real time | 32 | r1 | 627 (1.8) | 1,100 (0.8) | 1,245 (77.4) |
| Real time | 32 | r2 | 642 (1.8) | 1,407 (0.6) | 1,356 (77.0) |
| Interest accrual | 8 | r1 | 869 | 904 | 782 |

*The reset check: median milliseconds per insert over 75 inserts of each kind, for the whole insert and for the two parts of it that use cached plans.*

| Insert | Execution | BEFORE INSERT trigger | Foreign-key check |
| --- | --- | --- | --- |
| Warm | 0.230 | 0.038 | 0.024 |
| After `DISCARD ALL` | 0.317 | 0.069 | 0.087 |

What the pass shows:

- At 8 workers no database came near its CPU limit, which it hit in at most 0.1 percent of scheduler periods. Every other configuration's rate was within 3 percent of unchanged csharp-wax's in every core block, apart from python-direct's 5 percent higher rate in the first, so at 8 workers the configurations differ mainly in cost per command, not in rate.
- Skipping the reset cut the event insert by 29 to 30 percent in every block, from 0.265 to 0.186 ms per row on the means, which closes about 70 percent of its gap to python-direct's 0.155. It also cut statements per command by 8 to 10 percent, yet it moved database CPU per command by only 0 to 4 percent. The insert's saving agrees with the reset check, in which the median insert took 0.087 ms longer after `DISCARD ALL` than warm, with both its trigger and its foreign-key check slower; in the pass, skipping the reset saved 0.079 ms per row.
- With statement preparation as well, csharp-wax-prepared used 16 to 18 percent less database CPU per command than unchanged csharp-wax in every block, and was within 3 percent of python-direct's in every block, at 3.73 ms against 3.76 on the means. The pass did not run preparation without No Reset On Close, so it does not measure the two settings apart.
- csharp-wax-prepared's event insert cost more per row than csharp-wax-noreset's in every block, 0.230 to 0.238 ms against 0.184 to 0.190, so preparation raised the insert's own cost even as it lowered database CPU per command. The pass does not show why.
- Eight rule workers instead of 16 changed database CPU per command by 3 percent and the event insert by 1 to 2 percent in the two blocks where the run passed, so the number of rule workers is not a material part of the gap.
- At 16 and 32 workers unchanged csharp-wax's database hit its 6-CPU limit in 90 to 99 percent of scheduler periods, and its database CPU per command on the payment file rose with the worker count, from 4.5 ms at 8 workers to 6.0 to 6.5 at 16 and 7.7 to 7.9 at 32. csharp-wax-prepared's was 3.7, 3.0, and 4.2 to 4.4, and on the payment file it reached the limit only at 32 workers. In every block at 16 and 32 workers, csharp-wax-prepared ran 76 to 94 percent more commands per second than unchanged csharp-wax on the payment file and 76 to 119 percent more on real time.
- Against python-direct, csharp-wax-prepared ran 20 to 25 percent more commands per second on the payment file at 16 workers in both blocks, with 19 to 22 percent less database CPU per command; at 32 workers the two blocks disagreed. On real time python-direct's rate was 3 to 4 percent lower at 16 workers, and the blocks at 32 disagreed. python-direct's rules fell 55 to 77 seconds behind at p99 at 16 and 32 workers, and its engine finished 136 to 161 seconds behind the bench, against csharp-wax-prepared's 0.5 to 0.8 seconds and about 4 seconds, so python-direct's real-time rates there overstate what it sustained, as in the laptop pass.
- With both settings on each side, the fair batched mode ran within 1 percent of csharp-wax-prepared's rate on the payment file and 1 to 4 percent faster on placement with the engine paused, and it sent 7 percent fewer statements per command on the payment file. Its database CPU per command was 1 percent lower in one payment-file block and 3 percent higher in the other, and 3 to 4 percent lower on placement. None of these differences is large.
- Neither MultiXact cache read a block from disk in any core run, and the pages zeroed differed by less than 6 percent across the five configurations, so MultiXact traffic is not part of the gap.
- `DISCARD ALL` itself cost 0.012 to 0.017 ms per call at 8 and 16 workers, about 0.02 ms per command. Its cost lies in the plans it discards, not in running it.

Run 2 failed one gate: every account summary must match its account and its buckets. The cause is a defect in how the readers read their high-water mark, and both sides' readers share it. Both take the mark from a plain `SELECT last_value` on the event sequence. Each time `nextval` logs the sequence to the write-ahead log, PostgreSQL briefly holds a value 32 ahead in the sequence's row, and a plain read of the sequence can see that value, where `pg_sequence_last_value()`, which takes the buffer's lock, cannot. A reader that takes the inflated value as its mark can move its checkpoint past numbers that open transactions are about to commit, and then never reads their events.

Run 2's bench wrote all of its events, every reader reached the head 3.5 seconds after the bench ended, and every hash chain verified, so the failure was not a timing race in the harness. The harness dropped the run's database at the end of the run, as it does unless told to keep it, so its rows could not be inspected. To catch a failure that could be inspected, the configuration ran 17 more times after the pass, from 04:01 to 07:45 on 2026-11-15, with each run's database kept. These repeats are outside the plan, and their folders end with `-k1` to `-k17`. Repeat k5 reproduced the failure on a gate of the same kind, that every payment read model row matches its payment: three `payment.posted` events with consecutive numbers never reached the payment read model. Its kept database traces the skip to the defect. The highest of the three missing numbers is exactly the value the sequence held for a moment when it last logged itself before them, and the three transactions were still open when the reader read past them. Every other repeat passed every gate. A separate test after the pass, [seq_race.sh](passes/seq_race.sh), confirmed the mechanism: with 16 clients calling `nextval` at about 49,000 calls per second in a throwaway container of the bake-off's PostgreSQL 17.11 image, the plain read returned a number not yet handed out about once in 4 to 8 million reads, never more than 32 ahead, and the locked read never did. Its output is in [round2-diag.seq-race.txt](passes/round2-diag.seq-race.txt). It ran on the same laptop during repeat k1, whose `environment.json` lists its container, `seqrace-pg`. Run 2 is attributed to the same defect by its configuration and the kind of gate it failed, because its database was dropped. Over the night the configuration ran 20 times and failed twice.

The same reading of the code found a second, smaller defect: a reader can write its checkpoint backward and apply some events again, which costs repeated work but gives no wrong results. Unchanged csharp-wax's run in the first core block updated 500,294 summaries for 500,000 payments. The fix for the mark is to read it with `pg_sequence_last_value()`, and the fix for the checkpoint is to skip a page when the locked position is already at or past the safe one. Neither fix is in the measured tree, and neither has been run.

Several questions remain open:

- Preparation raised the event insert's own cost, as described above. One possible cause, the generic plan that PostgreSQL may switch a prepared statement to after five executions, was not tested.
- With one worker and the engine paused, python-direct ran 124 commands per second against csharp-wax-prepared's 108, with nearly the same database CPU per command, 3.57 ms against 3.70, while csharp-wax-prepared sent 11.2 statements per command against 10.0. With one run of each, this is an indication, not a result.
- csharp-wax-prepared used 2.03 to 2.12 ms of application CPU per command at 8 workers against unchanged csharp-wax's 1.74 to 1.77, but 1.19 to 1.23 against 1.65 to 1.78 at 16 workers, for a reason not found.
- The follow-up repeats ran at two speeds. Eight of them ran at 601 to 650 commands per second, with about 25 percent more database CPU and about 40 percent more application CPU per command than the other nine, which ran at 792 to 890. The host probe read about the same in both groups, and the cause was not found. Repeat k1 ran while the sequence test ran beside it, yet at 632 commands per second it was no slower than k2 to k6, which ran without it at 601 to 616, so the test does not explain the slow group. No comparison in this section uses the repeats.

With Npgsql's two defaults changed, csharp-wax used about the same database CPU per command as python-direct on the payment file at 8 workers and less at 16, and where the database was the limit it ran 76 to 119 percent more commands per second than with the defaults. Most of the event insert's extra cost per row went with the reset, which discards the session's cached plans. With both settings the insert still cost more per row than python-direct's, for a reason the pass does not show.

Fourteen containers and development services unrelated to the bake-off ran through the pass, as in the earlier round 2 passes. Two caveats are particular to this pass: the reset check ran after the pass rather than before it and needs a clean rerun, and eight of the follow-up repeats ran slow for a reason not found, which is why no comparison uses the repeats. The result covers one laptop, data that fits in memory, three samples of each core configuration, two of each configuration at 16 and 32 workers, and one of each other. `summarize_diag.py` prints the full tables for the planned runs, with latency, application and database CPU, statements, `DISCARD ALL` calls, the event insert, catch-up, rule delay, throttling, and the host probe for every run, and the MultiXact caches for the core block.

## Cloud passes

Passes on cloud hardware will run in the Foundation's own account, so that the results are not tied to a laptop. None has run yet. There are four plans, each with the engine settings it uses: [aws-anchor](passes/aws-anchor.plan.md), 36 runs at the standard limits; [aws-scaled](passes/aws-scaled.plan.md), 44 runs at larger limits, with a 16-CPU database; [aws-pgbouncer](passes/aws-pgbouncer.plan.md), 16 runs through pgBouncer at the larger limits; and [aws-5m](passes/aws-5m.plan.md), 10 runs on the C# sides at 5,000,000 accounts. The plans may change as the first checks on the cloud host show what needs adjusting.

## Rules

1. The method is committed before any result is recorded, so the history shows it came first. A later change to the method is committed with its reason, and the report says which earlier results it affects.
2. Any tuning is allowed on any side if it keeps every invariant and is committed here.
3. Database tuning applies to every side or to none.
4. Every result records the machine, the operating system, the PostgreSQL version and settings, the data size, the commit of each side, and the raw harness output, unedited, in [results](results/README.md).
5. A run that fails a correctness gate is recorded as failed and does not count.
6. Throughput is evidence, not a verdict. The 6,900 commands per second reference is not a pass line, and a decision about a language or an event store rests on all the grounds WAX-0025 weighs, of which throughput is one.

## Attribution

The Python side is adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Parts the bake-off does not measure, such as the blue-green release drill, remain in his fork. The [python README](python/README.md) lists what was kept, what was changed, and what was left out.
