# Engine bake-off

This experiment runs the same Wax workloads in two stacks, on the same machine and the same PostgreSQL configuration, and compares how fast each can push work through the Wax write path while keeping Wax's invariants. The champion is C# on .NET with Marten as the event store, the stack adopted in [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md). The challenger is Python, the stack proposed in Andy Troiano's pull request on his fork of platform-design.

Status: two measured passes are complete, and the first group of the third has run; its other groups wait for Rob's go. Four sides are built and tested: Python, adapted from Andy's prototype; C# through Marten; C# with a direct append; and C# without Marten. Every run of both passes, at 500,000 accounts, passed every correctness gate that applied to it, and their results are below.

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
| [csharp-marten](csharp-marten/README.md), direct append | Variant | C# on the current .NET LTS release | Marten's tables, appended with plain SQL; Marten's daemon for read models and rules |
| [csharp-wax](csharp-wax/README.md) | Variant | C# on the current .NET LTS release | No Marten: WAX-0001's `core.event`, with a reader ported from Andy Troiano's prototype for read models and rules |

The two variants separate the cost of the language from the cost of the library. The direct-append variant runs the champion's code with one change: it appends events with plain SQL instead of through Marten, as the Python side's own event store does, while Marten's daemon still runs the read models and rules. The variant without Marten drops it altogether: events go to WAX-0001's `core.event`, and a reader ported from Andy's prototype runs the read models and rules. Together they bear on whether Wax should use Marten for its event store at all.

The Foundation builds and tunes every side to the best of its ability. When the results are published, Andy Troiano and anyone else is invited to review any side and submit improvements; an accepted improvement means the affected runs are repeated and the results updated.

## Where Django fits

Andy's proposal is Python on Django, but Django is not the event store. His prototype writes events with its own SQL, and Django supplies the database connection, transactions, migrations, and the HTTP API. The bake-off drives the command handlers directly rather than over HTTP, so the web layer is outside the measured path on both sides. The Python side starts from Andy's code as written, with Django's database layer in the path, because that is what his proposal would ship. Replacing that layer with plain psycopg is allowed as tuning, and each result records which variant ran.

The two sides are not symmetrical, and the test does not pretend otherwise. On the C# side the event store is a library the Foundation would adopt; on the Python side it is code the Foundation would own.

## Invariants both sides keep

Every measured command does all of the following. A run that skips any of them does not count.

1. It locks the aggregate's authoritative state row with `SELECT FOR UPDATE` before computing anything, as [WAX-0013 Event Chain Integrity and Tamper-Evidence](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0013-event-chain-integrity-and-tamper-evidence.md) requires.
2. It rejects the write if the aggregate's version has moved since the command loaded it, the optimistic concurrency check in [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md).
3. It computes `event_hash` and `prior_event_hash` for each event, over the fields WAX-0013 lists, inside the same transaction as the event write. In Wax this is the Wax Security Module's job; the bake-off implements the same computation in each language.
4. It writes the event and updates the authoritative state in one atomic transaction, as [WAX-0002 CQRS Model](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0002-cqrs-model.md) requires.
5. It hashes every event individually and chains it to its own aggregate's previous event. The single-command workloads write one command per transaction through the command handler, as WAX-0024 requires of batch work today. The bulk workloads write a chunk of aggregates per transaction with set-based statements, a change to WAX-0024 being drafted for designated bulk operations; their events are still hashed and chained one by one. A bulk insert that bypasses the per-event hash is not allowed.

Every side hashes the list of fields that WAX-0013 named until 2026-10-08, when it moved to a hash over the event's full payload, including `recorded_at` and the two sequence numbers, which the store assigns and the hashing code must read inside the transaction before it computes the hash. No side does that yet, so the comparison between the sides holds, but the cost of the chain is understated by whatever that read adds to each write.

## Method (draft)

[WORKLOADS.md](WORKLOADS.md) specifies exactly what runs, so that both sides do the same work: the seed, the commands added for the test, the batch jobs, the real-time mix and its rules, the command each side's runtime answers to, and the correctness gates. In outline, two kinds of workload are measured.

**Batch.** A nightly job runs one command against every account in the database, split into chunks of 1,000 that workers claim with `SELECT FOR UPDATE SKIP LOCKED`, as WAX-0024 describes. Two batch jobs are measured: a payment file, which Andy's prototype already implements, and interest accrual, which both sides gain for this test.

