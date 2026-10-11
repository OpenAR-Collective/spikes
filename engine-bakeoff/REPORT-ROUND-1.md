# Engine bake-off, round 1

Report of The Open Accounts Receivable Collective Foundation, operating as The OpenAR Collective. The measurements were taken on one laptop from 2026-10-08 to 2026-10-10.

## Summary

Wax has to move a very large amount of work through one write path. Every command locks an aggregate, checks its version, hashes its event and chains it to the aggregate's previous event, and writes the event and the aggregate's state in one transaction. We built that path four ways, ran the same workloads against all four on the same machine with the same PostgreSQL settings, and measured each from outside. This report says what we found, how firm each finding is, and what the Foundation will do next. The repository holds everything needed to check or challenge it.

The four sides:

- **C# through Marten**, the stack that [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md) adopted.
- **C# with direct append**: the same code, appending events to Marten's own tables with plain SQL.
- **C# without Marten**: events written to the `core.event` table that [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md) specifies, with a reader ported from Andy Troiano's prototype.
- **Python**, adapted from the HiveAR prototype that Andy Troiano published with his proposal to build Wax in Python on Django.

We made 100 measured runs in three passes. Every run passed every correctness gate that applied to it: every command was applied exactly once, every hash chain verified, and every read model agreed with the authoritative state once its reader had caught up.

What the measurements show:

1. **PostgreSQL is the ceiling for every side.** At 8 workers the C# sides used 3.7 to 6.9 ms of database CPU per command. Six database CPUs divided by that cost gives a ceiling of roughly 900 to 1,600 commands per second, and the fastest single-command run of any C# side reached 1,547. If the cost per command held, 6,900 commands per second would take roughly 26 to 47 database cores.
2. **Bulk chunk writes are the largest lever by far.** Writing a chunk of 1,000 aggregates in one transaction, with every event still hashed and chained individually, moved the payment file through at 8,576 to 16,198 aggregates per second on the three sides that ran it, against 239 to 992 commands per second with one transaction per command.
3. **Python as written in the prototype cost more per command.** At 8 workers the C# sides moved 1.4 to 2.5 times as many commands per second, and Python's application used 4 to 9 times the CPU per command. Nobody tried to speed up Python's write path, so this describes the prototype's code and not what Python can do.
4. **Marten's append added database cost.** Its append function took 3.2 to 4.8 times as long as a plain insert into the same table in all 15 pairs of runs. Of the ten same-pass comparisons among the three C# sides (defined below), Marten used the most database CPU per command in eight and the least in none. The side without Marten passed every gate in all 22 of its runs.
5. **Chaining each event to the previous one is a modest cost.** On the payment file at 16 workers, turning hashing off, which skips the read of each aggregate's stream head as well as the hash computation, cut the database CPU per command by 18 percent on the side without Marten. That comes from one pair of runs, so we treat it as an indication.
6. **The order of the three C# sides by throughput is not established.** The same configuration moved by -16 to +47 percent between passes, so no margin between the C# sides in this report should be quoted as a measurement.

## The Foundation's path

These results settle the direction of three open questions, and the Foundation states that direction here. Decisions change only by pull request, so until the amendments merge, the decisions as written stand.

