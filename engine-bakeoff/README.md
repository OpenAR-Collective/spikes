# Engine bake-off

This experiment runs the same Wax workloads in four builds, on the same machine and the same PostgreSQL configuration, and compares how fast each can push work through the Wax write path while keeping Wax's invariants. The champion is C# on .NET with Marten as the event store, the stack adopted in [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md). Two C# variants separate the library from the language, and the challenger is Python, the stack proposed in Andy Troiano's pull request on his fork of platform-design.

Status: round 1 is complete. It made 100 measured runs on one laptop from 2026-10-08 to 2026-10-10, and every run passed every correctness gate that applied to it. The findings, their limits, and the Foundation's direction are in [REPORT-ROUND-1.md](REPORT-ROUND-1.md). Round 2, on cloud hardware, is planned, and its run lists are the `aws-*` plans in [passes](passes/).

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
| [passes](passes/) | Each measured pass's plan, log, and status, and the script that writes plans |
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
| C# without Marten | [csharp-wax](csharp-wax/README.md) | Variant | C# on .NET 10 | No Marten: WAX-0001's `core.event`, with a reader ported from Andy Troiano's prototype for read models and rules |
| Python | [python](python/README.md) | Challenger | Python 3.13 | Andy Troiano's own event store, written directly against PostgreSQL |

The two variants separate the cost of the language from the cost of the library. The direct-append variant runs the champion's code with one change: it appends events with plain SQL instead of through Marten, as the Python side's own event store does, while Marten's daemon still runs the read models and rules. The variant without Marten drops it altogether: events go to WAX-0001's `core.event`, and a reader ported from Andy Troiano's prototype runs the read models and rules. Together they bear on whether Wax should use Marten for its event store at all.

The Foundation builds and tunes every side to the best of its ability. Now that the results are published, Andy Troiano and anyone else is invited to review any side and submit improvements. An accepted improvement means the affected runs are repeated and the results updated.

## Where Django fits

Andy's proposal is Python on Django, but Django is not the event store. His prototype writes events with its own SQL, and Django supplies the database connection, transactions, migrations, and the HTTP API. The bake-off drives the command handlers directly rather than over HTTP, so the web layer is outside the measured path on every side. The Python side starts from Andy's code as written, with Django's database layer in the path, because that is what his proposal would ship. Replacing that layer with plain psycopg is allowed as tuning, and each result records which variant ran.

The sides are not symmetrical, and the test does not pretend otherwise. On the Marten sides the event store is a library the Foundation would adopt; on the other two it is code the Foundation would own.

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

## Passes

A pass is one session of recorded runs. From the second pass on, each pass has a plan that is committed before it starts: it lists every run, its priority group, and its place in an order shuffled from a recorded seed, and a run that would not finish before the pass's deadline is skipped, so the lowest group drops first. The plans come from `make_plan.py`, the runner is `run_pass.py`, and the runner records each pass in a log and a status file.

- **First pass,** 2026-10-08 and 2026-10-09: 18 runs. Its scope was fixed in this README before the pass began, in original commit 66e919b (see [COMMIT-MAP.md](COMMIT-MAP.md)), and it has no plan file or runner log.
- **Second pass,** 2026-10-09 and 2026-10-10: 61 of 82 planned runs. See the [plan](passes/second-pass.plan.md) and the [log](passes/second-pass.log).
- **Third pass,** 2026-10-10, group 1 only: 21 runs. See the [plan](passes/third-pass.plan.md) and the [log](passes/third-pass.log).

[Appendix B of the report](REPORT-ROUND-1.md#appendix-b-notes-on-each-pass) says what each pass ran and what is particular to it.

## Round 2

Round 2 runs on cloud hardware in the Foundation's own account, so that the results are not tied to a laptop. It has four plans, each with the engine settings it uses: [aws-anchor](passes/aws-anchor.plan.md), 36 runs at the standard limits; [aws-scaled](passes/aws-scaled.plan.md), 44 runs at larger limits, with a 16-CPU database; [aws-pgbouncer](passes/aws-pgbouncer.plan.md), 16 runs through pgBouncer at the larger limits; and [aws-5m](passes/aws-5m.plan.md), 10 runs on the C# sides at 5,000,000 accounts. The plans may change as the first checks on the cloud host show what needs adjusting.

## Rules

1. The method is committed before any result is recorded, so the history shows it came first. A later change to the method is committed with its reason, and the report says which earlier results it affects.
2. Any tuning is allowed on any side if it keeps every invariant and is committed here.
3. Database tuning applies to every side or to none.
4. Every result records the machine, the operating system, the PostgreSQL version and settings, the data size, the commit of each side, and the raw harness output, unedited, in [results](results/README.md).
5. A run that fails a correctness gate is recorded as failed and does not count.
6. Throughput is evidence, not a verdict. The 6,900 commands per second reference is not a pass line, and a decision about a language or an event store rests on all the grounds WAX-0025 weighs, of which throughput is one.

## Attribution

The Python side is adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Parts the bake-off does not measure, such as the blue-green release drill, remain in his fork. The [python README](python/README.md) lists what was kept, what was changed, and what was left out.
