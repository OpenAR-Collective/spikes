-- The bake-off harness reads events through this view, so it never needs to
-- know either event store's layout (WORKLOADS.md). Here it maps Marten's
-- table: the key, the stream, the type, the server's recorded time, and the
-- correlation from the event body.
CREATE VIEW core.bakeoff_event_log AS
SELECT id AS event_key,
       stream_id AS aggregate_pk_ref,
       type AS event_type,
       timestamp AS recorded_at,
       (data ->> 'correlation_pk_ref')::uuid AS correlation_pk_ref,
       data ->> 'correlation_type' AS correlation_type
FROM core.mt_events;

GRANT SELECT ON core.bakeoff_event_log TO wax_runtime;
