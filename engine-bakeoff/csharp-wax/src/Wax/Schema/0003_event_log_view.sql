-- The bake-off harness reads events through this view, so it never needs to
-- know any side's event store layout (WORKLOADS.md). On core.event it is a
-- direct mapping, as on the Python side.
CREATE VIEW core.bakeoff_event_log AS
SELECT event_pk AS event_key, aggregate_pk_ref, event_type, recorded_at, correlation_pk_ref, correlation_type
FROM core.event;

GRANT SELECT ON core.bakeoff_event_log TO wax_runtime;
