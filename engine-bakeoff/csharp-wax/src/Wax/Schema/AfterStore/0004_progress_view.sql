-- The bake-off harness samples this view for read-model and rule-runner lag,
-- and to know when the engine has caught up (WORKLOADS.md). Here it maps
-- Marten's progression table: one row per projection shard and per rule
-- runner subscription, each with the last sequence it has handled and the
-- highest sequence in the event store.
CREATE VIEW core.bakeoff_progress AS
SELECT progression.name::text AS name,
       COALESCE(progression.last_seq_id, 0) AS position,
       (SELECT COALESCE(max(seq_id), 0) FROM core.mt_events) AS head
FROM core.mt_event_progression progression
WHERE progression.name NOT IN ('HighWaterMark', 'HighWaterAllocationFence');

GRANT SELECT ON core.bakeoff_progress TO wax_runtime;
