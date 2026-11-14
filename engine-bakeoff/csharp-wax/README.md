# C# on .NET without Marten (variant)

This folder holds a variant of the [engine bake-off](../README.md)'s C# side: the same Wax command path in C# on .NET 10, with Marten removed entirely. Events go into `core.event`, the table [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md) specifies, and a reader ported from Andy Troiano's prototype keeps the read models and runs the rules. The only database library is Npgsql, the same version the Marten side uses.

Status: built, tested, and measured. It runs every workload in [WORKLOADS.md](../WORKLOADS.md), the bulk ones included, and its 41 tests pass in all four state modes. It ran in the second and third passes of round 1, tuned with `--projection-workers 3 --rule-workers 16` except for two untuned real-time runs, and all 22 of its runs passed every gate that applied to them. The results are in the [round 1 report](../REPORT-ROUND-1.md).

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

The 40 tests run against a real PostgreSQL, each test class in a fresh database. They are the champion's 32 with these changes:

- **Event store (9):** the champion's seven, plus a test that `TRUNCATE` is refused, and a test that the store assigns `global_sequence_number` and `recorded_at` and ignores any value a caller supplies.
- **Reader (8):** the prototype's reader tests, ported against this reader: an uncommitted lower sequence is never skipped; one aggregate's events arrive in its own order; a rolled-back sequence number does not hold a projection back; a failing projection stops alone and resumes; an unknown event type stops a projection until a release that knows it is adopted; an ignored type is passed over on purpose; and the mark is forgotten when the database goes back in time. The eighth is new: two readers sharing a projection apply each event exactly once.
- **Read models, workloads, seed and batches, rules, and bulk:** the champion's tests, with the projection reader in place of Marten's daemon and two rule runner partitions in place of the subscription.
- **Twin (3):** the EF Core mode, the batched mode, and the fair batched mode, each against the plain mode, described below.

The prototype's test that the reader refuses to run inside another transaction has no counterpart here, because the C# reader opens its own connections and cannot be handed one. Because the high-water mark waits on the oldest transaction holding a transaction ID anywhere on the server, and xunit runs test classes at the same time, a test that waits for the reader retries for a few seconds rather than expecting the first pass to finish.

## The EF Core mode (harness side csharp-wax-ef)

[WAX-0003 Database Platform](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0003-database-platform.md) routes all non-event data access through EF Core, and no side of round 1 used it. To measure what it costs, this side has a second state mode. With `WAX_STATE=efcore`, the command handlers read and write authoritative state through EF Core instead of plain SQL. That state is `ar.account`, its buckets, payments, tasks, and the Pending Event Register. Everything else is the same code in the same image: the event store, the hash chain, the reader, the rules, the seeder, the bulk writer, and the driver. The harness runs this mode as the side `csharp-wax-ef`, in this side's Compose project and from its seed template. Without the variable, the side runs plain SQL as before. Foundation-written for round 2.

- **Version and transaction.** EF Core 9, with the Npgsql provider 9.0.4 running on the same Npgsql 9.0.4. A DbContext from a pooled factory is attached to the command's own connection (`SetDbConnection`) and enlisted in its transaction (`UseTransaction`).
- **Atomic writes.** The dispatcher writes the events through the event store, then calls `SaveChanges`, then commits. Events and state therefore commit or roll back together (WAX-0002). A `CommandContext` hook, run just before the commit, makes that possible.
- **The row lock before deciding.** LINQ has no locking operator, so each lock is an EF query rooted in raw SQL that ends in `FOR UPDATE`, in key order, before any rule runs (WAX-0013). A register entry is claimed with `FOR UPDATE SKIP LOCKED` the same way.
- **Buckets load in a second query, never through an `Include` on the locking query.** That `Include` is the idiomatic EF way, and it is wrong here, and the concurrency tests caught it losing money. Under READ COMMITTED, a statement that waits for a row lock returns the locked row's latest version. It still reads every joined row from the snapshot it took before the wait, so concurrent payments overwrote each other's bucket balances. A separate statement takes a fresh snapshot after the lock is granted, as the plain mode's separate bucket read does.
- **The version check.** It is unchanged: against the stream head, which the event store reads. `aggregate_sequence_number` is also mapped as an EF concurrency token.
- **Writes.** Handlers change tracked entities and add new ones; the domain state classes are the EF entities themselves.
- **Not EF's schema.** The model maps onto the hand-written tables as they stand, with no EF migrations and no migrations history table, so both modes share one seed.
- **Round trips.** EF Core neither batches separate queries nor carries the event store's statements in `SaveChanges`. A payment therefore makes about six round trips where the plain mode makes two:
  1. the existence check;
  2. the locks;
  3. the buckets;
  4. the stream heads;
  5. the events;
  6. `SaveChanges`.

  The measurements report this cost.