- **Wax will not use Marten as its event store.** Wax will own a `core.event` table built to WAX-0001 and append to it with plain SQL, with a projection reader derived from Andy Troiano's design. The side without Marten shows that this works: its event store, reader, and rule runner came to about 1,200 lines of C# and a 190-line SQL schema. [What dropping Marten costs](#what-dropping-marten-costs) says what Wax then owns. Amendments to the decisions that name Marten will follow as pull requests.
- **Batch work will use bulk chunk writes.** A batch job type designated as a bulk operation will write each chunk of aggregates in one transaction through a framework that Wax ships. The framework runs the same handler logic for each item, produces the same events, hashes and chains every event individually, and falls back to one command per transaction when a chunk fails. The draft change to [WAX-0024 High Availability, Disaster Recovery, and Distributed Workloads](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0024-high-availability-disaster-recovery-and-distributed-workloads.md) is [pull request 26 of platform-design](https://github.com/OpenAR-Collective/platform-design/pull/26).
- **The language decision in WAX-0025 stands.** This report measures one thing, throughput on the write path, and WAX-0025 weighed more. It also named the Marten library as its most significant technical factor, so the amendment that removes Marten has to restate the case for C# on the grounds that remain. On these measurements Python as written gives the Foundation no throughput reason to switch, and throughput does not rule Python out either: the database sets the ceiling for every side, and most of Python's extra cost is application CPU, which scales out with more servers. The limits below apply, above all that nobody tuned Python's write path. The Foundation will answer Andy Troiano's proposal on its pull request, and this report is evidence for that answer, not the answer.

## Why we ran it

[WAX-0025](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md) chose C# on .NET with Marten as the event store in August 2026. On 2026-10-08 Andy Troiano proposed replacing that decision with Python on Django, in [atroiano/platform-design#1](https://github.com/atroiano/platform-design/pull/1), and backed the proposal with a working prototype. Most of what separates the two stacks is a judgment call for the Foundation. One question can be measured instead of argued: whether each stack can move work through the Wax write path fast enough. The bake-off exists because of that proposal, and the Python side exists because of his prototype.

Throughput is the question because [WAX-0024](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0024-high-availability-disaster-recovery-and-distributed-workloads.md) requires batch jobs to run through the normal command handler path, spread across instances and threads, so every command in a batch does the full work of a single write. The largest collection database seen in practice holds 200 million accounts, and the heaviest nightly job touches every one of them. One pass over 200 million accounts in an eight-hour window is about 6,900 commands per second, sustained, and a six-hour window is about 9,300. We use 6,900 as a reference point for reading the results. It is not a pass line.

## What we compared

*Each row is one side, with its language and runtime versions, its event store, and what runs its read models and rules.*

| Side | Language and runtime | Event store | Read models and rules |
| --- | --- | --- | --- |
| C# through Marten | C# on .NET 10.0.12, Npgsql 9.0.4 | Marten 9.47.0 on PostgreSQL | Marten's asynchronous projection daemon and subscription |
| C# with direct append | The same code and image | Marten's tables, appended with plain SQL | Marten's daemon and subscription |
| C# without Marten | C# on .NET 10.0.12, Npgsql 9.0.4 | `core.event` as WAX-0001 specifies | A reader ported from Andy Troiano's prototype, and a partitioned rule runner |
| Python | Python 3.13.16, Django 5.2.18, psycopg 3.3.6 | Andy Troiano's event store, written directly against PostgreSQL | Andy Troiano's projection reader, and a Foundation-written partitioned rule runner |

All four ran against PostgreSQL 17.11 in the same image with the same settings. The Python side starts from Andy Troiano's prototype, published at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) under the Apache License 2.0. The [python README](python/README.md) lists what was kept unchanged, what was changed, and what the Foundation wrote, and [NOTICE](../NOTICE) credits him.

The Foundation built and tuned every side to the best of its ability, but the sides are not equal, and these differences matter to how the results read:

- **Nobody tried to speed up Python's single-command write path.** It runs Andy Troiano's event store and his four command handlers as written, apart from a small change to placement, and Foundation-written handlers in the same style for the six commands the workloads added. State rows go through Django's object layer and events through the prototype's own SQL, and each query is sent as it goes, with a reason code looked up for every event. The C# sides batch their reads and writes and cache the reason-code registry. All of that is allowed tuning under the bake-off's rules, and none of it was tried on Python.
- **The Foundation wrote much of the Python engine.** The prototype supplies the command dispatch, the event store, the hashing, the projection reader and its read models, the allocation and waterfall, and the four command handlers. The Foundation wrote the six commands the workloads and rules added, the rule runner, the register poller, the batch coordinator, the bulk seed loader and chunk writer, the engine's process setup, and the bench drivers. The Python findings about rule delay and start-up describe that Foundation-written engine on Andy Troiano's reader, not his prototype.
- **The C# port of his reader is not like-for-like.** The port reads only the events of the aggregate types a read model covers. His reader reads every event for every read model and passes over the ones it does not cover, which with these three read models is two to three times the reading, depending on the mix: about twice on the payment workloads and three times on the interest accrual. It also loads each event through Django's object layer. Python's catch-up times partly reflect those differences, and the Python reader was not given the same change.
- **The two kinds of C# rule runner differ.** Marten's subscription hands the runner a page at a time and waits for the page's slowest aggregate. The side without Marten uses a partitioned runner in which each partition moves on its own, so a difference in rule latency between them reflects both changes.
- **A library on one side and owned code on the others.** On the Marten sides the event store is a library the Foundation would adopt. On the others it is code the Foundation would own.

The bake-off drives command handlers directly rather than over HTTP, so the web layer is outside the measured path on every side, Django's HTTP layer included. Django's database layer is inside it on the Python side.

## How we measured

[WORKLOADS.md](WORKLOADS.md) specifies exactly what runs, so that every side does the same work. There are three kinds of workload.

- **Batch.** A nightly job runs one command against every one of 500,000 accounts, in chunks of 1,000 that workers claim with `SELECT FOR UPDATE SKIP LOCKED`, as WAX-0024 describes. Two jobs were measured: a payment file and an interest accrual.
- **Real time.** Many workers at once simulate a working day. Payments, disputes, and new accounts arrive as commands, and each committed event is offered to event-triggered workflow rules, as in [WAX-0023 Workflow Automation Engine](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0023-workflow-automation-engine.md). A rule that matches performs its action as a new command, and some actions schedule a future event in the Pending Event Register of [WAX-0027](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0027-pending-event-register.md). The mix warms up for 60 seconds and is measured for 180.
- **Bulk.** The same payment file over all 500,000 accounts, and a placement of 100,000 new accounts, written a chunk of 1,000 aggregates per transaction with set-based statements, beside the same placement done one command per transaction.

Every measured command keeps Wax's invariants. It locks the aggregate's state row with `SELECT FOR UPDATE`, rejects the write if the aggregate's version has moved, computes the event's hash and links it to the previous event inside the same transaction, and writes the event and the state atomically. The bulk workloads hash and chain every event individually, and a bulk insert that bypasses the per-event hash is not allowed.

One harness drives every side. It measures CPU from each container's kernel cgroup counters, statements per command from PostgreSQL's `pg_stat_statements`, and catch-up and rule delay from the database, all from outside the process under test. Throughput and command latency come from each side's bench driver, which counts what it sent and what succeeded. After every run, the correctness gates in WORKLOADS.md are checked against the database, and they check the driver's counts too.

The benchmark machine is a laptop with an Intel Core i7-13620H (6 performance cores with two threads each and 4 efficiency cores), 64 GB of memory, and Windows 11 with Docker Desktop (Docker 29.7.2). The containers' CPU limits are the same for every side: 6 CPUs and 12 GB for the database, 4 CPUs and 4 GB for the bench driver, and 2 CPUs and 2 GB for the engine. Runs happen one side at a time, each from a fresh copy of that side's seed of 500,000 accounts, loaded by the side's own bulk loader in its own event store format with valid hash chains. From the second pass on, every run also started with only its own side's database up, freshly started. The method was committed before any result was recorded, and so was each pass's scope, so the history shows that the method came first.

*Each row is one pass, with its dates, the number of measured runs it made, and its scope.*

| Pass | When | Runs | Scope |
| --- | --- | --- | --- |
| First | 2026-10-08 23:46 to 2026-10-09 06:17 | 18 | Three sides, engines untuned, the payment file, the interest accrual, and real time at 4 and 16 workers |
| Second | 2026-10-09 21:07 to 2026-10-10 09:31 | 61 of 82 planned | Four sides, engines tuned, 4 to 32 workers, two diagnostics, and the bulk workloads |
| Third | 2026-10-10 10:32 to 16:42 | 21 | The runs the second pass skipped for time, with a host-speed probe and CPU-throttle counters recorded for every run |

The second and third passes each have a plan in [passes](passes/), committed before the pass started, and a log and a status file after it. The first pass's scope was committed in the README's method before it ran, and it has no plan, log, or status file. [Appendix B](#appendix-b-notes-on-each-pass) gives what is particular to each pass.

## Results

The tables below consolidate the runs, and [Appendix A](#appendix-a-every-run) lists every run with a link to its record. Every figure is from a single run.

### One command per transaction

Throughput is the commands that succeeded, divided by the length of the measured run. The figures for the batch workloads at 4 and 16 workers on the three sides other than the side without Marten come from the third pass, and every other figure comes from the second.

*Each cell is throughput in commands per second, with the engines tuned, at the stated number of concurrent bench workers.*

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
| Real time, inbound | 4 | 461 | 395 | 512 | 237 |
| Real time, inbound | 8 | 647 | 656 | 1,020 | 407 |
| Real time, inbound | 16 | 559 | 642 | 646 | 330 |
| Real time, inbound | 32 | 487 | 598 | 628 | 268 |

The application is the bench driver and the engine together. At 8 workers no side's driver was at its CPU limit.

*Each cell is CPU time per command in milliseconds at 8 workers, written as application / database.*

| Workload | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| Payment file | 1.7 / 5.1 | 1.5 / 4.7 | 1.9 / 4.6 | 12.8 / 7.4 |
| Interest accrual | 1.3 / 3.7 | 1.2 / 3.7 | 1.6 / 3.9 | 6.9 / 4.1 |
| Real time | 2.2 / 6.9 | 2.0 / 6.4 | 2.0 / 4.7 | 13.1 / 7.7 |

The rule delay is the time from an event committing to its rule's action committing.

*Each cell is the real-time rule delay in seconds, written as median / 99th percentile, with the engines tuned.*

| Workers | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| 4 | 0.23 / 1.01 | 0.24 / 1.03 | 0.23 / 0.52 | 1.29 / 5.53 |
| 8 | 0.22 / 1.02 | 0.23 / 1.03 | 0.22 / 0.52 | 2.87 / 5.21 |
| 16 | 0.29 / 1.16 | 0.30 / 1.11 | 0.33 / 0.69 | 6.57 / 10.70 |
| 32 | 0.42 / 1.40 | 0.41 / 1.41 | 0.80 / 1.83 | 1.98 / 3.12 |

### Bulk chunk writes

A bulk run's command is a chunk of 1,000 aggregates, so its rate is counted in aggregates to compare with one command per aggregate. Python's placement rows at 16 workers come from the third pass. The side through Marten did not run these: the Foundation found no set-based way to append events through Marten inside a caller's transaction, so it would have run the direct-append side's writer again. Anyone who knows a way is welcome to say so.

Catch-up is the time from the end of the bench command until every read model and the rule runner had reached the head as it stood at that moment.

*Each cell is aggregates written per second, with the seconds the read models and rules took to catch up in parentheses.*

| Workload | Workers | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- |
| Bulk payment file | 4 | 13,071 (79 s) | 16,198 (94 s) | 11,791 (1,704 s) |
| Bulk payment file | 16 | 8,576 (99 s) | 11,694 (111 s) | 12,051 (1,657 s) |
| Placement, one command each | 4 | 301 (3 s) | 370 (3 s) | 262 (1,319 s) |
| Placement, one command each | 16 | 471 (62 s) | 503 (57 s) | 432 (1,483 s) |
| Bulk placement | 4 | 4,790 (130 s) | 7,481 (133 s) | 15,640 (1,731 s) |
| Bulk placement | 16 | 12,077 (134 s) | 8,831 (144 s) | 11,391 (1,724 s) |

### Where the time goes

The payment file at 16 workers on each C# side, three ways: a normal run, a run with the engine paused so that nothing reads the stream while the commands run, and a run with hashing off, in which every event carries a placeholder hash and the stream head is not read. Neither diagnostic is a contestant. Only the side without Marten has its normal run in the same pass as its diagnostics; the other normal runs were taken in the third pass, hours later, and are marked with an asterisk. Python is left out because its driver was at its CPU limit at 16 workers and the host ran slow during its engine-paused run, so its diagnostics isolate nothing.

*Each cell is commands per second, with database CPU per command in milliseconds in parentheses.*

| Side | Normal | Engine paused | Hashing off |
| --- | --- | --- | --- |
| C# through Marten | 815 (6.9 ms)* | 1,014 (5.6 ms) | 1,071 (5.3 ms) |
| C# with direct append | 932 (6.2 ms)* | 1,114 (5.0 ms) | 1,190 (4.6 ms) |
| C# without Marten | 992 (5.9 ms) | 1,301 (4.0 ms) | 1,172 (4.9 ms) |

On the side without Marten, pausing the engine raised throughput by 31 percent and cut database CPU per command by a third, from 5.9 to 4.0 ms, which shows the database cost of keeping the read models and rules current while the commands run. With the engine paused that work is deferred, not avoided. Turning hashing off raised throughput by 18 percent and cut database CPU per command by 18 percent, from 5.9 to 4.9 ms. It removes the hash computation, which runs in the application, and the read of each aggregate's stream head, where the previous event's hash comes from, so the database saving is that read. The other two C# sides moved in the same direction.

### Tuning the rule runner

The first pass ran every engine with four rule workers and one projection worker. The tuned runs of the second pass use 16 rule workers on the C# sides and 8 on Python, and three projection workers on Python and the side without Marten.

*Each cell is real-time inbound commands per second, with the rule delay's 99th percentile in seconds in parentheses.*

| Workers | Engines | C# through Marten | C# with direct append | C# without Marten | Python |
| --- | --- | --- | --- | --- | --- |
| 4 | Untuned | 275 (1.0 s) | 443 (1.0 s) | 382 (0.5 s) | 190 (1.1 s) |
| 4 | Tuned | 461 (1.0 s) | 395 (1.0 s) | 512 (0.5 s) | 237 (5.5 s) |
| 16 | Untuned | 569 (26.2 s) | 631 (25.8 s) | 1,096 (28.8 s) | 348 (1.6 s) |
| 16 | Tuned | 559 (1.2 s) | 642 (1.1 s) | 646 (0.7 s) | 330 (10.7 s) |

## What the results mean

*Each row is a finding, how firm it is, and the evidence it rests on.*

| Finding | How firm | Basis |
| --- | --- | --- |
| The database is the shared ceiling | Firm | Holds on every side in every pass. Database CPU per command rises with concurrency, and throughput peaks at 8 to 16 workers on the C# sides and falls by 32. |
| Bulk chunk writes beat one transaction per command by an order of magnitude | Firm | All six bulk payment-file runs, on three sides, ran at 8,576 aggregates per second or more, against 992 or fewer for any normal single-command payment-file run. Bulk placement ran 16 to 60 times as fast as placing one account per command on the same side at the same worker count. |
| Marten's append function costs 3.2 to 4.8 times a plain insert | Firm | The statement records of 15 pairs of runs, each pair a Marten run and a direct-append run of the same workload and worker count in the same pass, in every pair. |
| Marten used the most database CPU per command among the C# sides | Fairly firm | Eight of ten same-pass comparisons and none lowest, with 2 to 18 percent more than the direct append in nine of them. The size of the margin is an indication. |
| The side without Marten is cheaper than a direct append | Not established | Within 8 percent of it either way in eight of ten comparisons, and higher in three of those (accrual at 8 workers, hashing off, real time at 16). The two larger gaps are the engine-paused run and one real-time run at 8 workers that is out of line with its neighbors. |
| Order of the C# sides by throughput | Not established | Single runs, with a spread of -16 to +47 percent between repeats. |
| Python as written costs 4 to 9 times the application CPU per command | Firm for this code | Consistent in all three passes. It says nothing about what a tuned Python write path would cost. |
| Reading the chain's head costs about 18 percent of database CPU per command | Indication | One pair of runs on one side. The hash itself is computed in the application. |
| The reader without Marten lags more under saturation | Indication | One run: 26 seconds to catch up after accrual at 32 workers, against 3 seconds through Marten and 6 seconds with the direct append. |
| Python's rule delays | Not steady state | The measured window includes engine start-up, as the limits describe. |

In more detail:

1. **The database is the shared ceiling.** The cost of each statement rises as more sessions are active. On the payment file the event insert on the side without Marten averaged 0.27 ms per call at 8 workers and 1.15 ms at 32, and Marten's append function averaged 0.70 ms and 2.72 ms. Every side shares hot spots that could explain this: one event sequence, the rightmost leaf of the index on it, and a few small tables updated on every command. The 16- and 32-worker results therefore measure contention as much as language or library, and a design that writes one transaction per command would need roughly 26 to 47 database cores at these costs, while the cost per command rises with concurrency, so whether one database can scale that far is a question round 2 tests. Bulk chunk writes change that picture, because the cost per aggregate is a small fraction of a single command's.
2. **Python as written spent more per command, and the gap is mostly application CPU.** At 8 workers its database used 1.0 to 1.6 times the CPU per command of the C# sides, and its application 4 to 9 times. Its database ran about 28 statements per payment at 8 workers, the engine's reads included, against 15 to 18 on the C# sides, because the prototype's handlers send each read and write as its own statement, look up a reason code for every event, and save each new payment with an UPDATE before its INSERT, which is what Django's save does for a row whose key is already set. Application servers scale out and the database does not, so the application CPU gap changes how many servers a load needs more than it changes the ceiling. It describes the prototype's code, which sends each query as it goes and uses Django's object layer for its state rows, and not what Python can do when tuned. At 16 and 32 workers Python's driver was held at its 4-CPU limit, so its throughput there is 4 CPUs divided by its CPU per command: a measure of cost per command, not of its engine or its database.
3. **Marten's cost is in its append.** The direct-append side runs Marten's code with one change, a plain insert into Marten's own table, and Marten's append function took 3.2 to 4.8 times as long per call. Reading the function, it re-reads the stream's version, checks the stream's tenant in a separate query, and inserts each event in a loop, much of which repeats the lock and version check that the command has already made. The ten same-pass comparisons are the second pass's tuned runs in which all three C# sides ran the same workload at the same worker count: both batch jobs at 8 and 32 workers, real time at 4, 8, 16, and 32 workers, and the two diagnostics. In them Marten used 2 to 18 percent more database CPU per command than the direct append in nine, and 0.7 percent less in the tenth. The side without Marten has no stream table, which both Marten sides update on every command. Whether that explains the largest gap between the direct append and the side without Marten, 5.0 against 4.0 ms with the engine paused, through dead tuples, vacuum, and full-page writes after each checkpoint, is untested. Statement time per payment in those two runs was about equal, 1.22 against 1.15 ms, so the gap lies outside statement execution. The harness now records write activity per run, so round 2 can test it.
4. **Tuning mattered, and differently for each runtime.** With four rule workers every C# side's rules fell 25 to 29 seconds behind at the 99th percentile at 16 bench workers, so the untuned 16-worker figures are not comparable with the tuned ones. The side without Marten reached 1,096 commands per second untuned at 16 workers for two reasons: part of its rule work was pushed past the measured window, and with fewer active database sessions every statement ran about twice as fast, its event insert averaging 0.38 ms against 0.73 ms in the tuned run. Python went the other way in the recorded runs: the untuned setting, one projection worker and four rule workers, kept up at 16 bench workers with a 99th-percentile delay of 1.6 seconds, where the tuned three and eight lagged at 10.7. The untuned engine runs 9 processes on its 2 CPUs and the tuned one 19, each loading Django at start-up, so the start-up flaw described in the limits, and fewer readers competing for the same CPUs, account for part of that difference. In a development trial after the passes, outside the recorded results, with the start-up flaw fixed and three projection workers in both settings, four rule workers gave a 99th-percentile delay of 25 seconds at 8 bench workers and 18 at 16, and eight gave 5.3 and 7.9, so Python stays at eight. Neither keeps fully up, because its engine was at its 2-CPU limit about 90 percent of the time: at 500,000 accounts its rules need more CPU than the method's engine cap allows, which is a limit of the method and not evidence about a Python deployment with more cores.
5. **Read models are the next question once writes are fast.** After the batch jobs, the C# read models were caught up within 3 to 6 seconds, except for the 26 seconds noted above. Python's took 31 and 46 minutes after the payment file with one projection worker, and 10 to 13 minutes with three at 8 workers or more. After the interest accrual its took 3 to 5 minutes with three. At 4 workers both jobs' read models caught up within about 3 seconds. After the bulk workloads the C# read models and rules caught up in 1.3 to 2.4 minutes, and Python's in about 28 minutes. Python's bulk writer is Foundation-written, with plain SQL through psycopg and not Django's object layer. Its writes kept pace with the C# sides', which suggests that Python's single-command cost lies in the per-command path, Django's object layer included, and not in Python's ability to drive PostgreSQL. The difference in catch-up is partly the reader difference described above, and partly that the reader loads every event through Django's object layer.
6. **A bulk chunk holds its aggregates locked until it commits.** In these runs the typical chunk, from start to commit, took 0.2 to 0.8 seconds at 4 workers and 1.1 to 1.7 seconds at 16, and the slowest 1 percent took up to 1.8 and 3.1 seconds. Only writers to those aggregates wait on the locks, and ordinary reads are not blocked. An open chunk transaction also holds back the high-water mark that every reader of the event stream waits on, so read models lag by about as long as the chunk runs. A bulk job in business hours would add database load and read-model lag on top of that, which the design records as an open question and these runs did not test.

### What dropping Marten costs

Dropping Marten means Wax owns what Marten provided. The side without Marten replaces the event and stream tables with `core.event`, appends with one insert per event, ports Andy Troiano's reader and high-water mark in place of Marten's daemon, and runs a partitioned rule runner in place of Marten's subscription. It has gaps that Marten would have filled, and costs that the runs measured:

- **Upcasting is not built.** [WAX-0026 Event Schema Versioning](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0026-event-schema-versioning.md) requires read-time upcasting, and nothing in the bake-off needed it.
- **Rebuilding a read model is not built.** A new projection version starts at zero and builds beside the old one, with no tooling around it.
- **Inline projections are not built.** WAX-0002 allows read models to be written in the command's transaction in v1, and this side does not do it.
- **Nothing supervises the readers across nodes.** Several readers share the projections through `SKIP LOCKED`, but there is no health view or rebalancing beyond the checkpoint table.
- **Wax owns the code and its correctness.** About 1,200 lines of C# need the Foundation's own tests and upkeep, and the reader's correctness rests on eight ported tests and not on a widely used library's production history.
- **It used more application CPU than Marten's daemon.** In the batch jobs at 8 workers its engine used 0.57 to 0.79 ms per command against 0.11 to 0.30 ms for the Marten sides, in every batch comparison. In one saturated run its read models also took 26 seconds to catch up against 3 seconds through Marten. Both are open to tuning.
- **The decision records change.** WAX-0025 names the Marten library as its most significant technical factor for C#, so C# has to stand on its other grounds, and the decisions that mention Marten need amending: WAX-0001, WAX-0003, WAX-0024, WAX-0025, and WAX-0026.

The reader also stops a projection on an event type it neither handles nor ignores, which Marten does not, and a long transaction anywhere on the PostgreSQL server holds every reader back until it ends. Both properties come from the prototype's design, and both are deliberate: a read model never silently skips an event it does not understand, and a reader never moves past a transaction that might still commit. They are described in the [csharp-wax README](csharp-wax/README.md).

Dropping Marten also gains something. `core.event` is built exactly as WAX-0001 specifies, with every column a real column. There is no stream table to update on every command. The dependency tree is smaller, Npgsql and the .NET hosting abstractions in place of Marten and what it brings, and Wax has no exposure to Marten's major-version changes. Database CPU per command was within 8 percent of a direct append's in eight of ten comparisons and lower than Marten's in nine of ten. In a development measurement outside the recorded passes, with the seed that `prepare --accounts 100000` makes on each side (100,000 accounts, one event per aggregate), `core.event` took 72 MiB including its indexes against 119 MiB for Marten's `core.mt_events` and `core.mt_streams` together, which is 39 percent less. Running `prepare` on each side and reading `pg_total_relation_size` of the event tables reproduces it.

## Limits

- **One laptop.** The machine is a laptop under Docker Desktop. A CPU limit caps CPU time without reserving cores, and the 12 CPUs the limits add up to share 16 logical processors with Windows, the Docker VM, and the harness. The laptop's speed drifted during the second pass: in Python's engine-paused run every database statement ran about 1.5 times slower than in other runs, with nothing in the code to explain it. From the third pass on, every run records a host-speed probe and the containers' CPU-throttle counters. The first pass ran with 13 idle containers from unrelated software up, and for the later passes those were stopped, along with a number of unrelated local MCP servers.
- **One run per configuration, and no spread.** The same configuration moved by -16 to +47 percent between passes. The order of the three C# sides at a given worker count is not settled, and margins between them should not be quoted until repeats exist.
- **A small data set.** 500,000 accounts fit in memory with PostgreSQL's 3 GB of shared buffers. A result here shows how fast each stack runs the command path while its working set fits in memory, not what happens at 200 million, where it would not.
- **Unequal effort on the write path.** Nobody tried to speed up Python's single-command write path, and the Foundation wrote much of its engine. [What we compared](#what-we-compared) says what that means for reading the Python numbers.
- **The Python driver's CPU limit.** At 16 and 32 workers Python's bench driver sat at its 4-CPU limit, so its throughput there measures its cost per command and not what its engine or database could do.
- **A flaw in when measuring started.** The harness started measuring 5 seconds after starting the engine. Measured in development runs after the passes, Python's engine spends about 26 seconds at its full 2-CPU limit while its processes load, and the C# engines about 10, so engine start-up fell inside the measured window, most of all on the Python side. Batch throughput barely changes, because start-up is 1 to 2 percent of a batch run, but rules cannot run while their processes load, so the backlog built during start-up drained into the measured window. Python's real-time rule delays in all three passes are therefore partly start-up and not steady state, and its CPU per command in the short bulk runs is noticeably high, because start-up was a large share of runs that lasted under a minute. The harness now waits for the engine's CPU to go quiet before measuring, in every later pass; the three recorded passes used the 5-second wait.
- **The hash covers the older field list.** Every side hashes the fields WAX-0013 listed until 2026-10-08, when it moved to a hash over the event's full payload, including the time the store assigns and the two sequence numbers, which the hashing code must read inside the transaction. Pull request 26 proposes taking the global sequence number out of the hashed content. No side does the full-payload hash yet, so the cost of the chain is understated by whatever that read adds.
- **Each side is tuned for its own runtime,** so the engines' settings differ by side, and the first pass used one untuned setting on every side.
- **What is not measured.** The web layer and frontend, developer productivity, hiring, deployment, security, and any other language. This is a measurement of the write path and nothing else.

## Review and reproduce

The repository holds the specification in [WORKLOADS.md](WORKLOADS.md), the [harness](harness/README.md), the plans, logs, and status files in [passes](passes/), and each run's record in [results](results/README.md), with its raw output unedited. Every figure from the three passes is in a run's `results.json`, in its summary block or, for statement-level figures, in its recorded top statements. The few figures from development trials outside the passes are marked as such where they appear, and the Python re-test of its rule workers is summarized in the comment above `TUNED` in [passes/make_plan.py](passes/make_plan.py). A rerun with the current harness waits for the engine to settle before measuring, which round 1 did not. To rerun a configuration, install Docker, and from the `harness` folder run `prepare` for a side and then `run` for a workload, as the harness README shows; `passes/run_pass.py` runs a whole plan.

Before publication the Foundation rewrote the repository's history once, to remove names of unrelated software and local file paths from the first pass's host records, two commit messages, and the pass logs, and to drop internal runbooks for two benchmark machines, the PC and the AWS host. Apart from removing those names and paths, and rewording one code comment that named the AWS host's runbook, no result, code, or method text changed, and every author, date, and commit subject is as it was. A result record's `repository_commit` cites the commit it ran against, and [COMMIT-MAP.md](COMMIT-MAP.md) maps each of those commit IDs to the published history.

We invite review and want corrections. Andy Troiano wrote the prototype the Python side starts from, and he is the first person we ask to review it. If you believe any side was not built or tuned properly, send a pull request or open an issue with the change and the reason, and say what you expect it to change. When an improvement is accepted, we repeat the affected runs and update the results. We equally welcome a different reading of the numbers.

## Round 2

Round 2 runs on cloud hardware in the Foundation's own account, so that the results are not tied to a laptop. The plans are in [passes](passes/): an anchoring pass under the same limits as round 1, to see whether the laptop's ordering holds on server CPUs; a scaled pass with a database of 16 CPUs, to find where each side stops scaling; the highest worker counts again through PgBouncer, to separate each side's ceiling from the contention seen past 8 to 16 workers; and a check at 5,000,000 accounts, ten times the data, to see whether the cost per command changes when the working set is larger; the limits for that check are still to be settled. Round 2 measures each configuration afresh on the cloud host. A spread on round 1's laptop figures would need the laptop repeats that the third pass's plan still holds. The plans may change as the first checks on the cloud host show what needs adjusting.

## Appendix A: Every run

Every recorded run, in the order the passes ran them. Each run's folder holds its `run.md`, `results.json`, `environment.json`, and `raw/` output. The first pass's real-time CPU and catch-up figures were measured before two corrections to the method, so they do not compare with the later passes. Workers is the number of concurrent bench workers, Commands/s is the commands that succeeded per second of the measured window, and catch-up is the time the read models and rules took to reach the head after the bench command ended.

*Each row is one run: commands per second, CPU in ms per command (application / database), catch-up in seconds, and 99th percentile rule delay in seconds.*

| Run | Side | Workload | Workers | Engine arguments | Commands/s | CPU, app / DB | Catch-up s | Rule p99 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [first-pass-csharp-batch-payments](results/2026-10-08-first-pass-csharp-batch-payments/run.md) | C# through Marten | Payment file | 4 | `rule-workers 4` | 425 | 1.97 / 5.11 | 1.6 |  |
| [first-pass-csharp-batch-payments](results/2026-10-08-first-pass-csharp-batch-payments/run.md) | C# through Marten | Payment file | 16 | `rule-workers 4` | 841 | 1.70 / 6.81 | 1.8 |  |
| [first-pass-csharp-batch-accrual](results/2026-10-09-first-pass-csharp-batch-accrual/run.md) | C# through Marten | Interest accrual | 4 | `rule-workers 4` | 334 | 1.82 / 4.38 | 1.6 |  |
| [first-pass-csharp-batch-accrual](results/2026-10-09-first-pass-csharp-batch-accrual/run.md) | C# through Marten | Interest accrual | 16 | `rule-workers 4` | 1,168 | 1.11 / 4.40 | 1.5 |  |
| [first-pass-csharp-realtime](results/2026-10-09-first-pass-csharp-realtime/run.md) | C# through Marten | Real time | 4 | `rule-workers 4` | 297 | 4.38 / 10.50 | 60.8 | 1.03 |
| [first-pass-csharp-realtime](results/2026-10-09-first-pass-csharp-realtime/run.md) | C# through Marten | Real time | 16 | `rule-workers 4` | 651 | 3.51 / 12.14 | 60.5 | 26.81 |
| [first-pass-csharp-direct-batch-payments](results/2026-10-09-first-pass-csharp-direct-batch-payments/run.md) | C# with direct append | Payment file | 4 | `rule-workers 4` | 321 | 1.97 / 5.38 | 1.7 |  |
| [first-pass-csharp-direct-batch-payments](results/2026-10-09-first-pass-csharp-direct-batch-payments/run.md) | C# with direct append | Payment file | 16 | `rule-workers 4` | 975 | 1.34 / 5.72 | 1.5 |  |
| [first-pass-csharp-direct-batch-accrual](results/2026-10-09-first-pass-csharp-direct-batch-accrual/run.md) | C# with direct append | Interest accrual | 4 | `rule-workers 4` | 337 | 1.69 / 4.12 | 1.5 |  |
| [first-pass-csharp-direct-batch-accrual](results/2026-10-09-first-pass-csharp-direct-batch-accrual/run.md) | C# with direct append | Interest accrual | 16 | `rule-workers 4` | 1,287 | 0.93 / 3.78 | 1.5 |  |
| [first-pass-csharp-direct-realtime](results/2026-10-09-first-pass-csharp-direct-realtime/run.md) | C# with direct append | Real time | 4 | `rule-workers 4` | 302 | 3.93 / 9.60 | 61.5 | 1.03 |
| [first-pass-csharp-direct-realtime](results/2026-10-09-first-pass-csharp-direct-realtime/run.md) | C# with direct append | Real time | 16 | `rule-workers 4` | 755 | 2.90 / 10.37 | 60.8 | 27.73 |
| [first-pass-python-batch-payments](results/2026-10-09-first-pass-python-batch-payments/run.md) | Python | Payment file | 4 | `rule-workers 4` | 205 | 11.62 / 6.98 | 1,880 |  |
| [first-pass-python-batch-payments](results/2026-10-09-first-pass-python-batch-payments/run.md) | Python | Payment file | 16 | `rule-workers 4` | 310 | 15.65 / 9.54 | 2,743 |  |
| [first-pass-python-batch-accrual](results/2026-10-09-first-pass-python-batch-accrual/run.md) | Python | Interest accrual | 4 | `rule-workers 4` | 264 | 7.52 / 4.55 | 1.5 |  |
| [first-pass-python-batch-accrual](results/2026-10-09-first-pass-python-batch-accrual/run.md) | Python | Interest accrual | 16 | `rule-workers 4` | 489 | 9.70 / 5.67 | 548 |  |
| [first-pass-python-realtime](results/2026-10-09-first-pass-python-realtime/run.md) | Python | Real time | 4 | `rule-workers 4` | 194 | 18.64 / 10.96 | 163 | 1.19 |
| [first-pass-python-realtime](results/2026-10-09-first-pass-python-realtime/run.md) | Python | Real time | 16 | `rule-workers 4` | 292 | 26.56 / 14.84 | 395 | 1.95 |
| [second-pass-csharp-direct-batch-accrual-w32](results/2026-10-09-second-pass-csharp-direct-batch-accrual-w32/run.md) | C# with direct append | Interest accrual | 32 | `rule-workers 16` | 1,044 | 1.06 / 5.74 | 6.3 |  |
| [second-pass-csharp-realtime-w4](results/2026-10-09-second-pass-csharp-realtime-w4/run.md) | C# through Marten | Real time | 4 | `rule-workers 16` | 461 | 2.23 / 6.27 | 3.3 | 1.01 |
| [second-pass-python-realtime-w32](results/2026-10-09-second-pass-python-realtime-w32/run.md) | Python | Real time | 32 | `projection-workers 3 rule-workers 8` | 268 | 24.20 / 12.83 | 133 | 3.12 |
| [second-pass-csharp-batch-accrual-w8](results/2026-10-09-second-pass-csharp-batch-accrual-w8/run.md) | C# through Marten | Interest accrual | 8 | `rule-workers 16` | 835 | 1.26 / 3.71 | 3.1 |  |
| [second-pass-csharp-batch-payments-w32](results/2026-10-09-second-pass-csharp-batch-payments-w32/run.md) | C# through Marten | Payment file | 32 | `rule-workers 16` | 622 | 1.89 / 9.64 | 2.9 |  |
| [second-pass-csharp-direct-realtime-w32](results/2026-10-09-second-pass-csharp-direct-realtime-w32/run.md) | C# with direct append | Real time | 32 | `rule-workers 16` | 598 | 2.18 / 9.94 | 3.3 | 1.41 |
| [second-pass-python-batch-accrual-w32](results/2026-10-09-second-pass-python-batch-accrual-w32/run.md) | Python | Interest accrual | 32 | `projection-workers 3 rule-workers 8` | 469 | 10.99 / 6.21 | 192 |  |
| [second-pass-csharp-wax-realtime-w32](results/2026-10-09-second-pass-csharp-wax-realtime-w32/run.md) | C# without Marten | Real time | 32 | `projection-workers 3 rule-workers 16` | 628 | 2.24 / 9.47 | 3.4 | 1.83 |
| [second-pass-python-realtime-w16](results/2026-10-09-second-pass-python-realtime-w16/run.md) | Python | Real time | 16 | `projection-workers 3 rule-workers 8` | 330 | 19.16 / 10.88 | 212 | 10.70 |
| [second-pass-csharp-wax-realtime-w16](results/2026-10-09-second-pass-csharp-wax-realtime-w16/run.md) | C# without Marten | Real time | 16 | `projection-workers 3 rule-workers 16` | 646 | 2.61 / 8.98 | 3.4 | 0.69 |
| [second-pass-csharp-wax-batch-accrual-w32](results/2026-10-09-second-pass-csharp-wax-batch-accrual-w32/run.md) | C# without Marten | Interest accrual | 32 | `projection-workers 3 rule-workers 16` | 1,054 | 1.21 / 5.69 | 25.7 |  |
| [second-pass-csharp-batch-payments-w8](results/2026-10-09-second-pass-csharp-batch-payments-w8/run.md) | C# through Marten | Payment file | 8 | `rule-workers 16` | 679 | 1.66 / 5.15 | 3.3 |  |
| [second-pass-csharp-wax-batch-accrual-w8](results/2026-10-09-second-pass-csharp-wax-batch-accrual-w8/run.md) | C# without Marten | Interest accrual | 8 | `projection-workers 3 rule-workers 16` | 834 | 1.62 / 3.91 | 3.1 |  |
| [second-pass-csharp-direct-realtime-w8](results/2026-10-09-second-pass-csharp-direct-realtime-w8/run.md) | C# with direct append | Real time | 8 | `rule-workers 16` | 656 | 2.00 / 6.43 | 3.2 | 1.03 |
| [second-pass-python-batch-payments-w8](results/2026-10-09-second-pass-python-batch-payments-w8/run.md) | Python | Payment file | 8 | `projection-workers 3 rule-workers 8` | 427 | 12.80 / 7.38 | 751 |  |
| [second-pass-csharp-wax-batch-payments-w8](results/2026-10-10-second-pass-csharp-wax-batch-payments-w8/run.md) | C# without Marten | Payment file | 8 | `projection-workers 3 rule-workers 16` | 804 | 1.86 / 4.64 | 3.1 |  |
| [second-pass-csharp-wax-batch-accrual-w16](results/2026-10-10-second-pass-csharp-wax-batch-accrual-w16/run.md) | C# without Marten | Interest accrual | 16 | `projection-workers 3 rule-workers 16` | 1,547 | 0.98 / 3.42 | 3.0 |  |
| [second-pass-csharp-direct-batch-payments-w32](results/2026-10-10-second-pass-csharp-direct-batch-payments-w32/run.md) | C# with direct append | Payment file | 32 | `rule-workers 16` | 691 | 1.63 / 8.68 | 3.2 |  |
| [second-pass-csharp-direct-batch-accrual-w8](results/2026-10-10-second-pass-csharp-direct-batch-accrual-w8/run.md) | C# with direct append | Interest accrual | 8 | `rule-workers 16` | 787 | 1.21 / 3.65 | 3.4 |  |
| [second-pass-python-realtime-w4](results/2026-10-10-second-pass-python-realtime-w4/run.md) | Python | Real time | 4 | `projection-workers 3 rule-workers 8` | 237 | 15.97 / 9.19 | 57.2 | 5.53 |
| [second-pass-csharp-wax-batch-payments-w16](results/2026-10-10-second-pass-csharp-wax-batch-payments-w16/run.md) | C# without Marten | Payment file | 16 | `projection-workers 3 rule-workers 16` | 992 | 1.64 / 5.92 | 3.1 |  |
| [second-pass-csharp-wax-batch-accrual-w4](results/2026-10-10-second-pass-csharp-wax-batch-accrual-w4/run.md) | C# without Marten | Interest accrual | 4 | `projection-workers 3 rule-workers 16` | 410 | 2.68 / 4.74 | 3.1 |  |
| [second-pass-python-batch-accrual-w8](results/2026-10-10-second-pass-python-batch-accrual-w8/run.md) | Python | Interest accrual | 8 | `projection-workers 3 rule-workers 8` | 556 | 6.90 / 4.07 | 210 |  |
| [second-pass-csharp-batch-accrual-w32](results/2026-10-10-second-pass-csharp-batch-accrual-w32/run.md) | C# through Marten | Interest accrual | 32 | `rule-workers 16` | 919 | 1.32 / 6.51 | 3.1 |  |
| [second-pass-csharp-realtime-w32](results/2026-10-10-second-pass-csharp-realtime-w32/run.md) | C# through Marten | Real time | 32 | `rule-workers 16` | 487 | 2.70 / 11.70 | 3.7 | 1.40 |
| [second-pass-python-batch-payments-w32](results/2026-10-10-second-pass-python-batch-payments-w32/run.md) | Python | Payment file | 32 | `projection-workers 3 rule-workers 8` | 276 | 22.04 / 12.09 | 575 |  |
| [second-pass-csharp-direct-realtime-w16](results/2026-10-10-second-pass-csharp-direct-realtime-w16/run.md) | C# with direct append | Real time | 16 | `rule-workers 16` | 642 | 2.13 / 8.62 | 3.3 | 1.11 |
| [second-pass-csharp-wax-batch-payments-w32](results/2026-10-10-second-pass-csharp-wax-batch-payments-w32/run.md) | C# without Marten | Payment file | 32 | `projection-workers 3 rule-workers 16` | 701 | 1.73 / 8.55 | 3.9 |  |
| [second-pass-csharp-wax-realtime-w4](results/2026-10-10-second-pass-csharp-wax-realtime-w4/run.md) | C# without Marten | Real time | 4 | `projection-workers 3 rule-workers 16` | 512 | 3.21 / 6.02 | 3.3 | 0.52 |
| [second-pass-csharp-wax-batch-payments-w4](results/2026-10-10-second-pass-csharp-wax-batch-payments-w4/run.md) | C# without Marten | Payment file | 4 | `projection-workers 3 rule-workers 16` | 415 | 3.08 / 5.82 | 3.1 |  |
| [second-pass-csharp-realtime-w16](results/2026-10-10-second-pass-csharp-realtime-w16/run.md) | C# through Marten | Real time | 16 | `rule-workers 16` | 559 | 2.62 / 10.12 | 4.5 | 1.16 |
| [second-pass-csharp-realtime-w8](results/2026-10-10-second-pass-csharp-realtime-w8/run.md) | C# through Marten | Real time | 8 | `rule-workers 16` | 647 | 2.18 / 6.91 | 3.3 | 1.02 |
| [second-pass-csharp-wax-realtime-w8](results/2026-10-10-second-pass-csharp-wax-realtime-w8/run.md) | C# without Marten | Real time | 8 | `projection-workers 3 rule-workers 16` | 1,020 | 1.96 / 4.73 | 3.5 | 0.52 |
| [second-pass-csharp-direct-batch-payments-w8](results/2026-10-10-second-pass-csharp-direct-batch-payments-w8/run.md) | C# with direct append | Payment file | 8 | `rule-workers 16` | 729 | 1.48 / 4.71 | 3.2 |  |
| [second-pass-python-realtime-w8](results/2026-10-10-second-pass-python-realtime-w8/run.md) | Python | Real time | 8 | `projection-workers 3 rule-workers 8` | 407 | 13.14 / 7.69 | 240 | 5.21 |
| [second-pass-csharp-direct-realtime-w4](results/2026-10-10-second-pass-csharp-direct-realtime-w4/run.md) | C# with direct append | Real time | 4 | `rule-workers 16` | 395 | 2.24 / 6.32 | 3.2 | 1.03 |
| [second-pass-csharp-wax-batch-payments-w16-hash-off](results/2026-10-10-second-pass-csharp-wax-batch-payments-w16-hash-off/run.md) | C# without Marten | Payment file, hashing off | 16 | `projection-workers 3 rule-workers 16` | 1,172 | 1.45 / 4.87 | 3.2 |  |
| [second-pass-csharp-batch-payments-w16-no-engine](results/2026-10-10-second-pass-csharp-batch-payments-w16-no-engine/run.md) | C# through Marten | Payment file, engine paused | 16 | `rule-workers 16` | 1,014 | 1.31 / 5.64 |  |  |
| [second-pass-csharp-wax-batch-payments-w16-no-engine](results/2026-10-10-second-pass-csharp-wax-batch-payments-w16-no-engine/run.md) | C# without Marten | Payment file, engine paused | 16 | `projection-workers 3 rule-workers 16` | 1,301 | 0.96 / 3.99 |  |  |
| [second-pass-csharp-direct-batch-payments-w16-hash-off](results/2026-10-10-second-pass-csharp-direct-batch-payments-w16-hash-off/run.md) | C# with direct append | Payment file, hashing off | 16 | `rule-workers 16` | 1,190 | 1.21 / 4.65 | 3.5 |  |
| [second-pass-csharp-batch-payments-w16-hash-off](results/2026-10-10-second-pass-csharp-batch-payments-w16-hash-off/run.md) | C# through Marten | Payment file, hashing off | 16 | `rule-workers 16` | 1,071 | 1.36 / 5.34 | 3.2 |  |
| [second-pass-python-batch-payments-w16-no-engine](results/2026-10-10-second-pass-python-batch-payments-w16-no-engine/run.md) | Python | Payment file, engine paused | 16 | `projection-workers 3 rule-workers 8` | 238 | 16.81 / 10.37 |  |  |
| [second-pass-csharp-direct-batch-payments-w16-no-engine](results/2026-10-10-second-pass-csharp-direct-batch-payments-w16-no-engine/run.md) | C# with direct append | Payment file, engine paused | 16 | `rule-workers 16` | 1,114 | 1.08 / 5.00 |  |  |
| [second-pass-python-batch-payments-w16-hash-off](results/2026-10-10-second-pass-python-batch-payments-w16-hash-off/run.md) | Python | Payment file, hashing off | 16 | `projection-workers 3 rule-workers 8` | 404 | 14.92 / 8.12 | 751 |  |
| [second-pass-csharp-direct-placement-w4](results/2026-10-10-second-pass-csharp-direct-placement-w4/run.md) | C# with direct append | Placement, one command each | 4 | `rule-workers 16` | 301 | 5.08 / 16.37 | 3.3 |  |
| [second-pass-csharp-direct-placement-bulk-w16](results/2026-10-10-second-pass-csharp-direct-placement-bulk-w16/run.md) | C# with direct append | Bulk placement | 16 | `rule-workers 16` | 12,077 | 0.18 / 0.55 | 134 |  |
| [second-pass-csharp-wax-batch-payments-bulk-w16](results/2026-10-10-second-pass-csharp-wax-batch-payments-bulk-w16/run.md) | C# without Marten | Bulk payment file | 16 | `projection-workers 3 rule-workers 16` | 11,694 | 0.15 / 0.50 | 111 |  |
| [second-pass-csharp-wax-placement-w16](results/2026-10-10-second-pass-csharp-wax-placement-w16/run.md) | C# without Marten | Placement, one command each | 16 | `projection-workers 3 rule-workers 16` | 503 | 3.28 / 11.98 | 56.9 |  |
| [second-pass-csharp-direct-batch-payments-bulk-w4](results/2026-10-10-second-pass-csharp-direct-batch-payments-bulk-w4/run.md) | C# with direct append | Bulk payment file | 4 | `rule-workers 16` | 13,071 | 0.20 / 0.44 | 79.0 |  |
| [second-pass-csharp-direct-placement-bulk-w4](results/2026-10-10-second-pass-csharp-direct-placement-bulk-w4/run.md) | C# with direct append | Bulk placement | 4 | `rule-workers 16` | 4,790 | 0.35 / 1.29 | 130 |  |
| [second-pass-csharp-direct-placement-w16](results/2026-10-10-second-pass-csharp-direct-placement-w16/run.md) | C# with direct append | Placement, one command each | 16 | `rule-workers 16` | 471 | 3.47 / 12.79 | 61.6 |  |
| [second-pass-python-batch-payments-bulk-w16](results/2026-10-10-second-pass-python-batch-payments-bulk-w16/run.md) | Python | Bulk payment file | 16 | `projection-workers 3 rule-workers 8` | 12,051 | 0.66 / 0.41 | 1,657 |  |
| [second-pass-python-placement-bulk-w4](results/2026-10-10-second-pass-python-placement-bulk-w4/run.md) | Python | Bulk placement | 4 | `projection-workers 3 rule-workers 8` | 15,640 | 0.38 / 0.23 | 1,731 |  |
| [second-pass-python-batch-payments-bulk-w4](results/2026-10-10-second-pass-python-batch-payments-bulk-w4/run.md) | Python | Bulk payment file | 4 | `projection-workers 3 rule-workers 8` | 11,791 | 0.41 / 0.33 | 1,704 |  |
| [second-pass-csharp-direct-batch-payments-bulk-w16](results/2026-10-10-second-pass-csharp-direct-batch-payments-bulk-w16/run.md) | C# with direct append | Bulk payment file | 16 | `rule-workers 16` | 8,576 | 0.18 / 0.68 | 98.8 |  |
| [second-pass-csharp-wax-placement-bulk-w4](results/2026-10-10-second-pass-csharp-wax-placement-bulk-w4/run.md) | C# without Marten | Bulk placement | 4 | `projection-workers 3 rule-workers 16` | 7,481 | 0.23 / 0.86 | 133 |  |
| [second-pass-python-placement-w4](results/2026-10-10-second-pass-python-placement-w4/run.md) | Python | Placement, one command each | 4 | `projection-workers 3 rule-workers 8` | 262 | 13.11 / 7.84 | 1,319 |  |
| [second-pass-csharp-wax-placement-w4](results/2026-10-10-second-pass-csharp-wax-placement-w4/run.md) | C# without Marten | Placement, one command each | 4 | `projection-workers 3 rule-workers 16` | 370 | 5.25 / 13.94 | 3.3 |  |
| [second-pass-csharp-wax-placement-bulk-w16](results/2026-10-10-second-pass-csharp-wax-placement-bulk-w16/run.md) | C# without Marten | Bulk placement | 16 | `projection-workers 3 rule-workers 16` | 8,831 | 0.17 / 0.76 | 144 |  |
| [second-pass-csharp-wax-batch-payments-bulk-w4](results/2026-10-10-second-pass-csharp-wax-batch-payments-bulk-w4/run.md) | C# without Marten | Bulk payment file | 4 | `projection-workers 3 rule-workers 16` | 16,198 | 0.16 / 0.35 | 94.2 |  |
| [second-pass-csharp-direct-realtime-w16-untuned](results/2026-10-10-second-pass-csharp-direct-realtime-w16-untuned/run.md) | C# with direct append | Real time | 16 | `rule-workers 4` | 631 | 2.39 / 9.07 | 13.5 | 25.77 |
| [third-pass-csharp-direct-batch-payments-w16](results/2026-10-10-third-pass-csharp-direct-batch-payments-w16/run.md) | C# with direct append | Payment file | 16 | `rule-workers 16` | 932 | 1.43 / 6.18 | 3.2 |  |
| [third-pass-python-realtime-w16-untuned](results/2026-10-10-third-pass-python-realtime-w16-untuned/run.md) | Python | Real time | 16 | `rule-workers 4` | 348 | 16.52 / 9.53 | 419 | 1.62 |
| [third-pass-csharp-batch-accrual-w4](results/2026-10-10-third-pass-csharp-batch-accrual-w4/run.md) | C# through Marten | Interest accrual | 4 | `rule-workers 16` | 398 | 1.59 / 3.88 | 3.2 |  |
| [third-pass-csharp-direct-batch-payments-w4](results/2026-10-10-third-pass-csharp-direct-batch-payments-w4/run.md) | C# with direct append | Payment file | 4 | `rule-workers 16` | 386 | 1.77 / 4.75 | 3.3 |  |
| [third-pass-python-batch-payments-w16](results/2026-10-10-third-pass-python-batch-payments-w16/run.md) | Python | Payment file | 16 | `projection-workers 3 rule-workers 8` | 372 | 16.17 / 9.03 | 626 |  |
| [third-pass-csharp-direct-batch-accrual-w16](results/2026-10-10-third-pass-csharp-direct-batch-accrual-w16/run.md) | C# with direct append | Interest accrual | 16 | `rule-workers 16` | 1,416 | 0.86 / 3.70 | 3.2 |  |
| [third-pass-python-placement-bulk-w16](results/2026-10-10-third-pass-python-placement-bulk-w16/run.md) | Python | Bulk placement | 16 | `projection-workers 3 rule-workers 8` | 11,391 | 0.97 / 0.54 | 1,724 |  |
| [third-pass-python-batch-accrual-w4](results/2026-10-10-third-pass-python-batch-accrual-w4/run.md) | Python | Interest accrual | 4 | `projection-workers 3 rule-workers 8` | 366 | 6.75 / 4.02 | 3.4 |  |
| [third-pass-csharp-direct-realtime-w4-untuned](results/2026-10-10-third-pass-csharp-direct-realtime-w4-untuned/run.md) | C# with direct append | Real time | 4 | `rule-workers 4` | 443 | 2.33 / 5.78 | 3.2 | 1.04 |
| [third-pass-python-batch-accrual-w16](results/2026-10-10-third-pass-python-batch-accrual-w16/run.md) | Python | Interest accrual | 16 | `projection-workers 3 rule-workers 8` | 569 | 8.98 / 5.23 | 314 |  |
| [third-pass-csharp-realtime-w16-untuned](results/2026-10-10-third-pass-csharp-realtime-w16-untuned/run.md) | C# through Marten | Real time | 16 | `rule-workers 4` | 569 | 2.75 / 9.96 | 12.3 | 26.17 |
| [third-pass-csharp-batch-payments-w4](results/2026-10-10-third-pass-csharp-batch-payments-w4/run.md) | C# through Marten | Payment file | 4 | `rule-workers 16` | 406 | 1.89 / 4.96 | 3.2 |  |
| [third-pass-csharp-wax-realtime-w16-untuned](results/2026-10-10-third-pass-csharp-wax-realtime-w16-untuned/run.md) | C# without Marten | Real time | 16 | `rule-workers 4` | 1,096 | 1.52 / 5.26 | 26.0 | 28.84 |
| [third-pass-csharp-batch-accrual-w16](results/2026-10-10-third-pass-csharp-batch-accrual-w16/run.md) | C# through Marten | Interest accrual | 16 | `rule-workers 16` | 1,046 | 1.12 / 4.46 | 3.1 |  |
| [third-pass-python-realtime-w4-untuned](results/2026-10-10-third-pass-python-realtime-w4-untuned/run.md) | Python | Real time | 4 | `rule-workers 4` | 190 | 13.16 / 7.96 | 143 | 1.12 |
| [third-pass-csharp-direct-batch-accrual-w4](results/2026-10-10-third-pass-csharp-direct-batch-accrual-w4/run.md) | C# with direct append | Interest accrual | 4 | `rule-workers 16` | 307 | 1.77 / 4.32 | 3.4 |  |
| [third-pass-csharp-batch-payments-w16](results/2026-10-10-third-pass-csharp-batch-payments-w16/run.md) | C# through Marten | Payment file | 16 | `rule-workers 16` | 815 | 1.66 / 6.90 | 3.1 |  |
| [third-pass-csharp-realtime-w4-untuned](results/2026-10-10-third-pass-csharp-realtime-w4-untuned/run.md) | C# through Marten | Real time | 4 | `rule-workers 4` | 275 | 3.15 / 7.82 | 3.2 | 1.02 |
| [third-pass-python-placement-w16](results/2026-10-10-third-pass-python-placement-w16/run.md) | Python | Placement, one command each | 16 | `projection-workers 3 rule-workers 8` | 432 | 14.39 / 8.10 | 1,483 |  |
| [third-pass-csharp-wax-realtime-w4-untuned](results/2026-10-10-third-pass-csharp-wax-realtime-w4-untuned/run.md) | C# without Marten | Real time | 4 | `rule-workers 4` | 382 | 3.32 / 6.50 | 3.5 | 0.51 |
| [third-pass-python-batch-payments-w4](results/2026-10-10-third-pass-python-batch-payments-w4/run.md) | Python | Payment file | 4 | `projection-workers 3 rule-workers 8` | 239 | 14.25 / 8.13 | 3.3 |  |

## Appendix B: Notes on each pass

### First pass, 2026-10-08 and 2026-10-09

The pass ran overnight from 23:46 to 06:17 and made 18 runs: C# through Marten, C# with direct append, and Python, each running the payment file, the interest accrual, and the real-time mix at 4 and at 16 workers, one run of each, with `--rule-workers 4` on every side. The laptop had no other work running, but 13 idle containers from unrelated software were up.

Limits particular to this pass:

- The catch-up time in each real-time run's record is not comparable across sides, because dispute reviews scheduled near the end of a run kept firing for up to 60 seconds afterward and kept moving the head. Batch catch-up times are unaffected.
- The real-time CPU figures included the warm-up's CPU but were divided only by the commands of the measured window, so they read about a third high, by the ratio of 240 seconds to 180. The ratios between the sides hold, and the batch figures, which the database ceiling uses, are unaffected.
- The sides ran in order, so a side's place in the night is mixed with its result, and the laptop may have slowed for heat over a seven-hour run.
- No engine was tuned.

After this pass the Foundation corrected two things in the method. Catch-up now ends at the head as it stood when the bench command ended, and CPU per command now divides by every command sent, the warm-up's included. The first pass's real-time configurations were rerun under the corrected method: tuned in the second pass, and with the first pass's untuned setting in the second and third.

### Second pass, 2026-10-09 and 2026-10-10

The pass ran overnight from 21:07 to 09:31 under the plan in [passes/second-pass.plan.md](passes/second-pass.plan.md), with the engines tuned and the unrelated software stopped. It completed 61 of the plan's 82 runs, and every one passed every gate that applied to it. The plan's runs fall into five groups, in priority order, each shuffled from a recorded seed:

1. The core comparison. On all four sides, both batch jobs at 8 and 32 workers and the real-time mix at 4, 8, 16, and 32. The side without Marten also ran both batch jobs at 4 and 16, to match the first pass. All of group 1 ran.
2. Two diagnostics, on the payment file at 16 workers on every side. With the engine paused, nothing reads the stream while the commands run, which shows what the readers cost the write path. With hashing off, every event carries a placeholder hash, the stream's head is not read, and the version check trusts the locked state row, with the unique index on stream and version as the backstop. All of group 2 ran.
3. The bulk workloads at 4 and 16 workers, on the direct-append side, the side without Marten, and Python. Group 3 ran 16 of its 18 runs; Python's placement and bulk placement at 16 workers did not fit before the deadline.
4. The real-time mix at 4 and 16 workers with every engine untuned, at the first pass's `--rule-workers 4`. Group 4 ran 1 of its 8 runs.
5. The first pass's batch runs on its three sides, for a second sample. Group 5 ran none.

The skipped runs carried over to the third pass.

Limits particular to this pass:

- The host's speed was not constant. In Python's engine-paused run, every statement's average time in PostgreSQL, a trivial in-memory lookup included, ran about 1.5 times as long as in other runs, and Python's payment file at 32 workers showed the same. Nothing in either run's code explains it, so the laptop itself slowed, most likely from clock speed, heat, or Docker's processors landing on the efficiency cores. Differences of 10 or 20 percent between single runs taken at different times of the night are within the noise.
- The engines' settings differ by side, because each side was tuned to its own runtime. The trials that chose them ran at 50,000 accounts on the same laptop and are not recorded beyond the summary in the comment above `TUNED` in [passes/make_plan.py](passes/make_plan.py).
- A light scheduled job that does not use Docker ran at 08:07, during Python's bulk payment file at 4 workers.

### Third pass, group 1 only, 2026-10-10

The pass ran from 10:32 to 16:42 under the plan in [passes/third-pass.plan.md](passes/third-pass.plan.md), cut to its first group: the 21 runs the second pass skipped for time. All 21 passed every gate that applied to them. They were the first pass's batch runs at 4 and 16 workers on its three sides, which complete the tuned results at every worker count; Python's placement and bulk placement at 16 workers; and the remaining untuned real-time runs. The plan's other groups, the diagnostic triples at 8 workers, the core repeat, and a second round of triples, did not run. The pass first started with the whole plan, was stopped during its first run, and restarted with group 1 only. That run's partial output was moved out of the results folder.

Every run in this pass records a host-speed probe, the median time of five sums of 20,000,000 integers in the run's database, taken just before the bench and outside the measured window, and each container's CPU-throttle counters from its cgroup. The probe stayed between 716 and 795 ms all day, so the laptop showed none of the slowdown seen in the second pass. Python's probes read 721 to 795 ms, with a median of 777, against 716 to 774 ms, with a median of 731, on the C# sides, because its engine's start-up overlapped the probe, as the limits describe; that difference is start-up and not host drift.
