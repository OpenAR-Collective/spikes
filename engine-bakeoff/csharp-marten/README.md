# C# on .NET with Marten (champion)

This folder holds the champion side of the [engine bake-off](../README.md): the Wax command path written in C# on .NET 10, the current LTS release, with Marten 9 as the event store on PostgreSQL, the stack [WAX-0025 Primary Programming Language](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0025-primary-programming-language.md) adopts.

Status: built and tested at parity with the Python side. It implements the same four commands with the same rules, writes the same tables, and computes byte-for-byte the same event hashes. Its 24 tests pass. The throughput harness and the batch and real-time workloads come next.

## What it is

The code is in three projects, plus a test project:

- `src/Wax` is the framework: the event store over Marten, the hash computation that stands in for the Wax Security Module, the command dispatcher, the chain verifier, and the schema migrator.
- `src/HiveAR` is the accounts receivable slice, ported rule for rule from the prototype's `hivear/` package: placing an account, opening a dispute, posting a payment across accounts with the allocation and waterfall rules of HIVE-0007, reversing a payment, and the three read models.
- `src/Bakeoff.Host` is the command line, which mirrors the Python side's management commands: `migrate`, `seed-demo`, `run-projections`, `projections`, `verify-chain`, and `about`.
- `tests/Bakeoff.Tests` holds the tests.

## How WAX-0001 maps onto Marten

