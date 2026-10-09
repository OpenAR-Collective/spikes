-- Adapted from the HiveAR prototype by Andy Troiano
-- (https://github.com/atroiano/platform-design, commit 361b6aa,
-- prototype/wax/migrations/0001_core_schema.py), Apache License 2.0.
-- Modified by The OpenAR Collective contributors.
-- Changes: the global sequence takes the name WAX-0001 gives it,
-- core.event_global_sequence_seq; the blue-green deployment tables and the
-- worker heartbeat are left out, because the bake-off does not run the
-- release drill; and the statements run as a script of the C# migrator
-- rather than a Django migration.
--
-- The core schema: the event store of WAX-0001 Enriched Event Sourcing, the
-- reason code registry (WAX-0020), and the projection checkpoints the reader
-- keeps. Hand-written SQL, because the names, the sequence, and the triggers
-- are specified by WAX-0001, WAX-0004 Schema Architecture, and WAX-0005
-- Naming Conventions.

-- The application connects as a member of wax_runtime, never as the owner
-- that runs migrations. The reference deployment creates the role before
-- the first migration; this covers every other path.
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

-- CACHE 1 is required: the reader's high-water mark relies on values being
-- handed out in order across sessions.
CREATE SEQUENCE core.event_global_sequence_seq CACHE 1;

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
-- high-water mark exact (WAX-0001, "Reading the Stream Without Skipping").
CREATE FUNCTION core.fn_event_before_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    PERFORM pg_current_xact_id();
    NEW.global_sequence_number := nextval('core.event_global_sequence_seq');
    NEW.recorded_at := clock_timestamp();
    RETURN NEW;
END $$;

CREATE TRIGGER trg_event_before_insert
    BEFORE INSERT ON core.event
    FOR EACH ROW EXECUTE FUNCTION core.fn_event_before_insert();

-- Events are immutable (WAX-0001). The runtime role is not granted UPDATE or
-- DELETE, and these triggers refuse both, and TRUNCATE, for every role, the
-- owner included.
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

-- One row per projection version, and one per rule runner partition. A
-- changed projection is a new version with its own row and its own table.
-- definition_digest pins the projection's definition, so two releases cannot
-- share a version they disagree about.
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

GRANT USAGE ON SCHEMA core TO wax_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA core TO wax_runtime;
GRANT INSERT ON core.event TO wax_runtime;
GRANT INSERT, UPDATE ON core.projection_checkpoint TO wax_runtime;
-- Tables a later script adds to core are readable without a grant in each one.
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT ON TABLES TO wax_runtime;
GRANT SELECT ON SEQUENCE core.event_global_sequence_seq TO wax_runtime;
