# Engine bake-off

This experiment runs the same Wax workloads in two stacks, on the same machine and the same PostgreSQL configuration, and compares how fast each can push work through the Wax write path while keeping Wax's invariants. The champion is C# on .NET with Marten as the event store, the stack adopted in [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md). The challenger is Python, the stack proposed in Andy Troiano's pull request on his fork of platform-design.

Status: setting up. Both sides are built: the Python side is adapted from Andy's prototype and its 27 tests pass, and the C# side matches it rule for rule, hashes identically, and passes 24 tests. The workloads in WORKLOADS.md and the harness come next. The method below is a draft, and no results have been recorded.

## Background

WAX-0025 chose C# on .NET, with Marten as the event store, in August 2026. It weighed four criteria (platform-agnostic deployment, performance, ecosystem maturity, and the contributor community the project can realistically attract) and counted the founding team's fluency in C# as a further advantage in the first year.

On 2026-10-08 Andy Troiano proposed replacing that decision with Python on Django, in [atroiano/platform-design#1](https://github.com/atroiano/platform-design/pull/1). He backed the proposal with a working prototype: an event store written directly against PostgreSQL, a projection reader, a HiveAR slice that posts payments, and a blue-green release drill. Most of what separates the two stacks is a judgment call for the Foundation, which decides under its governance process. One question can be measured instead of argued: whether each stack can move work through the Wax write path fast enough. This experiment measures that, and nothing else.

## Why throughput is the question

[WAX-0024 High Availability, Disaster Recovery, and Distributed Workloads](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0024-high-availability-disaster-recovery-and-distributed-workloads.md) requires batch jobs to run through the normal command handler path, spread across instances and threads. Every command in a batch therefore does the full work of a single write: it locks the aggregate's state row, checks the aggregate's version, computes the event's hash and links it to the prior event, writes the event, and updates the authoritative state, all in one transaction. The same path carries the daytime load: payments and disputes arriving from people and files, and the workflow rules and scheduled events that react to them.

The largest collection database seen in practice holds 200 million accounts, and the heaviest nightly job touches every one of them. One pass over 200 million accounts in an eight-hour overnight window is about 6,900 commands per second, sustained, which is the pass threshold for the batch workloads. A six-hour window would make it about 9,300.

Wax v1 defers hash computation, multi-instance batch work, and the Pending Event Register to v2. The bake-off includes them anyway, because the language has to carry the full design, not only the first release.

## Contestants

| Folder | Role | Stack | Event store |
| --- | --- | --- | --- |
| [csharp-marten](csharp-marten/README.md) | Champion | C# on the current .NET LTS release | Marten on PostgreSQL |
| [python](python/README.md) | Challenger | Python 3.13 | Andy Troiano's own event store, written directly against PostgreSQL |

The Foundation builds and tunes both sides to the best of its ability. When the results are published, Andy Troiano and anyone else is invited to review either side and submit improvements; an accepted improvement means the affected runs are repeated and the results updated.

## Where Django fits

Andy's proposal is Python on Django, but Django is not the event store. His prototype writes events with its own SQL, and Django supplies the database connection, transactions, migrations, and the HTTP API. The bake-off drives the command handlers directly rather than over HTTP, so the web layer is outside the measured path on both sides. The Python side starts from Andy's code as written, with Django's database layer in the path, because that is what his proposal would ship. Replacing that layer with plain psycopg is allowed as tuning, and each result records which variant ran.

The two sides are not symmetrical, and the test does not pretend otherwise. On the C# side the event store is a library the Foundation would adopt; on the Python side it is code the Foundation would own.

## Invariants both sides keep

Every measured command does all of the following. A run that skips any of them does not count.

1. It locks the aggregate's authoritative state row with `SELECT FOR UPDATE` before computing anything, as [WAX-0013 Event Chain Integrity and Tamper-Evidence](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0013-event-chain-integrity-and-tamper-evidence.md) requires.
2. It rejects the write if the aggregate's version has moved since the command loaded it, the optimistic concurrency check in [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md).
3. It computes `event_hash` and `prior_event_hash` for each event, over the fields WAX-0013 lists, inside the same transaction as the event write. In Wax this is the Wax Security Module's job; the bake-off implements the same computation in each language.
4. It writes the event and updates the authoritative state in one atomic transaction, as [WAX-0002 CQRS Model](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0002-cqrs-model.md) requires.
5. It writes events one command at a time through the command handler, as WAX-0024 requires of batch work. A bulk insert that bypasses the per-event hash is not allowed.

## Method (draft)

[WORKLOADS.md](WORKLOADS.md) specifies exactly what runs, so that both sides do the same work: the seed, the commands added for the test, the batch jobs, the real-time mix and its rules, the command each side's runtime answers to, and the correctness gates. In outline, two kinds of workload are measured.

**Batch.** A nightly job runs one command against every account in the database, split into chunks of 1,000 that workers claim with `SELECT FOR UPDATE SKIP LOCKED`, as WAX-0024 describes. Two batch jobs are measured: a payment file, which Andy's prototype already implements, and interest accrual, which both sides gain for this test.

**Real time.** Many workers at once simulate a working day. Payments, disputes, and new accounts arrive as commands, as they would from collectors, a payment portal, and inbound files. Each committed event is offered to event-triggered workflow rules, the "when X happens, do Y" rules of [WAX-0023 Workflow Automation Engine](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0023-workflow-automation-engine.md), and a rule that matches performs its action as a new command. Some actions schedule a future-dated event in the Pending Event Register of [WAX-0027 Pending Event Register](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0027-pending-event-register.md); workers claim entries as they come due, fire them, and run their rules in turn. The rule set is the same on both sides.

Each workload runs with 1, 2, 4, 8, 16, and 32 workers, so the results show how each stack scales and where it stops scaling. The first measured runs use a seed of 500,000 accounts, and whether larger sizes are needed is decided after the first side-by-side comparison. A result at 500,000 accounts shows how fast each stack runs the command path while its working set fits in memory; it does not show what happens at 200 million, where it would not. Every result records its data size. The seed is loaded by each side's own bulk loader, in its own event store format, with valid hash chains, and loading it is not measured.

Read models are updated by a separate reader that catches up after the commands commit, the design WAX-0002 describes. Both stacks already have one: Marten's asynchronous projection daemon and Andy's projection reader. The in-transaction mode that WAX-0002 also allows in v1 is not measured.

One harness drives both sides. It measures from outside the process under test, reading container CPU from the kernel's cgroup counters and statements per command from PostgreSQL's `pg_stat_statements`, so neither side reports its own numbers. Each side supplies only a small driver that runs the workload in process against its own command handler.

The benchmark machine is a laptop: an Intel Core i7-13620H with 10 cores and 16 threads, 64 GB of memory, and Windows 11 with Docker Desktop. Runs happen only when the laptop is otherwise idle, one side at a time and never in parallel, against the same PostgreSQL 17 container with the same configuration. Connection pooling is available to both: each side may use its driver's own pool, and either may put pgBouncer in front of PostgreSQL, as tuning that the result records. Each configuration gets a warm-up pass and then three measured runs, and the median is reported.

The measurements are these:

- Sustained commands per second over the measured run.
- Application CPU time and database CPU time per thousand commands.
- Command latency at the 50th, 95th, and 99th percentiles.
- In the real-time workload, the time from an event committing to its rule's action committing, and from a scheduled event coming due to its firing.
- How far the read models fall behind, and how long they take to catch up once a batch ends.

After every run, the correctness gates in WORKLOADS.md are checked. In short: every command was applied exactly once, every hash chain verifies, and once the reader has caught up, every read model agrees with the authoritative state.

## Rules

1. The method is committed before any result is recorded, so the history shows it came first. A later change to the method is committed with its reason, and earlier results are rerun under it.
2. Any tuning is allowed on either side if it keeps every invariant and is committed here.
3. Database tuning applies to both sides or to neither.
4. Every result records the machine, the operating system, the PostgreSQL version and settings, the data size, the commit of each side, and the raw harness output, unedited, in [results](results/README.md).
5. A run that fails a correctness gate is recorded as failed and does not count.
6. If both stacks clear the pass threshold, throughput does not decide the question, and the decision rests on the other grounds in WAX-0025 and the proposal.

## Results

No runs yet. Each run is recorded in [results](results/README.md), and this section will summarize them.

## Attribution

The Python side is adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Parts the bake-off does not measure, such as the blue-green release drill, remain in his fork. The [python README](python/README.md) lists what was kept, what was changed, and what was left out.
