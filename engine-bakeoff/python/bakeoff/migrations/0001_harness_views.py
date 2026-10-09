"""The views the bake-off harness reads, so it never depends on either
side's event store layout (WORKLOADS.md, "What each side provides").
Foundation-written.

core.bakeoff_event_log is the specified view. core.bakeoff_progress gives the
harness each reader's position and the head of the stream, for read-model
and rule lag; it is proposed to the C# side for the specification.
"""

from django.db import migrations

FORWARD = """
CREATE VIEW core.bakeoff_event_log AS
SELECT event_pk AS event_key, aggregate_pk_ref, event_type, recorded_at, correlation_pk_ref, correlation_type
FROM core.event;

CREATE VIEW core.bakeoff_progress AS
SELECT projection_name AS name,
       last_global_sequence_number AS position,
       (SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.seq_event_global_sequence) AS head
FROM core.projection_checkpoint
WHERE projection_name <> 'wax.rules:base';

GRANT SELECT ON core.bakeoff_event_log, core.bakeoff_progress TO wax_runtime;
"""

REVERSE = "DROP VIEW core.bakeoff_progress; DROP VIEW core.bakeoff_event_log;"


class Migration(migrations.Migration):
    initial = True
    dependencies = [("hivear", "0006_bakeoff_additions")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