**Bulk.** Rob approved adding bulk workloads after the first pass showed that database CPU per command sets every side's ceiling. The same payment file, and a mass placement of new accounts, are written a chunk of 1,000 aggregates per transaction with set-based statements, beside the same work done one command per transaction. They measure how much of the ceiling is the transaction per command. [WORKLOADS.md](WORKLOADS.md) says exactly what each does.

**Real time.** Many workers at once simulate a working day. Payments, disputes, and new accounts arrive as commands, as they would from collectors, a payment portal, and inbound files. Each committed event is offered to event-triggered workflow rules, the "when X happens, do Y" rules of [WAX-0023 Workflow Automation Engine](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0023-workflow-automation-engine.md), and a rule that matches performs its action as a new command. Some actions schedule a future-dated event in the Pending Event Register of [WAX-0027 Pending Event Register](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0027-pending-event-register.md); workers claim entries as they come due, fire them, and run their rules in turn. The rule set is the same on both sides.

Each workload runs with 1, 2, 4, 8, 16, and 32 workers, so the results show how each stack scales and where it stops scaling. The first measured runs use a seed of 500,000 accounts, and whether larger sizes are needed is decided after the first side-by-side comparison. A result at 500,000 accounts shows how fast each stack runs the command path while its working set fits in memory; it does not show what happens at 200 million, where it would not. Every result records its data size. The seed is loaded by each side's own bulk loader, in its own event store format, with valid hash chains, and loading it is not measured.

Read models are updated by a separate reader that catches up after the commands commit, the design WAX-0002 describes. Both stacks already have one: Marten's asynchronous projection daemon and Andy's projection reader. The in-transaction mode that WAX-0002 also allows in v1 is not measured.

One harness drives both sides. It measures from outside the process under test, reading container CPU from the kernel's cgroup counters and statements per command from PostgreSQL's `pg_stat_statements`, so neither side reports its own numbers. Each side supplies only a small driver that runs the workload in process against its own command handler.

The benchmark machine is a laptop: an Intel Core i7-13620H with 10 cores and 16 threads, 64 GB of memory, and Windows 11 with Docker Desktop. Each result's `environment.json` records the rest, including the disks and the power plan. A separate desktop PC was considered as the benchmark machine; its setup is on hold, nothing has run on it, and [RUNBOOK-PC.md](RUNBOOK-PC.md) describes how a pass would run there. Runs happen only when the laptop is otherwise idle, one side at a time and never in parallel, against the same PostgreSQL 17 container with the same configuration. The containers' CPU limits, 6 for the database, 4 for the bench driver, and 2 for the engine, add up to 12 of the laptop's 16 logical processors, leaving 4 for Windows, the Docker VM, and the harness. A limit caps CPU time; it does not reserve cores. The laptop's 16 logical processors come from 6 performance cores with two threads each and 4 efficiency cores, so when every container is busy at once, a logical processor does less than a performance core's full work. Connection pooling is available to both: each side may use its driver's own pool, and either may put pgBouncer in front of PostgreSQL, as tuning that the result records. Each configuration gets a warm-up pass and then three measured runs, and the median is reported.

**The first side-by-side pass.** Measuring every worker count three times at 500,000 accounts would take days on the benchmark laptop, so the first pass is narrower, and its results say so. Each workload runs at 4 and at 16 workers, once each, with no separate warm-up run, because a batch over 500,000 accounts runs for many minutes and that swamps any start-up effect. The real-time workload warms up for 60 seconds and is measured for 180. Every side runs its engine with `--rule-workers 4`, the same value on each. The sides run one after another, never at once: C# through Marten, then C# with direct append, then Python. More worker counts, repeats, or larger sizes are decided after this pass.

**The second pass.** After the first pass, Rob asked for the remaining worker counts, repeats in a shuffled order, tuned engines, three diagnostics, and the bulk workloads, and the side without Marten joined. The second pass runs on the same laptop under the corrected method described with the measurements below, one measured run per configuration at 500,000 accounts, and is repeated the following night in a different order. Its runs fall into five groups, in priority order:

