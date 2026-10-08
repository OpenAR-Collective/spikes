# Python (challenger)

This folder holds the challenger side of the [engine bake-off](../README.md): the Wax command path in Python, adapted from the HiveAR prototype Andy Troiano published with his proposal to build Wax in Python on Django.

Status: imported and trimmed. It builds, its 23 retained tests pass, and `verify_chain` verifies the chains it writes. The throughput harness comes after the method is agreed.

## Provenance

- Source: [atroiano/platform-design](https://github.com/atroiano/platform-design/tree/361b6aa/prototype), commit 361b6aa (2026-10-08), directory `prototype/`.
- Author: Andy Troiano.
- License: Apache License 2.0, as the prototype's README states.

The first commit that adds a file from the prototype adds it unchanged and is authored as Andy Troiano, so the history shows exactly what was changed afterward. A file changed after that carries a header comment saying it was adapted from his prototype and modified, as section 4(b) of the Apache License requires.

## What was kept, changed, and left out

The rule was to keep everything the Wax write path and its read side need, as Andy wrote it, and to leave out what the bake-off does not measure. Each file below sits at the same relative path it had under `prototype/`.

### Kept unchanged

These 42 files are byte-for-byte copies of the originals.

- The Wax framework: `wax/store.py` (the event store), `wax/hashing.py`, `wax/commands.py` (command dispatch), `wax/projections.py` (the projection reader and its high-water mark), `wax/models.py`, `wax/release.py`, `wax/apps.py`, both `wax/migrations/`, and the management commands `run_projections`, `projections`, and `verify_chain`.
- The HiveAR slice: `hivear/commands.py`, `hivear/handlers.py`, `hivear/allocation.py` (payment allocation and the waterfall), `hivear/currency.py`, `hivear/models.py`, `hivear/projections.py`, all five `hivear/migrations/`, and the `seed_demo` command that loads a sample bucket registry.
- The tests for those pieces: `tests/conftest.py`, `tests/test_event_store.py`, `tests/test_payments.py`, and `tests/test_projection_reader.py`.
- Supporting files: `manage.py`, `pytest.ini`, `.dockerignore`, `config/__init__.py`, and `deploy/initdb/10-runtime-role.sh`, which creates the runtime database role.

### Changed

Each of these six files carries a header comment naming its source and its change.

| File | Change | Reason |
| --- | --- | --- |
| `config/settings.py` | Removed the settings for the HTTP API, the frontend, the preview port, and the migration compatibility checker. | The bake-off has no web layer. The database, logging, and time settings are unchanged. |
| `hivear/apps.py` | No longer imports `hivear.smoke`. | The smoke probes belong to the blue-green release procedure, which stays in the prototype. |
| `Dockerfile` | One Python stage; the default process is the projection reader. | The frontend build, the OpenAPI export, static files, and gunicorn served the HTTP layer. |
| `requirements.txt` | Removed `django-ninja`, `gunicorn`, and `whitenoise`. | They served the HTTP API and the frontend. |
| `constraints.txt` | Removed the pins for the same three packages. | Every other pin is unchanged. |
| `docker-compose.yml` | One PostgreSQL 17, a one-shot `migrate` service, the projection `worker`, and a `tools` service for tests and one-off commands. | Replaces the blue-green pair, pgBouncer, and the Caddy proxy. No ports are published to the host. |

### Left out

These stay in Andy's fork at commit 361b6aa.

- The blue-green release tooling: `deploy/bluegreen.sh`, `deploy/drill.sh`, `deploy/compat_test.sh`, `deploy/drill-next-release/`, `wax/migration_safety.py`, `wax/migration_operations.py`, `wax/smoke.py`, `hivear/smoke.py`, the `deployment`, `check_migrations`, and `smoke_check` commands, and `tests/test_deployment.py`.
- The HTTP layer: `hivear/api.py`, `config/urls.py`, `config/wsgi.py`, `config/middleware.py`, `tests/test_frontend_api.py`, and `tests/test_projection_and_api.py`, which exercises the read side through the API.
- The React frontend (`frontend/`), the Caddy proxy (`Caddyfile`), the `Makefile`, `.env.example`, the prototype's `.gitignore`, and its README.

### Judgment calls at the edges

- `wax/release.py` is kept even though its main job is the blue-green schema interlock, because `dispatch` and the projection reader call it. Its cost in the command path is one cached lookup every five seconds per process. Andy, as the Python side's advocate, may remove it, and a result should record whether it ran.
- pgBouncer is out of the base Compose file because connection pooling is a deployment choice, not part of the engine. The `DATABASE_POOLED` setting is kept, so a run can put it back as tuning.
- `seed_demo` is kept because the payment-posting slice needs a bucket registry, and the prototype has no other way to load one.
- The `projections` management command is kept because it reports the reader's position, which the read-lag measurement needs.
- Andy's draft has no command-line entry point for placing accounts or posting payments; the tests, `manage.py shell`, and the coming harness dispatch commands in-process.

## Running it

From this folder, with Docker:

```
docker compose build
docker compose up -d db
docker compose run --rm tools
```

That runs the 23 retained tests. To run the engine against a real database:

```
docker compose up -d migrate worker
docker compose run --rm tools python manage.py verify_chain
docker compose exec worker python manage.py projections status
docker compose down -v
```

On Windows, run these from WSL or set `MSYS_NO_PATHCONV=1` in Git Bash, so container paths are not rewritten.

Checked on 2026-10-08 with Docker Desktop: the image builds, all 23 retained tests pass, and after 50 accounts and 51 payments were dispatched through `manage.py shell`, `verify_chain` verified all 101 aggregate chains and the worker, running as the runtime role, had brought all three read models up to the head of the stream.
