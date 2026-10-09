-- The bake-off harness samples this view for read-model and rule-runner lag,
-- and to know when the engine has caught up (WORKLOADS.md): one row per
-- projection version and per rule runner partition, each with the last
-- sequence it has handled, and the sequence's position as the head. The rule
-- runner's base position, which only the seed sets, is not a reader.
CREATE VIEW core.bakeoff_progress AS
SELECT projection_name AS name,
       last_global_sequence_number AS position,
       (SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.event_global_sequence_seq) AS head
FROM core.projection_checkpoint
WHERE projection_name <> 'wax.rules:base';

GRANT SELECT ON core.bakeoff_progress TO wax_runtime;