1. The core comparison. On all four sides, both batch jobs run at 8 and 32 workers and the real-time mix at 4, 8, 16, and 32. The side without Marten also runs both batch jobs at 4 and 16, to match the first pass.
2. Three diagnostics, on the payment file at 16 workers on every side. With the engine paused, nothing reads the stream while the commands run, which shows what the readers cost the write path; the read-model and rule gates are skipped. With hashing off, every event carries a placeholder hash, the stream's head is not read, and the version check trusts the locked state row, with the unique index on stream and version as the backstop; that shows what the chain costs, and the chain gate is skipped. Neither diagnostic is a contestant. The third diagnostic, the statements that took the most database time, is recorded for every run and needs no runs of its own.
3. The bulk workloads, at 4 and 16 workers: the bulk payment file over all 500,000 accounts, and a placement of 100,000 new accounts, one command per transaction and in bulk. They run on the direct-append side, the side without Marten, and Python. The side through Marten does not run them, because Marten has no set-based append inside a caller's transaction, so it would run the direct-append side's writer again.
4. The real-time mix at 4 and 16 workers with every engine untuned, at the first pass's `--rule-workers 4`, to show what tuning changed under the corrected method. This group waits for Rob's approval.
5. The first pass's batch runs on its three sides, at 4 and 16 workers, for a second sample if the window leaves time.

The first pass stays the baseline on the same machine. Its batch figures, catch-up included, compare directly with the second pass's, with the engines' tuning as the one difference. Its real-time throughput, latency, and rule delays compare as well. Its real-time CPU per command and real-time catch-up times do not, because they were measured before the two corrections.

Within each group the order is shuffled from a recorded seed, so that a side's place in the pass is not mixed with its result. The [passes](passes/) folder holds the generator, the seed, and the run list, committed before the pass started. A run that would not finish before the window ends is skipped, so the lowest group drops first. Before each run every bake-off database container is stopped, so each run starts with only its own side's database up, freshly started. A run's read models may take up to an hour to catch up before the run fails. The containers of an unrelated stack that ran during the first pass are stopped for this one.

Each engine runs with the settings its tuning trial chose, which the plan file records. The trials ran on the same laptop at 50,000 accounts and are not recorded. Marten's daemon runs each projection as its own agent, so the two sides through Marten have no projection-worker setting. On the other two sides, one reader advances all three projections in turn, as Andy's prototype does, and `--projection-workers` sets how many workers share that reading.

**The third pass.** The second pass showed two things that change how the next one runs. The laptop's speed drifted during the night, and Python's driver was at its 4-CPU limit at 16 and 32 workers, so the second pass's diagnostics at 16 workers did not isolate the engine or the hashing on every side. The third pass runs the same tuned engines on the same laptop, with each run's seed copy, gates, and measurements unchanged, and it adds two records to every run: a host-speed probe, the median time of five sums of 20,000,000 integers in the run's database, taken just before the bench and outside the measured window, and each container's CPU-throttle counters from its cgroup. A run on a slowed host then shows in its own result. Its runs fall into four groups, in priority order:

1. Every run the second pass skipped for time: the first pass's batch runs at 4 and 16 workers on its three sides, which complete the tuned results at every worker count; Python's placement and bulk placement at 16 workers; and the remaining untuned real-time runs.
2. The diagnostics again, on the payment file at 8 workers, below Python's limit, as triples run back to back on each side: a normal run, the engine paused, and hashing off, in a shuffled order within each triple, so the host's drift between them is small.
3. The core comparison again, in a new shuffle, for a second sample of every configuration.
4. A second round of the triples, if the window leaves time.

The [plan](passes/third-pass.plan.md) is generated with the seed 20261010 and committed before the pass starts. The runner now judges whether a run fits before the deadline from the second pass's actual run times plus 15 percent, instead of from rough estimates.

The measurements are these:

- Sustained commands per second over the measured run.
- Application CPU time and database CPU time per thousand commands.
- Command latency at the 50th, 95th, and 99th percentiles.
- In the real-time workload, the time from an event committing to its rule's action committing, and from a scheduled event coming due to its firing.
- How far the read models fall behind, and how long they take to catch up once the bench command ends: the time until every projection and the rule runner have reached the head as it stood at that moment. Events written afterward, such as scheduled reviews still firing, do not count.
- CPU per command is each container's CPU over the whole bench command, divided by every command sent in it, the warm-up's included. Statements and commits per command are divided the same way.

After every run, the correctness gates in WORKLOADS.md are checked. In short: every command was applied exactly once, every hash chain verifies, and once the reader has caught up, every read model agrees with the authoritative state.

## Rules

