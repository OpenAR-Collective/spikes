# Python (challenger)

This folder holds the challenger side of the [engine bake-off](../README.md): the Wax command path in Python, adapted from the HiveAR prototype Andy Troiano published with his proposal to build Wax in Python on Django, plus the Foundation-written code that the bake-off's [workloads](../WORKLOADS.md) need beyond his prototype.

Status: built to [WORKLOADS.md](../WORKLOADS.md). It builds, its 50 tests pass, and the commands the harness calls run end to end. No result has been recorded.

## Provenance

- Source: [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype), commit 361b6aa (2026-10-08), directory `prototype/`.
- Author: Andy Troiano.
- License: Apache License 2.0, as the prototype's README states.

The first commit that adds a file from the prototype adds it unchanged and is authored as Andy Troiano, so the history shows exactly what was changed afterward. A file changed after that carries a header comment saying it was adapted from his prototype and modified, as section 4(b) of the Apache License requires.

## Foundation-written code

Rob Grafrath's direction is to build the Python side to the best of the Foundation's ability, publish it, and invite Andy to review it and fix anything that was not built properly for his side. Everything below was written by the Foundation, in Andy's patterns, and is not his: a result that depends on it should say so.

- **The six commands** in `hivear/commands.py` and `hivear/handlers.py`: AccrueInterest, ChangeAccountStatus, ScheduleFutureEvent, FirePendingEvent, CreateTask, and AssignToQueue. Each takes the account's row lock, decides under it, and writes its event and state in one transaction through Andy's `dispatch`. FirePendingEvent claims its register row with `SELECT ... FOR UPDATE SKIP LOCKED`.
- **The schema additions:** `wax/migrations/0003_pending_event_and_batch.py` (the Pending Event Register and the batch tables, hand-written SQL as Andy's core migrations are), `hivear/migrations/0006_bakeoff_additions.py` (the account's `interest_accrued_through` and `queue_code`, the task table, and six reason codes), the matching model changes, and `bakeoff/migrations/0001_harness_views.py` (the harness's `core.bakeoff_event_log` view and a `core.bakeoff_progress` view).
- **The account summary projection's handling of the new events**, changed in place in `hivear/projections.py` as the specification directs.
- **Three framework pieces** in `wax/`: `rules.py`, a rule runner that delivers events at least once in global order from Andy's high-water mark and advances only after its actions commit; `register.py`, the register's poller; and `batch.py`, the batch coordinator and chunk claiming, with chunk boundaries computed in PostgreSQL's own key order.
- **The `bakeoff` package:** the derived keys and actors (`keys.py`), the five rules (`rules.py`), the bulk seed loader (`seed.py`), the batch and real-time drivers (`drivers.py`), `run-engine` (`engine.py`), a one-pass chain verifier (`verify.py`), and the command line (`__main__.py`).
- **The tests:** `tests/test_bakeoff_commands.py`, `tests/test_bakeoff_rules.py`, `tests/test_bakeoff_seed_and_batch.py`, and `tests/test_hash_vectors.py`.

Python uses worker processes for concurrency, its usual way to use more than one core. The drivers let `dispatch` make one attempt per call and retry a version conflict themselves, so that every retry is counted, as the specification's `retried` key requires; the behavior is the same as `dispatch`'s own retry loop.

## The harness's commands

The harness runs these in the `bench` and `engine` services. Each prints one line of JSON on stdout when it ends.

```
python -m bakeoff seed --accounts N
python -m bakeoff bench batch-payments --workers W
python -m bakeoff bench batch-accrual --workers W
python -m bakeoff bench realtime --workers W --warmup S --duration S --random-seed R
python -m bakeoff run-engine --seconds-per-day 2 --poll-interval-ms 250 --rule-workers 1 --projection-workers 1
python -m bakeoff verify-chain
```

`run-engine` starts J projection workers (Andy's own reader loop), K rule runner partitions, and one register poller, each in its own process. The rule runner and the projection reader sleep the poll interval when they find nothing to do.

## What was kept, changed, and left out of the prototype

The rule was to keep everything the Wax write path and its read side need, as Andy wrote it, and to leave out what the bake-off does not measure. Each file below sits at the same relative path it had under `prototype/`.

### Kept unchanged

These 37 files are byte-for-byte copies of the originals.

- The Wax framework: `wax/store.py` (the event store), `wax/hashing.py`, `wax/commands.py` (command dispatch), `wax/projections.py` (the projection reader and its high-water mark), `wax/release.py`, `wax/apps.py`, both of his `wax/migrations/`, and the management commands `run_projections`, `projections`, and `verify_chain`.
- The HiveAR slice: `hivear/allocation.py` (payment allocation and the waterfall), `hivear/currency.py`, all five of his `hivear/migrations/`, and the `seed_demo` command that loads a sample bucket registry.
- The tests for those pieces: `tests/conftest.py`, `tests/test_event_store.py`, `tests/test_payments.py`, and `tests/test_projection_reader.py`.
- Supporting files: `manage.py`, `pytest.ini`, `.dockerignore`, `config/__init__.py`, and `deploy/initdb/10-runtime-role.sh`, which creates the runtime database role.

### Changed

Each of these eleven files carries a header comment naming its source and its change.

| File | Change | Reason |
| --- | --- | --- |
| `config/settings.py` | Removed the settings for the HTTP API, the frontend, the preview port, and the migration compatibility checker, and installed the `bakeoff` app. | The bake-off has no web layer. The database, logging, and time settings are unchanged. |
| `hivear/apps.py` | No longer imports `hivear.smoke`. | The smoke probes belong to the blue-green release procedure, which stays in the prototype. |
| `hivear/commands.py` | PlaceAccount takes an optional `interest_accrued_through`; six commands added. | The workloads. |
| `hivear/handlers.py` | PlaceAccount records `interest_accrued_through`; six handlers added. | The workloads. Andy's other handlers are unchanged. |
| `hivear/models.py` | The account's two new columns and the Task model. | The workloads. |
| `hivear/projections.py` | The account summary handles `account.interest_accrued` and `account.status_changed` and ignores the other new account events. | The reader stops on an event type it neither handles nor ignores. |
| `wax/models.py` | Added the PendingEvent model. | The register. |
| `Dockerfile` | One Python stage; the default process is the projection reader; the image carries `bakeoff/`. | The frontend build, the OpenAPI export, static files, and gunicorn served the HTTP layer. |
| `requirements.txt` | Removed `django-ninja`, `gunicorn`, and `whitenoise`. | They served the HTTP API and the frontend. |
| `constraints.txt` | Removed the pins for the same three packages. | Every other pin is unchanged. |
| `docker-compose.yml` | One PostgreSQL 17, a one-shot `migrate` service, the projection `worker`, a `tools` service for tests, and the bake-off's `engine` and `bench` services. | Replaces the blue-green pair, pgBouncer, and the Caddy proxy. Limits and database settings come from the harness. No ports are published. |

### Left out

These stay in Andy's fork at commit 361b6aa.

- The blue-green release tooling: `deploy/bluegreen.sh`, `deploy/drill.sh`, `deploy/compat_test.sh`, `deploy/drill-next-release/`, `wax/migration_safety.py`, `wax/migration_operations.py`, `wax/smoke.py`, `hivear/smoke.py`, the `deployment`, `check_migrations`, and `smoke_check` commands, and `tests/test_deployment.py`.
- The HTTP layer: `hivear/api.py`, `config/urls.py`, `config/wsgi.py`, `config/middleware.py`, `tests/test_frontend_api.py`, and `tests/test_projection_and_api.py`, which exercises the read side through the API.
- The React frontend (`frontend/`), the Caddy proxy (`Caddyfile`), the `Makefile`, `.env.example`, the prototype's `.gitignore`, and its README.

### Judgment calls at the edges

- `wax/release.py` is kept even though its main job is the blue-green schema interlock, because `dispatch` and the projection reader call it. Its cost in the command path is one cached lookup every five seconds per process. Andy, as the Python side's advocate, may remove it, and a result should record whether it ran.
- pgBouncer is out of the base Compose file because connection pooling is a deployment choice, not part of the engine. The `DATABASE_POOLED` setting is kept, so a run can put it back as tuning.
- `seed_demo` is kept because the payment-posting slice needs a bucket registry, and the prototype has no other way to load one.
- The `projections` management command is kept because it reports the reader's position.
- The `bakeoff` package's `verify-chain` streams the event table once instead of querying each aggregate, as Andy's `verify_chain` does, because his takes a query per aggregate at the bake-off's sizes. Both apply the same three checks, and his command is still here.

## Running it

From this folder, with Docker. On Windows, run these from WSL or set `MSYS_NO_PATHCONV=1` in Git Bash, so container paths are not rewritten.

```
docker compose build
docker compose up -d db
docker compose run --rm tools
```

That runs the 50 tests. To run the workloads by hand on a small seed:

```
docker compose up -d migrate
docker compose --profile bakeoff up -d bench engine
docker compose exec bench python -m bakeoff seed --accounts 2000
docker compose exec bench python -m bakeoff bench batch-payments --workers 2
docker compose exec bench python -m bakeoff verify-chain
docker compose --profile tools --profile bakeoff down -v
```

Measured runs go through the [harness](../harness/README.md), which applies the shared limits and database settings.

Checked on 2026-10-08 with Docker Desktop: all 50 tests pass. A hand run on 2,000 seeded accounts posted the payment file and the accrual with no failures, ran the real-time mix for ten seconds, and left every reader at the head of the stream, every due first-contact entry fired with exactly one task, and every chain verified.
