# C# on .NET without Marten (variant)

This folder holds a variant of the [engine bake-off](../README.md)'s C# side: the same Wax command path in C# on .NET 10, with Marten removed entirely. Events go into `core.event`, the table [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md) specifies, and a reader ported from Andy Troiano's prototype keeps the read models and runs the rules. The only database library is Npgsql, the same version the Marten side uses.

Status: built, tested, and measured. It runs every workload in [WORKLOADS.md](../WORKLOADS.md), the bulk ones included, and its 38 tests pass. It ran in the second and third passes of round 1, tuned with `--projection-workers 3 --rule-workers 16` except for two untuned real-time runs, and all 22 of its runs passed every gate that applied to them. The results are in the [round 1 report](../REPORT-ROUND-1.md).

## Why this side exists

[WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md) chose C# largely for Marten, and five Wax decisions mention Marten: three name it as the event store's implementation, one relies on its upcasters, and one cites it in choosing Wolverine. Before this side, the bake-off's two C# sides both depended on Marten. The champion appends through Marten. The direct-append variant writes Marten's own tables with plain SQL, but Marten's serializer still writes the event bodies, and Marten's daemon still keeps the read models and runs the rules.

This side answers a different question: what Wax would have to own if it did not use Marten, and how that version performs. It starts from the champion's code ([csharp-marten](../csharp-marten/README.md) at 7088e4d, copied unchanged in its own commit so the next diff shows exactly what removing Marten changed) and keeps everything that is not Marten: the handlers, the allocation and waterfall, the hashes, the batch runner, the seeder's approach, the bulk writer, and the driver. It replaces three things.

1. **The event store.** Events are rows in `core.event`, with every column of WAX-0001 as a real column, rather than a JSON body and headers in `mt_events`.
2. **The reader.** A C# port of Andy Troiano's projection reader replaces Marten's async daemon.
3. **The rule runner.** The Python side's partitioned rule runner, which reads with the same reader's high-water mark, replaces the Marten subscription.

## The event store

`src/Wax/Schema/0001_core.sql` is adapted from the prototype's own core migration, so `core.event` here has the same columns, constraints, and triggers as on the Python side.

- **The database numbers events.** A `BEFORE INSERT` trigger forces a transaction id, then takes the next value of `core.event_global_sequence_seq` (a `CACHE 1` sequence) as `global_sequence_number`, and stamps `recorded_at` with `clock_timestamp()`. A caller cannot supply either value, as WAX-0001 requires, and a test checks that.
- **Events are immutable.** Triggers refuse `UPDATE`, `DELETE`, and `TRUNCATE` on `core.event` for every role, the owner included, and the runtime role is granted only `INSERT`.
- **There is no stream table.** Marten keeps each stream's version in `mt_streams` and updates that row on every append. Here the head of an aggregate's stream is read straight from `core.event`, with one backward probe of the unique index on `(aggregate_pk_ref, aggregate_sequence_number)`, in the same round trip as the command's row locks. That index is also the backstop for the version check.
- **One insert per event.** `NewEvent.Create` shapes each event and computes its hash, chained to the aggregate's previous event, and `EventStore` writes it. The single-command path and the bulk writer both build their events there, so they cannot store different ones. A command's events and state writes still go out as one Npgsql batch, inside the command's one transaction, as on the other C# sides.

The sequence takes the name WAX-0001 gives it. The Python side keeps the prototype's name, `core.seq_event_global_sequence`; nothing outside each side's own code reads it.

## The reader

`src/Wax/Projections/` is a port of the prototype's `wax/projections.py`, and each file says so in its header.

- **The high-water mark** is the prototype's: a reader samples the sequence position and then takes a transaction id of its own, and it reads up to that position only once every older transaction has finished. It never skips an event whose transaction committed late, and it waits for no timeout. It forgets everything when the database restarts, is swapped, or shows a lower position, which is what a restore or a failover looks like.
- **Checkpoints** are rows in `core.projection_checkpoint`, one per projection version. Each page of events, its read model writes, and the checkpoint's advance commit in one transaction. Several readers can share the projections, because each claims a projection's row with `SELECT ... FOR UPDATE SKIP LOCKED`, so `run-engine --projection-workers J` applies here as it does on the Python side. One reader advances its projections one after another, as the prototype's does; Marten's daemon runs one agent per projection at once, so three readers here is the closer match to the Marten side.
- **An unknown event type stops a projection.** Each projection names the aggregate types it covers, and an event on one of them must have a handler or be listed as ignored. Any other type stops that projection at that event and records why, and a later release that knows the type picks it up. Marten passes such an event over silently, which the champion's README notes as a difference.
- **A failing handler stops one projection, not the reader.** The projection records the event it stopped at and the error, and the others carry on. A retry clears the record, and the projection resumes at the same event.
- **Definition digests** pin each projection version's definition, so two releases cannot both write one version that they define differently.

Three changes from the prototype's reader, all for speed or for C#:

1. A handler queues its SQL rather than running it, so a page of up to 500 events' read model writes goes to PostgreSQL as one batch inside a savepoint, as Marten's daemon also does. If the batch fails, the page is rolled back to the savepoint and replayed one event at a time to find the event that fails, as in the original.
2. A page is read only for the aggregate types the projection covers. The prototype reads every event and passes over the ones it does not cover, which gives the same result with more rows read.
3. The definition digest covers each projection's declared shape: its aggregate types, the event types it handles and ignores, and the methods that handle them. The prototype digests the handlers' source text, which a compiled assembly does not keep, so here a change of logic alone has to be made a new version by discipline.

The prototype's release checks and heartbeat for its blue-green drill are left out, because the bake-off does not run the drill.

