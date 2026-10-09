-- The Pending Event Register (WAX-0027): the operational projection of the
-- scheduling and firing events. Entries are written in the same transaction
-- as the event that schedules them, and pollers claim due entries with
-- SKIP LOCKED.
CREATE TABLE core.pending_event (
    pending_event_pk uuid PRIMARY KEY,
    aggregate_pk_ref uuid NOT NULL,
    aggregate_type text NOT NULL,
    target_event_type text NOT NULL,
    due_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled')),
    scheduled_by_event uuid,
    fired_at timestamptz
);
CREATE INDEX idx_pending_event_due ON core.pending_event (status, due_at);

-- Batch job runs and the chunks workers claim (WAX-0024).
CREATE TABLE core.batch_job_run (
    batch_job_run_pk uuid PRIMARY KEY,
    job_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
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