Marten has its own event table, `mt_events`, and it does not have the enriched columns that [WAX-0001 Enriched Event Sourcing](https://github.com/OpenAR-Collective/platform-design/blob/main/decisions/wax/WAX-0001-enriched-event-sourcing.md) gives `core.event`. The C# side keeps every one of those fields, and they sit in three places:

| WAX-0001 column | Where it is kept here |
| --- | --- |
| `global_sequence_number` | Marten's `seq_id` |
| `aggregate_pk_ref` | Marten's `stream_id` |
| `aggregate_sequence_number` | Marten's `version` |
| `event_type` | Marten's `type`, mapped to the same names the Python side uses, such as `account.placed` |
| `recorded_at` | Marten's `timestamp`, set by the server |
| `event_hash`, `prior_event_hash` | The event's headers, a jsonb column Marten stores beside the body |
| Every other column: `aggregate_type`, `schema_version`, `prior_value`, `new_value`, `occurred_at`, `actor_pk_ref`, `actor_type`, `reason_code_fk`, `source`, `correlation_pk_ref`, `correlation_type`, `metadata` | The event body, a jsonb column, under the same snake_case names |

Marten's tables live in the `core` schema. The authoritative state tables and the read models live in `ar`, and their columns and constraints are the Python side's own.

## How a command runs

The dispatcher opens one connection and one transaction per command and hands the handler a Marten session enlisted in that transaction. The handler does three things in order:

1. It reads what it needs and takes its row locks in a single round trip. For a payment, that one batch checks whether the payment exists, locks the accounts' state rows with `SELECT FOR UPDATE` in key order, reads their buckets, and reads the head of each account's stream. The head is the stream's version and its last event's hash.
2. It applies the business rules in memory. They are ported line for line from the prototype, with the same checks in the same order and the same refusal codes.
3. It records each event and queues the state writes. Recording checks the expected version against the stream head, computes the hash over the fields WAX-0013 lists, and puts the hash and the prior hash in the event's headers. Marten then sends every event and every state write in one batch, and the dispatcher commits.

A conflict with a concurrent writer is retried from the start, up to three attempts, as on the Python side. The conflicts are a stale version, a stream or row that now exists, or a deadlock.

Marten appends in its quick mode, Marten 9's default (`QuickWithServerTimestamps`), which assigns versions and sequence numbers on the server.

## How it matches the Python side

- **The same rules.** The handlers, the allocation, and the waterfall are ports of the prototype's code, and the payment tests are ports of its tests.
- **The same state.** `src/HiveAR/Schema/0001_ar.sql` creates the same `ar` tables, columns, and constraints as the Python side's migrations. The read model tables keep the Python side's names, including the `_v2` suffix, so one harness can check both.
- **The same hashes.** `EventHasher` writes the canonical form exactly as Python's `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)` does. A test computes the hashes for the vectors in [shared/hash-vectors.json](../shared/hash-vectors.json), which the Python side's own `wax/hashing.py` produced, and requires an exact match. The vectors cover non-ASCII text, characters outside the Basic Multilingual Plane, control characters, key order, and time zone offsets.
- **The same protections.** A trigger refuses UPDATE, DELETE, and TRUNCATE on `core.mt_events` for every role. The worker connects as `hivear_app`, a member of `wax_runtime`, which has no UPDATE on the event table and cannot change the schema.
- **The same database.** The Compose file uses the same PostgreSQL 17 image by digest, the same settings, and the same roles.

## Where it differs, and why

- **Reads and writes are batched.** The Python side sends each query as it goes. Here the reads go out in one round trip and the writes in another, because that is how Npgsql and Marten are meant to be used. Nothing stops the Python side from doing the same; it is allowed tuning under the bake-off's rules.
- **Reason codes are cached.** The Python side looks a reason code up for every event. Here the registry is read once per process and again only when a code is missing. The Python side could do the same.
- **Writing state outside a command is a compile-time error, not a runtime one.** State rows are saved only through a `CommandContext`, which only the dispatcher can construct. The Python side checks the same rule when the code runs.
- **Marten's daemon replaces the prototype's projection reader.** Marten 9 holds its high-water mark below any sequence number that a live transaction may still fill, and it skips a gap only once no transaction could fill it. The four reader tests ported from the prototype pass against it. They cover an open transaction holding a lower number, per-aggregate order, a rolled-back number, and one failing projection stopping alone. Projections here stop on an error instead of skipping the event, which is not Marten's default.
- **An event type a projection does not subscribe to is passed over silently.** The prototype's reader stops when an event on an aggregate it covers has no handler, so a new event type cannot slip past a read model. Marten delivers only the event types a projection lists. The same protection could be added here as a startup check; it is not built, because it does not affect throughput.
- **No position token.** The prototype returns a position after each command so that a screen can wait for its own write. That belongs to the proposal rather than to the Wax decisions, and the bake-off does not measure it.

## Tests

The 24 tests run against a real PostgreSQL, each test class in a fresh database:

- **Event store (7):** an account's first event and its state row are written together, state cannot be written outside a command, events cannot be updated or deleted, a stale expected version is refused, a forged link in the chain is detected, and the hashes match the Python side's vectors.
- **Payments (10):** the nine payment tests of the prototype, plus one showing that a retried command is answered as success and that a different command under the same key is refused.
- **Concurrency (2):** 25 payments against one account at once serialize on its row lock, with no money lost and an unbroken chain, and 30 payments over two accounts in both orders do not deadlock.
- **Read models (1):** after the daemon catches up, every read model agrees with the authoritative state, and every chain verifies.
- **Reader (4):** the reader behaviors listed above.

These prototype reader tests have no counterpart here: the reader refusing to run inside another transaction, and its high-water mark resetting after a restore. Both test the prototype's own reader, and Marten manages its own connections and marks.

## Running it

The Compose file brings up the database, applies the schema, and starts the projection worker:

```
docker compose up -d worker
docker compose run --rm tools
docker compose run --rm --entrypoint "dotnet Bakeoff.Host.dll" worker verify-chain
docker compose down -v
```

The second command builds the test image and runs the 24 tests. For a faster loop while editing, `build.sh` runs any `dotnet` command in the pinned SDK container. Set `NETWORK` to reach the Compose database:

```
./build.sh dotnet build
NETWORK=hivear-bakeoff-csharp_default ./build.sh dotnet test
```

On Windows, run these from WSL, or from Git Bash with `MSYS_NO_PATHCONV=1` set, so container paths are not rewritten. The build uses the .NET 10 SDK image pinned by digest, so no .NET SDK is needed on the host.