1. The method is committed before any result is recorded, so the history shows it came first. A later change to the method is committed with its reason, and earlier results are rerun under it.
2. Any tuning is allowed on either side if it keeps every invariant and is committed here.
3. Database tuning applies to both sides or to neither.
4. Every result records the machine, the operating system, the PostgreSQL version and settings, the data size, the commit of each side, and the raw harness output, unedited, in [results](results/README.md).
5. A run that fails a correctness gate is recorded as failed and does not count.
6. If both stacks clear the pass threshold, throughput does not decide the question, and the decision rests on the other grounds in WAX-0025 and the proposal.

## Results

### First side-by-side pass, 2026-10-08 and 2026-10-09

The pass ran overnight from 23:46 to 06:17 under the scope above: 500,000 accounts, 4 and 16 workers, one run of each configuration, `--rule-workers 4` on every side, on the benchmark laptop with no other work running, though 13 idle containers from an unrelated stack were up. All 18 runs passed every correctness gate. Each run's full record, with its raw output, is in [results](results/README.md); the folder names begin `2026-10-08-first-pass` or `2026-10-09-first-pass`.

Throughput, in commands per second over the measured run:

| Workload | Workers | C# through Marten | C# with direct append | Python |
| --- | --- | --- | --- | --- |
| Payment file | 4 | 425 | 321 | 205 |
| Payment file | 16 | 841 | 975 | 310 |
| Interest accrual | 4 | 334 | 337 | 264 |
| Interest accrual | 16 | 1,168 | 1,287 | 489 |
| Real time, inbound | 4 | 297 | 302 | 194 |
| Real time, inbound | 16 | 651 | 755 | 292 |

CPU per command at 16 workers, in milliseconds, from each container's cgroup counters. "Application" is the driver and the engine together:

| Workload | C# through Marten, application / database | C# with direct append | Python |
| --- | --- | --- | --- |
| Payment file | 1.7 / 6.8 | 1.3 / 5.7 | 15.7 / 9.5 |
| Interest accrual | 1.1 / 4.4 | 0.9 / 3.8 | 9.7 / 5.7 |
| Real time, read about a third high (see the limits below) | 3.5 / 12.1 | 2.9 / 10.4 | 26.6 / 14.8 |

What the pass shows:

- **The C# sides moved two to three times as many commands at 16 workers, with the same 4 CPUs for the driver.** Python's driver was held at its CPU limit at 16 workers, so its throughput is where its CPU per command put it.
- **Python's application spent about nine times as much CPU per command, and its database about 1.4 times as much.** The Python side ran about 27 statements per payment against 15 for the C# side, because the prototype's code reads and writes one statement at a time.
- **Read models.** After the payment file, the C# read models were caught up within two seconds; Python's took 31 minutes after the 4-worker run and 46 minutes after the 16-worker run, because one projection worker on 2 CPUs could not keep pace. Its engine was not tuned for this pass, and a second projection worker is allowed tuning.
- **Rules under load.** At 4 workers the rule delay was about 0.24 seconds on both C# sides and 0.70 seconds on Python. At 16 workers the C# rule runner fell behind, with a median delay of about 14 seconds, while Python's held near 1 second, but at less than half the inbound rate. Neither side's rule runner was tuned beyond four workers.
- **Marten's append.** At 16 workers the direct append was faster than Marten's by 16 percent on the payment file, 10 percent on the accrual, and 16 percent on the real-time mix, with less database CPU each time. At 4 workers it was slower on the payment file, 321 against 425, with about the same database CPU; one run cannot say whether that is real or noise.
- **The database sets the ceiling.** Database CPU per command, 3.8 to 6.8 ms on the C# sides and 5.7 to 9.5 ms on Python, is what limits every side: a 6-core database caps the C# sides near 900 to 1,600 commands per second. At the same cost per command, 6,900 per second on one primary would take roughly 26 to 47 cores of comparable CPU for the C# sides, and 39 to 66 for Python as written. That is a question about the design's one-transaction-per-command write path more than about the language; the language mostly decides how many application servers the same load needs.
- **The threshold.** No side came near 6,900 commands per second on this laptop. The best was 1,287, the direct append's accrual at 16 workers. The laptop runs say how the stacks compare, not whether either clears the threshold on server hardware.

