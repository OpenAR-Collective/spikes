"""The ar additions the engine bake-off's WORKLOADS.md specifies: the
account's interest_accrued_through and queue_code, the task table, and six
reason codes. Foundation-written; not part of Andy Troiano's prototype.
"""

import django.db.models.deletion
from django.db import migrations, models

ACCOUNT_COLUMNS = """
ALTER TABLE ar.account ADD COLUMN interest_accrued_through date;
UPDATE ar.account SET interest_accrued_through = itemization_date;
ALTER TABLE ar.account ALTER COLUMN interest_accrued_through SET NOT NULL;
ALTER TABLE ar.account ADD COLUMN queue_code text;
"""

REASON_CODES = """
INSERT INTO core.reference_value (reference_type_fk, code, label)
SELECT reference_type_pk, value.code, value.label
FROM core.reference_type,
     (VALUES ('interest_accrued', 'Interest accrued'),
             ('paid_in_full', 'Paid in full'),
             ('pending_event_scheduled', 'Pending event scheduled'),
             ('pending_event_fired', 'Pending event fired'),
             ('task_created', 'Task created'),
             ('queue_assigned', 'Queue assigned')) AS value (code, label)
WHERE core.reference_type.code = 'reason_code';
"""


class Migration(migrations.Migration):
    dependencies = [("hivear", "0005_account_payment_lines"), ("wax", "0003_pending_event_and_batch")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    ACCOUNT_COLUMNS,
                    "ALTER TABLE ar.account DROP COLUMN queue_code; ALTER TABLE ar.account DROP COLUMN interest_accrued_through;",
                )
            ],
            state_operations=[
                migrations.AddField(model_name="account", name="interest_accrued_through", field=models.DateField()),
                migrations.AddField(model_name="account", name="queue_code", field=models.TextField(null=True)),
            ],
        ),
        migrations.CreateModel(
            name="Task",
            fields=[
                ("task_pk", models.UUIDField(primary_key=True, serialize=False)),
                ("task_type", models.TextField()),
                ("status", models.TextField()),
                ("created_at", models.DateTimeField()),
                (
                    "account",
                    models.ForeignKey(
                        db_column="account_fk",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tasks",
                        to="hivear.account",
                    ),
                ),
            ],
            options={"db_table": 'ar"."task'},
        ),
        migrations.RunSQL(
            REASON_CODES,
            "DELETE FROM core.reference_value WHERE code IN ('interest_accrued', 'paid_in_full',"
            " 'pending_event_scheduled', 'pending_event_fired', 'task_created', 'queue_assigned');",
        ),
    ]
