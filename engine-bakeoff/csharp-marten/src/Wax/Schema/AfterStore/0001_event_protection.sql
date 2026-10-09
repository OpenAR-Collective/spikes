-- Events are immutable (WAX-0001). Marten's event table is append-only here:
-- the runtime role is not granted UPDATE or DELETE on it, and a trigger
-- refuses both, and TRUNCATE, for every role, the owner included.
CREATE FUNCTION core.fn_event_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'core.mt_events is append-only';
END $$;

CREATE TRIGGER trg_event_before_update_or_delete
    BEFORE UPDATE OR DELETE ON core.mt_events
    FOR EACH ROW EXECUTE FUNCTION core.fn_event_reject_mutation();

CREATE TRIGGER trg_event_before_truncate
    BEFORE TRUNCATE ON core.mt_events
    FOR EACH STATEMENT EXECUTE FUNCTION core.fn_event_reject_mutation();

-- The runtime role appends events, advances stream versions, and records
-- projection progress. It cannot alter the schema or change history.
GRANT USAGE ON SCHEMA core TO wax_runtime;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core TO wax_runtime;
REVOKE UPDATE ON core.mt_events FROM wax_runtime;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA core TO wax_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA core TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT, INSERT, UPDATE ON TABLES TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO wax_runtime;