Limits of this pass: the catch-up time that each real-time run's record reports is not comparable across sides, because dispute reviews scheduled near the end of a run keep firing for up to 60 seconds afterward and keep moving the head; batch catch-up times are unaffected. The real-time CPU figures include the warm-up's CPU but are divided only by the commands of the measured window, so they read about a third high, by the ratio of 240 seconds to 180; the ratios between the sides hold, and the batch figures and the database ceiling, which use batch runs, are unaffected. One run per configuration, so no spread is known; a laptop under Docker Desktop, with 13 unrelated containers running and possibly slowing itself for heat over a seven-hour run; the sides ran in order, so a side's place in the night is mixed with its result; and neither engine was tuned. Repeats, the remaining worker counts, and tuned engines are the next decisions. After this pass, Rob approved two corrections to the method that fix the flaws above: catch-up now ends at the head as it stood when the bench command ended, and CPU per command now divides by every command sent, the warm-up's included. The real-time runs are repeated under the corrected method in the next pass.

### Second pass, 2026-10-09 and 2026-10-10

The pass ran overnight from 21:07 to 09:31 on the benchmark laptop, under the plan committed before it in [passes](passes/second-pass.plan.md), with the engines tuned as the method describes and the containers of the unrelated stack stopped. It completed 61 of the plan's 82 runs, and every one passed every gate that applied to it. All of group 1, the core comparison, and all of group 2, the diagnostics, ran. Group 3, the bulk workloads, ran 16 of its 18 runs; Python's placement and bulk placement at 16 workers did not fit before the deadline. Group 4 ran 1 of its 8 untuned runs, and group 5 none. The skipped runs carry over to the next pass. Each run's record is in [results](results/README.md), in the folders whose names contain `second-pass`.

Throughput, in commands per second, one run each. A figure marked * comes from the first pass, with every engine untuned at `--rule-workers 4`; the second pass did not run those configurations:

| Workload | Workers | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- | --- |
| Payment file | 4 | 425* | 321* | 415 | 205* |
| Payment file | 8 | 679 | 729 | 804 | 427 |
| Payment file | 16 | 841* | 975* | 992 | 310* |
| Payment file | 32 | 622 | 691 | 701 | 276 |
| Interest accrual | 4 | 334* | 337* | 410 | 264* |
| Interest accrual | 8 | 835 | 787 | 834 | 556 |
| Interest accrual | 16 | 1,168* | 1,287* | 1,547 | 489* |
| Interest accrual | 32 | 919 | 1,044 | 1,054 | 469 |
| Real time, inbound | 4 | 461 | 395 | 512 | 237 |
| Real time, inbound | 8 | 647 | 656 | 1,020 | 407 |
| Real time, inbound | 16 | 559 | 642 | 646 | 330 |
| Real time, inbound | 32 | 487 | 598 | 628 | 268 |

CPU per command at 8 workers, in milliseconds, application (the driver and the engine together) / database. At 8 workers no side's driver is at its CPU limit:

| Workload | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| Payment file | 1.7 / 5.2 | 1.5 / 4.7 | 1.9 / 4.6 | 12.8 / 7.4 |
| Interest accrual | 1.3 / 3.7 | 1.2 / 3.7 | 1.6 / 3.9 | 6.9 / 4.1 |
| Real time | 2.2 / 6.9 | 2.0 / 6.4 | 2.0 / 4.7 | 13.1 / 7.7 |

The real-time rule delay, from an event committing to its rule's action committing, in seconds, 50th / 99th percentile:

| Workers | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| 4 | 0.23 / 1.01 | 0.24 / 1.03 | 0.23 / 0.52 | 1.29 / 5.53 |
| 8 | 0.22 / 1.02 | 0.23 / 1.03 | 0.22 / 0.52 | 2.87 / 5.21 |
| 16 | 0.29 / 1.16 | 0.30 / 1.11 | 0.33 / 0.69 | 6.57 / 10.70 |
| 32 | 0.42 / 1.40 | 0.41 / 1.41 | 0.80 / 1.83 | 1.98 / 3.12 |

The bulk workloads, in aggregates per second, with the time the read models and rules took to catch up afterward. A bulk run's command is a chunk of 1,000 aggregates, so its rate is counted in aggregates to compare with one command per aggregate:

| Workload | Workers | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| Bulk payment file | 4 | 13,071 (79 s) | 16,198 (94 s) | 11,791 (1,704 s) |
| Bulk payment file | 16 | 8,576 (99 s) | 11,694 (111 s) | 12,051 (1,657 s) |
| Placement, one command each | 4 | 301 (3 s) | 370 (3 s) | 262 (1,319 s) |
| Placement, one command each | 16 | 471 (62 s) | 503 (57 s) | skipped |
| Bulk placement | 4 | 4,790 (130 s) | 7,481 (133 s) | 15,640 (1,731 s) |
| Bulk placement | 16 | 12,077 (134 s) | 8,831 (144 s) | skipped |