One property of the prototype's design carries over and is worth knowing: the high-water mark waits on the oldest transaction that holds a transaction ID anywhere on the PostgreSQL server, not only those writing `core.event`. A long transaction that has written anything or locked a row, in any database on the same server, holds every reader back until it ends; a read-only transaction does not, whatever its isolation level. The prototype's own Compose file ends a session left idle in a transaction after five minutes for this reason, and so does this one.

## The rules

`src/Wax/Rules/RuleRunner.cs` follows the Python side's `wax/rules.py`, which the Foundation wrote for the bake-off. Each of K partitions takes the aggregates whose key hashes to it, reads the events its rules care about up to the high-water mark, runs each action as a command, and advances its position only after the actions have committed, so delivery is at least once and each aggregate's events stay in order. `--rule-workers K` sets the number of partitions and the register poller's parallelism, as on the other sides.

This is a different design from the champion's, not only a different library. Marten's subscription hands the rule runner a page at a time and runs that page's aggregates in parallel, so the page waits for its slowest aggregate. Here each partition moves on its own. Any difference in rule latency between the two C# sides therefore reflects both changes.

The partition is `(hashtext(aggregate_pk_ref::text) & 2147483647) % K`, the same expression as on the Python side.

## What Marten did that Wax now owns

| Marten provided | Here |
| --- | --- |
| The event and stream tables, and their migrations | `core.event`, its sequence, and its triggers, in hand-written SQL |
| Appending, with a stream version check | One `INSERT` per event in the command's batch; the version check against the head read with the row locks, and the unique index behind it |
| Serializing event bodies | None needed: every field is a column |
| The async daemon, its high-water mark, and gap handling | The prototype's reader and high-water mark, ported |
| Projection progress | `core.projection_checkpoint` |
| Pausing a projection on error | Stopping at the event and recording why, then a retry |
| Subscriptions | The partitioned rule runner |
| Upcasting | Not built; nothing here needs it yet |
| Rebuilding a projection | Not built; a new version starts at zero and builds beside the old one |

The new code is about 1,200 lines of C# and a 190-line SQL schema: the reader is about 700 lines, the event store 240, the rule runner 210, and the stored event's type 60. The `src` tree is about 780 lines longer than the champion's in all.

## How it matches the other sides

- **The same commands and rules.** Everything under `src/HiveAR` is the champion's code, with these edits: the read models are Wax projections instead of Marten ones, with the same SQL; the rules take a stored event instead of a Marten one; one handler query reads `core.event` instead of `mt_events`; and the seeder and the bulk writer write `core.event` and the projection checkpoints instead of Marten's tables.
- **The same state and hashes.** The `ar` tables, their constraints, and the hash computation are the champion's, and the tests check the hashes against [shared/hash-vectors.json](../shared/hash-vectors.json).
- **The same harness views.** `core.bakeoff_event_log` maps `core.event` column for column, and `core.bakeoff_progress` has a row per projection version and per rule runner partition, as on the Python side.
- **The same database and limits.** The Compose file uses the same PostgreSQL image, settings, and roles, under its own project name, `hivear-bakeoff-csharp-wax`, so its database and network never meet the other sides'.
- **The same driver.** `Bench.cs` is the champion's, and prints the same JSON, with `append` reported as `wax` and `hashing` as `on` or `off`.
- **The same diagnostics.** `WAX_HASH=off` (in Compose, `BAKEOFF_HASH=off`) stores a fixed placeholder hash and skips the read of the aggregate's previous hash, exactly as on the Marten sides; see `src/Wax/Hashing/EventHashing.cs`. A run with it cannot pass verify-chain and is a diagnostic only.

## Tests

The 38 tests run against a real PostgreSQL, each test class in a fresh database. They are the champion's 32 with these changes:

- **Event store (9):** the champion's seven, plus a test that `TRUNCATE` is refused, and a test that the store assigns `global_sequence_number` and `recorded_at` and ignores any value a caller supplies.
- **Reader (8):** the prototype's reader tests, ported against this reader: an uncommitted lower sequence is never skipped; one aggregate's events arrive in its own order; a rolled-back sequence number does not hold a projection back; a failing projection stops alone and resumes; an unknown event type stops a projection until a release that knows it is adopted; an ignored type is passed over on purpose; and the mark is forgotten when the database goes back in time. The eighth is new: two readers sharing a projection apply each event exactly once.
- **Read models, workloads, seed and batches, rules, and bulk:** the champion's tests, with the projection reader in place of Marten's daemon and two rule runner partitions in place of the subscription.

The prototype's test that the reader refuses to run inside another transaction has no counterpart here, because the C# reader opens its own connections and cannot be handed one. Because the high-water mark waits on the oldest transaction holding a transaction ID anywhere on the server, and xunit runs test classes at the same time, a test that waits for the reader retries for a few seconds rather than expecting the first pass to finish.

## Running it

The same as the champion, from this folder:

```
docker compose up -d worker
docker compose run --rm tools
docker compose run --rm --entrypoint "dotnet Bakeoff.Host.dll" worker verify-chain
docker compose down -v
```

For a faster loop while editing, `build.sh` runs any `dotnet` command in the pinned SDK container, and `NETWORK` joins this side's Compose network:

```
./build.sh dotnet build
NETWORK=hivear-bakeoff-csharp-wax_default ./build.sh dotnet test
```

On Windows, run these from WSL, or from Git Bash with `MSYS_NO_PATHCONV=1` set. To look around the database with a desktop client, `docker-compose.browse.yml` publishes it on 127.0.0.1:5434, so it can run beside the Marten side's 5433.

## Attribution

The event store schema, the reader, its high-water mark, its checkpoints, and its tests are adapted from the HiveAR prototype that Andy Troiano published with his proposal, at [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype) (commit 361b6aa, directory `prototype/`), under the Apache License 2.0. Each adapted file names its source and says what was changed.
