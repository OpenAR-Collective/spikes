"""core schema: event store, reference values, projection checkpoints,
deployment state, and the runtime role's privileges.

Hand-written SQL rather than generated migrations, because the names, the
sequence, and the triggers are specified by WAX-0001 Enriched Event Sourcing,
WAX-0004 Schema Architecture, and WAX-0005 Naming Conventions.
"""

from django.db import migrations

FORWARD = """
CREATE SCHEMA IF NOT EXISTS core;

-- The application connects as a member of wax_runtime, never as the owner
-- that runs migrations (WAX-0016, SHARED-0010). The reference deployment
-- creates the role before the first migration; this covers every other path.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'wax_runtime') THEN
        CREATE ROLE wax_runtime NOLOGIN;
    END IF;
END $$;

CREATE TABLE core.reference_type (
    reference_type_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    label text NOT NULL
);

CREATE TABLE core.reference_value (
    reference_value_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_type_fk uuid NOT NULL REFERENCES core.reference_type (reference_type_pk),
    code text NOT NULL,
    label text NOT NULL,
    UNIQUE (reference_type_fk, code)
);

-- CACHE 1 is required: the projection reader's high-water mark relies on
-- values being handed out in order across sessions.
CREATE SEQUENCE core.seq_event_global_sequence CACHE 1;

CREATE TABLE core.event (
    event_pk uuid PRIMARY KEY,
    global_sequence_number bigint NOT NULL UNIQUE,
    aggregate_sequence_number integer NOT NULL CHECK (aggregate_sequence_number >= 1),
    event_type text NOT NULL,
    schema_version smallint NOT NULL,
    aggregate_pk_ref uuid NOT NULL,
    aggregate_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    actor_pk_ref uuid NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN ('user', 'system', 'integration', 'batch')),
    prior_value jsonb CHECK (prior_value IS NULL OR jsonb_typeof(prior_value) = 'object'),
    new_value jsonb CHECK (new_value IS NULL OR jsonb_typeof(new_value) = 'object'),
    reason_code_fk uuid REFERENCES core.reference_value (reference_value_pk),
    source text,
    correlation_pk_ref uuid,
    correlation_type text,
    metadata jsonb,
    prior_event_hash text,
    event_hash text NOT NULL,
    UNIQUE (aggregate_pk_ref, aggregate_sequence_number),
    CHECK ((correlation_pk_ref IS NULL) = (correlation_type IS NULL)),
    CHECK ((aggregate_sequence_number = 1) = (prior_event_hash IS NULL))
);

CREATE INDEX idx_event_event_type ON core.event (event_type);

-- The database, not the writer, assigns the global sequence and the recorded
-- time. Forcing a transaction id before nextval() is what makes the reader's
-- high-water mark exact (WAX-0001, Reading the Stream Without Skipping).
CREATE FUNCTION core.fn_event_before_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_current_xact_id();
    NEW.global_sequence_number := nextval('core.seq_event_global_sequence');
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER trg_event_before_insert
    BEFORE INSERT ON core.event
    FOR EACH ROW EXECUTE FUNCTION core.fn_event_before_insert();

CREATE FUNCTION core.fn_event_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'core.event is append-only';
END $$;

CREATE TRIGGER trg_event_before_update_or_delete
    BEFORE UPDATE OR DELETE ON core.event
    FOR EACH ROW EXECUTE FUNCTION core.fn_event_reject_mutation();

CREATE TRIGGER trg_event_before_truncate
    BEFORE TRUNCATE ON core.event
    FOR EACH STATEMENT EXECUTE FUNCTION core.fn_event_reject_mutation();

-- One row per projection version. A changed projection is a new version with
-- its own row and its own table (SHARED-0010). definition_digest pins the
-- handler set, so two releases cannot share a version they disagree about.
CREATE TABLE core.projection_checkpoint (
    projection_checkpoint_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    projection_name text NOT NULL UNIQUE,
    definition_digest text NOT NULL,
    last_global_sequence_number bigint NOT NULL DEFAULT 0,
    failed_at timestamptz,
    failure_kind text CHECK (failure_kind IN ('handler_error', 'unknown_event_type')),
    failed_global_sequence_number bigint,
    failure_detail text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Exactly one row: which color takes user traffic, which color is waiting to
-- be cut over to, and which color a rollback would return to (SHARED-0010).
CREATE TABLE core.deployment_state (
    deployment_state_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    is_singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (is_singleton),
    active_color text NOT NULL CHECK (active_color IN ('blue', 'green')),
    candidate_color text CHECK (candidate_color IN ('blue', 'green')),
    previous_color text CHECK (previous_color IN ('blue', 'green')),
    changed_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO core.deployment_state (active_color) VALUES ('blue');

-- One row per independently versioned component (Wax, HiveAR, each module):
-- the release whose migrations were last applied, the oldest code release
-- that schema still supports, and the release now serving users.
CREATE TABLE core.component_release (
    component_release_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    component text NOT NULL UNIQUE,
    schema_release text NOT NULL,
    minimum_code_release text NOT NULL,
    live_release text,
    changed_at timestamptz NOT NULL DEFAULT now()
);

-- Each color's worker reports in, so the release tooling can tell a color
-- whose worker is dead from one that is merely idle.
CREATE TABLE core.worker_heartbeat (
    worker_heartbeat_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    color text NOT NULL UNIQUE CHECK (color IN ('blue', 'green')),
    release text NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA core TO wax_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO wax_runtime;
GRANT INSERT ON core.event TO wax_runtime;
GRANT INSERT, UPDATE ON core.projection_checkpoint TO wax_runtime;
GRANT INSERT, UPDATE ON core.worker_heartbeat TO wax_runtime;
-- Tables a later release adds to core are readable without a grant in each migration.
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT ON TABLES TO wax_runtime;
GRANT SELECT ON SEQUENCE core.seq_event_global_sequence TO wax_runtime;
GRANT SELECT ON public.django_migrations TO wax_runtime;
"""

REVERSE = "DROP SCHEMA core CASCADE;"


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