What the pass shows:

- **Past 16 workers every side slowed.** At 32 workers every side's payment file ran slower than at 8, and real time peaked at 8 workers on every side. The C# sides' database CPU per payment rose from about 5 ms at 8 workers to about 9 ms at 32. On this laptop the database's 6 CPUs set the ceiling, and contention at high worker counts adds cost on top.
- **The C# side without Marten led or tied in most rows,** with the lowest or nearly the lowest database CPU per command. One figure is out of line with its neighbors: its real-time inbound rate at 8 workers, 1,020 per second against 512 at 4 and 646 at 16. One run cannot say whether that is real.
- **Python's application spent 4 to 9 times as much CPU per command as the C# sides at 8 workers,** and its database 1.1 to 1.6 times as much. At 16 and 32 workers Python's driver is held at its 4-CPU limit, so its throughput there is 4 CPUs divided by its CPU per command: a measure of Python's cost per command, not of its engine or its database.
- **Tuning worked for read models.** After the batch jobs, every C# side's read models were caught up within 3 to 6 seconds after the bench ended, except 26 seconds for the side without Marten after accrual at 32 workers. Python's took 3 to 13 minutes, down from 31 to 46 minutes in the first pass, with three projection workers.
- **Rules under load.** Every C# side's rule runner kept up at every worker count, with a 99th-percentile delay of 1.8 seconds or less. Python's kept up in its 50,000-account trial, but at 500,000 accounts its 99th-percentile delay was 5.2 to 10.7 seconds at 4, 8, and 16 workers, and 3.1 seconds at 32.
- **Bulk writes are an order of magnitude faster.** One transaction per chunk of 1,000 aggregates moved 8,600 to 16,200 aggregates per second through the payment file, against 280 to 990 commands per second one at a time, with every event still hashed and chained to its own aggregate. Placement in bulk ran 16 to 60 times faster than one command per account. Python's bulk writes kept pace with the C# sides', but its read models and rules then took about 28 minutes to catch up, against 1.3 to 2.4 minutes on the C# sides.

The diagnostics ran the payment file at 16 workers on every side. With the engine paused, the C# sides ran 1,014 (through Marten), 1,114 (direct append), and 1,301 (without Marten) commands per second; with hashing off, 1,071, 1,190, and 1,172. Only the side without Marten has a normal run at 16 workers in this pass, at 992, so against it, pausing the engine was worth about 31 percent and turning hashing off about 18 percent. Python's diagnostics at 16 workers do not isolate anything, because its driver was at its CPU limit and the host ran slow during its engine-paused run, as the limits below describe. The next pass repeats the diagnostics at 8 workers, below Python's limit, with each side's normal, engine-paused, and hashing-off runs back to back.

Limits of this pass: one run per configuration, so no spread is known, and the host's speed was not constant. In Python's engine-paused run, every statement's average time in PostgreSQL, a trivial in-memory lookup included, ran about 1.5 times as long as in other runs, and Python's payment file at 32 workers showed the same; nothing in either run's code explains it, so the laptop itself slowed, most likely from clock speed, heat, or Docker's processors landing on the efficiency cores. Differences of 10 or 20 percent between single runs taken at different times of the night are therefore within the noise until the repeats arrive, and the percentages above are indications, not measurements of an effect. From the next pass, each run also records a host-speed probe and each container's CPU-throttle counters, so a slow host shows in the result. The engines' settings differ by side, because each side was tuned to its own runtime, and the first pass's figures marked * used the same untuned setting on every side. A light scheduled job that does not use Docker ran at 08:07 during Python's bulk payment file at 4 workers.

### Third pass, group 1 only, 2026-10-10

At Rob's instruction the third pass ran only its group 1, the 21 runs the second pass skipped for time, on the benchmark laptop from 10:32 to 16:42 under the plan in [passes](passes/third-pass.plan.md). All 21 passed every gate that applied to them. Groups 2 to 4, the diagnostic triples at 8 workers, the core repeat, and the second round of triples, did not run and wait for Rob's go. The host-speed probe stayed between 716 and 795 ms all day, so the laptop showed none of the slowdown seen in the second pass. Each run's record is in the folders whose names contain `third-pass`.

