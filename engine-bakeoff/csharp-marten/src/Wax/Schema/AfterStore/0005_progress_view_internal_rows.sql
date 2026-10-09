-- Marten keeps more internal rows in its progression table than the high-water
-- mark itself: HighWaterAllocationFence, and HighWaterStuckGap, which records a
-- gap the daemon waited on. None is a projection or the rule runner, so the
-- harness's progress view leaves out every HighWater row.
CREATE OR REPLACE VIEW core.bakeoff_progress AS
SELECT progression.name::text AS name,
       COALESCE(progression.last_seq_id, 0) AS position,
       (SELECT COALESCE(max(seq_id), 0) FROM core.mt_events) AS head
FROM core.mt_event_progression progression
WHERE progression.name NOT LIKE 'HighWater%';