- **What it does not cover.** The bulk workloads are excluded on `csharp-wax-ef`, because the bulk writer is set-based SQL with no EF Core equivalent. The projection reader and the rule runner use plain SQL in both modes.
- **The twin test.** `TwinTests` runs 23 commands through both modes, on two databases copied from one migrated template, each with a clock that steps one millisecond per read. It requires:
  - the same outcomes;
  - byte-identical events, hashes and chains included;
  - identical state.

  The register's `fired_at` is the one exception: the plain mode stamps it with the server's clock and the EF mode with the client's.

### Tuning

The EF mode is tuned the way a careful EF team would tune it, and only in the ways the test's design allows. It uses no third-party bulk library and no raw-SQL state updates. Development runs before the recorded mini-pass used the untuned mode; the recorded mini-pass uses the tuned one. In order:

1. **DbContext pooling** was there from the start. Each command rents a context from a `PooledDbContextFactory` instead of building one.
2. **Compiled queries.** Every hot-path LINQ query is compiled once per process with `EF.CompileAsyncQuery`, so a command skips EF's per-call translation and cache lookup. These cover the existence checks for accounts, payments, tasks, and register entries; the bucket load; and the bucket-definition lookups.
   - EF Core cannot compile a query rooted in raw SQL. The three locking queries (accounts, a payment, and a register entry) therefore stay ordinary queries, which EF translates once and keeps in its query cache.
3. **No tracking for what is never written.** Bucket definitions are reference data, so the bucket load takes their code and priority through a projection in the same query. Only the bucket rows themselves are tracked, and the definitions lookup in PlaceAccount uses `AsNoTracking`. Existence checks project scalar values and track nothing.
4. **A compiled model.** `Data/CompiledModel` was generated with `dotnet ef dbcontext optimize` (EF Core tools 9.0.1), and the context factory uses it. It mostly saves the one-time cost of building the model at start-up, not per-command work. It must be regenerated whenever the mapping changes, with the design package `Microsoft.EntityFrameworkCore.Design`, which the host project references at build time only. The `jsonb` converter for a payment's distribution is a named class, `JsonObjectConverter`, so that the compiled model can refer to it.

After the tuning, all 39 tests pass in both modes, the twin test included.

## The batched mode (harness side csharp-wax-batched)

`WAX_STATE=batched` runs the same handlers with the statement shapes the python-direct side uses, so that the two languages can be compared on the same SQL. When `WAX_STATE` is unset, plain mode sends exactly the statements it always has.

- **Events.** A command's events go out as one multi-row `INSERT INTO core.event ... VALUES`, in the order they were recorded, at the place in the batch where the first of them was queued. PostgreSQL inserts the rows in the order they are listed, so the trigger numbers them as plain mode's one statement per event does.
- **State.** Every account row a command changes goes out in one `UPDATE ar.account ... FROM unnest(...)`, and every bucket row in one `UPDATE ar.account_balance_bucket ... FROM unnest(...)`, after the command's other writes and in the same batch. The last value of each row wins, which is the value that plain mode's last statement for that row writes.
- **Placement.** An account's opening buckets go in as one multi-row `VALUES` list instead of plain mode's `unnest`, which cost more database time per placement in the EF Core pass.
- **Unchanged.** The reads and row locks, the round trips (one batch of reads and one batch of writes), the single-row inserts such as a payment, a task, or a register entry, the read models and the projection reader, and the bulk writer, which is set-based in every mode. The mode is process-wide, so the commands that the engine's rules dispatch take it too.
- **The twin test.** `TwinTests` runs the same 24 commands through plain and batched modes and requires identical events, every column but the random event key and the store's `recorded_at`, hashes and global order included, and identical state. Its last command is dispatched with the rule runner's attribution, as a rule would dispatch it.

All 40 tests pass in each of the three modes.

## The fair batched mode (harness side csharp-wax-fair-batched)

The code-shape pass found the batched mode slower than plain mode on the payment file, partly because its account update wrote six columns from six arrays even when a payment changed only one account's version. `WAX_STATE=fair-batched` keeps the batched mode's multi-row event insert and placement `VALUES` list, and sizes the state updates to what changed, as the python-direct side does:

- **Accounts.** An account whose only change is its version gets the version-only update: one such account is a single-row `UPDATE ar.account SET aggregate_sequence_number = ...`, and several are one `UPDATE ... FROM unnest(...)` of two arrays. An account with any other change, such as its status, dispute flag, accrual date, or queue, gets plain mode's single-row statement. The account remembers the values it was read with under its row lock, which is how the mode tells the two apart; an account it cannot compare gets plain mode's statement.
- **Buckets.** One changed bucket row is plain mode's single-row statement, and several are one set-based `UPDATE`.
- **The batched mode is unchanged**, so the code-shape pass stays reproducible from this tree.
- **The twin test** runs the same 24 commands through plain and fair batched modes, with the same requirements as for the batched mode.

All 41 tests pass in each of the four modes.

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