With these runs every side has tuned batch results at every worker count. Commands per second, one run each; the 8- and 32-worker figures, and every figure for the C# side without Marten, come from the second pass:

| Workload | Workers | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- | --- |
| Payment file | 4 | 406 | 386 | 415 | 239 |
| Payment file | 8 | 679 | 729 | 804 | 427 |
| Payment file | 16 | 815 | 932 | 992 | 372 |
| Payment file | 32 | 622 | 691 | 701 | 276 |
| Interest accrual | 4 | 398 | 307 | 410 | 366 |
| Interest accrual | 8 | 835 | 787 | 834 | 556 |
| Interest accrual | 16 | 1,046 | 1,416 | 1,547 | 569 |
| Interest accrual | 32 | 919 | 1,044 | 1,054 | 469 |

The untuned real-time runs repeat the first pass's engine setting, `--rule-workers 4` on every side, under the corrected method. Inbound commands per second, with the rule delay's 99th percentile in seconds, against the tuned runs of the second pass:

| Workers | Engines | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- | --- |
| 4 | Untuned | 275 (1.0 s) | 443 (1.0 s) | 382 (0.5 s) | 190 (1.1 s) |
| 4 | Tuned | 461 (1.0 s) | 395 (1.0 s) | 512 (0.5 s) | 237 (5.5 s) |
| 16 | Untuned | 569 (26.2 s) | 631 (25.8 s) | 1,096 (28.8 s) | 348 (1.6 s) |
| 16 | Tuned | 559 (1.2 s) | 642 (1.1 s) | 646 (0.7 s) | 330 (10.7 s) |

Python placed 100,000 new accounts at 16 workers at 432 per second one command each and 11,391 per second in bulk, with its read models and rules catching up in about 25 and 29 minutes.

What these runs add:

- **Single runs vary more than the differences between sides.** The untuned real-time runs repeat configurations the first pass ran, and the same configuration moved by -16 to +47 percent between passes: the side through Marten went from 297 to 275 at 4 workers and from 651 to 569 at 16, the direct append from 302 to 443 at 4 and from 755 to 631 at 16, and Python from 194 to 190 at 4 and from 292 to 348 at 16. The order of the C# sides at one worker count is therefore not settled by one run, and margins between them should not be quoted until repeats exist.
- **Batch work peaks at 16 workers on the C# sides.** Python's payment file peaks at 8, where its driver is still below its CPU limit.
- **Tuning the rule runner mattered at 16 workers.** With four rule workers, every C# side's rules fell 25 to 29 seconds behind at the 99th percentile, so part of the rule work landed after the measured window. On the side without Marten that freed enough database time to lift its inbound rate to 1,096 per second; on the other two C# sides the inbound rate barely moved. Tuned, all three kept up within 1.2 seconds, so the untuned 16-worker figures are not comparable with the tuned ones. At 4 workers every side's rules kept up under both settings, and the inbound differences there are within the run-to-run spread above. Python went the other way: four rule workers kept up at 16 workers, with a 99th-percentile delay of 1.6 seconds, where its tuned eight lagged at 10.7. Each Python rule worker is its own process, so at 500,000 accounts more of them crowd the engine's 2 CPUs; its tuning trial at 50,000 accounts did not show this, and its setting should be revisited.
- **Past about 8 concurrent workers, every statement costs more.** In the committed statement records, the event insert of the side without Marten took 0.26, 0.38, 0.73, and 1.58 ms as the number of active sessions grew, and Marten's append function went from 0.90 ms to about 1.8 ms. Every side shares hot spots that would explain this: one event sequence, the rightmost leaf of the index on it, and a few small, often-updated tables. The 16- and 32-worker results therefore measure contention as much as language or library.
- **The C# side without Marten spends the least database CPU per command in most rows,** 4.7 ms per real-time command at 8 workers against 6.9 through Marten and 6.4 with the direct append. Statement times explain the gap between Marten's append and the direct append, but most of the gap between the direct append and the side without Marten lies outside statement execution, which accounts for only about a quarter of database CPU on every side. One hypothesis is the cost of updating Marten's stream table on every command, through dead tuples, vacuum, and full-page writes after each checkpoint; the harness records no WAL or checkpoint statistics yet, so it is unconfirmed.

## Attribution

The Python side is adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Parts the bake-off does not measure, such as the blue-green release drill, remain in his fork. The [python README](python/README.md) lists what was kept, what was changed, and what was left out.
