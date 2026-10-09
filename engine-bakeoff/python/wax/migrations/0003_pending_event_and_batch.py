"""The Pending Event Register (WAX-0027) and the batch job tables (WAX-0024),
as the engine bake-off's WORKLOADS.md specifies them.

Foundation-written for the bake-off; not part of Andy Troiano's prototype.
Hand-written SQL, as the prototype's core migrations are, with explicit
grants: the runtime role may read and write these operational tables but,
as everywhere in core, may not delete from them.
"""

from django.db import migrations, models

FORWARD = """
CREATE TABLE core.pending_event (
    pending_event_pk uuid PRIMARY KEY,
    aggregate_pk_ref uuid NOT NULL,
    aggregate_type text NOT NULL,
    target_event_type text NOT NULL,
    due_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
    scheduled_by_event uuid NOT NULL,
    fired_at timestamptz,
    CHECK ((status = 'fired') = (fired_at IS NOT NULL))
);
CREATE INDEX idx_pending_event_status_due_at ON core.pending_event (status, due_at);

CREATE TABLE core.batch_job_run (
    batch_job_run_pk uuid PRIMARY KEY,
    job_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);

CREATE TABLE core.batch_job_chunk (
    batch_job_chunk_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_job_run_fk uuid NOT NULL REFERENCES core.batch_job_run (batch_job_run_pk),
    chunk_sequence_number integer NOT NULL,
    range_start uuid NOT NULL,
    range_end uuid NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'claimed', 'complete', 'failed')),
    claimed_by_instance text,
    claim_expires_at timestamptz,
    completed_at timestamptz,
    error_detail text,
    UNIQUE (batch_job_run_fk, chunk_sequence_number)
);
CREATE INDEX idx_batch_job_chunk_claim ON core.batch_job_chunk (batch_job_run_fk, status, chunk_sequence_number);

GRANT SELECT, INSERT, UPDATE ON core.pending_event, core.batch_job_run, core.batch_job_chunk TO wax_runtime;
"""

REVERSE = "DROP TABLE core.batch_job_chunk; DROP TABLE core.batch_job_run; DROP TABLE core.pending_event;"


class Migration(migrations.Migration):
    dependencies = [("wax", "0002_event_model_state")]
    operations = [
        migrations.RunSQL(FORWARD, REVERSE),
        migrations.CreateModel(
            name="PendingEvent",
            fields=[
                ("pending_event_pk", models.UUIDField(primary_key=True, serialize=False)),
                ("aggregate_pk_ref", models.UUIDField()),
                ("aggregate_type", models.TextField()),
                ("target_event_type", models.TextField()),
                ("due_at", models.DateTimeField()),
                ("status", models.TextField(default="pending")),
                ("scheduled_by_event", models.UUIDField()),
                ("fired_at", models.DateTimeField(null=True)),
            ],
            options={"db_table": 'core"."pending_event', "managed": False},
        ),
    ]
