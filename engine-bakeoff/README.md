# Engine bake-off

This experiment runs the same Wax command workload in two stacks, on the same machine and the same PostgreSQL configuration, and compares how fast each can push commands through the Wax write path while keeping Wax's invariants. The champion is C# on .NET with Marten as the event store, the stack adopted in [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md). The challenger is Python, the stack proposed in Andy Troiano's pull request on his fork of platform-design.

Status: setting up. The method below is a draft, and no results have been recorded.

## Background

WAX-0025 chose C# on .NET, with Marten as the event store, in August 2026. It weighed four criteria (platform-agnostic deployment, performance, ecosystem maturity, and the contributor community the project can realistically attract) and counted the founding team's fluency in C# as a further advantage in the first year.

On 2026-10-08 Andy Troiano proposed replacing that decision with Python on Django, in [atroiano/platform-design#1](https://github.com/atroiano/platform-design/pull/1). He backed the proposal with a working prototype: an event store written directly against PostgreSQL, a projection reader, a HiveAR slice that posts payments, and a blue-green release drill. Most of what separates the two stacks is a judgment call for the Foundation, which decides under its governance process. One question can be measured instead of argued: whether each stack can move commands through the Wax write path fast enough. This experiment measures that, and nothing else.

## Why throughput is the question

[WAX-0024 High Availability, Disaster Recovery, and Distributed Workloads](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0024-high-availability-disaster-recovery-and-distributed-workloads.md) requires batch jobs to run through the normal command handler path, spread across instances and threads. Every command in a batch therefore does the full work of a single write: it locks the aggregate's state row, checks the aggregate's version, computes the event's hash and links it to the prior event, writes the event, and updates the authoritative state, all in one transaction. A nightly job over a large agency's accounts is millions of those commands in a fixed window. Five million accounts in a four-hour window is about 350 commands per second, sustained; twenty million in six hours is about 930.

Wax v1 defers hash computation and multi-instance batch work to v2. The bake-off includes both anyway, because the language has to carry the full design, not only the first release.

## Contestants

| Folder | Role | Stack | Event store | Tuned by |
| --- | --- | --- | --- | --- |
| [csharp-marten](csharp-marten/README.md) | Champion | C# on the current .NET LTS release | Marten on PostgreSQL | Rob Grafrath |
| [python](python/README.md) | Challenger | Python 3.13 | Andy Troiano's own event store, written directly against PostgreSQL | Andy Troiano, who will be invited |

## Where Django fits

Andy's proposal is Python on Django, but Django is not the event store. His prototype writes events with its own SQL, and Django supplies the database connection, transactions, migrations, and the HTTP API. The bake-off drives the command handlers directly rather than over HTTP, so the web layer is outside the measured path on both sides. The Python side starts from Andy's code as written, with Django's database layer in the path, because that is what his proposal would ship. Its advocate may replace that layer with plain psycopg, and each result records which variant ran.

The two sides are not symmetrical, and the test does not pretend otherwise. On the C# side the event store is a library the Foundation would adopt; on the Python side it is code the Foundation would own.

## Invariants both sides keep

Every measured command does all of the following. A run that skips any of them does not count.

1. It locks the aggregate's authoritative state row with `SELECT FOR UPDATE` before computing anything, as [WAX-0013 Event Chain Integrity and Tamper-Evidence](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0013-event-chain-integrity-and-tamper-evidence.md) requires.
2. It rejects the write if the aggregate's version has moved since the command loaded it, the optimistic concurrency check in [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md).
3. It computes `event_hash` and `prior_event_hash` for each event, over the fields WAX-0013 lists, inside the same transaction as the event write. In Wax this is the Wax Security Module's job; the bake-off implements the same computation in each language.
4. It writes the event and updates the authoritative state in one atomic transaction, as [WAX-0002 CQRS Model](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0002-cqrs-model.md) requires.
5. It writes events one command at a time through the command handler, as WAX-0024 requires of batch work. A bulk insert that bypasses the per-event hash is not allowed.

## Method (draft, not yet agreed)

The workload is a payment file: one payment for each of one million accounts in a seeded database. Andy's prototype already implements payment posting, so both sides start from the same business rule. A second, contended workload sends several payments to the same accounts at once, to measure lock waits.

Each workload runs with 1, 2, 4, 8, 16, and 32 workers, so the results show how each stack scales and where it stops scaling. Each runs twice: once with the read model updated inside the command's own transaction, which WAX-0002 permits in v1, and once with a separate reader catching up afterward.

Both stacks run on one named machine against the same PostgreSQL 17 container with the same configuration, one stack at a time. Each configuration gets a warm-up pass and then three measured runs, and the median is reported.

The measurements are these:

- Sustained commands per second over the measured run.
- Application CPU time and database CPU time per thousand commands.
- Command latency at the 50th and 99th percentiles.
- How far the read model falls behind, in the separate-reader mode.

After every run, three correctness gates are checked: every payment was applied exactly once, every hash chain verifies, and the read-model balances match the authoritative balances.

## Rules

1. The method is agreed and committed before any result is recorded. A later change to the method is committed with its reason, and earlier results are rerun under it.
2. Each side is tuned by its advocate. Any tuning is allowed if it keeps every invariant and is committed here.
3. Database tuning applies to both sides or to neither.
4. Every result records the machine, the operating system, the PostgreSQL version and settings, the commit of each side, and the raw harness output, unedited, in [results](results/README.md).
5. A run that fails a correctness gate is recorded as failed and does not count.
6. The pass threshold is set before the first run, from the largest agency HiveAR expects to serve and that agency's nightly window. If both stacks clear it, throughput does not decide the question, and the decision rests on the other grounds in WAX-0025 and the proposal.

## Open before the first run

- The pass threshold, which needs the largest target agency's account count and nightly window.
- Whether to add a second business command, such as interest accrual, alongside payment posting.
- The benchmark machine.
- Andy Troiano's agreement to the method, and whether he wants to tune the Python side.

## Results

No runs yet. Each run is recorded in [results](results/README.md), and this section will summarize them.

## Attribution

The Python side is adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Parts the bake-off does not measure, such as the blue-green release drill, remain in his fork. The [python README](python/README.md) lists what was kept, what was changed, and what was left out.
