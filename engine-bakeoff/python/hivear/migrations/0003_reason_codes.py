"""Reason codes this draft's commands use (WAX-0020 Reason Code Registry)."""

from django.db import migrations

FORWARD = """
INSERT INTO core.reference_type (code, label) VALUES ('reason_code', 'Reason code');
INSERT INTO core.reference_value (reference_type_fk, code, label)
SELECT reference_type_pk, value.code, value.label
FROM core.reference_type,
     (VALUES ('account_placed', 'Account placed'),
             ('dispute_raised', 'Dispute raised'),
             ('payment_received', 'Payment received'),
             ('payment_returned', 'Payment returned')) AS value (code, label)
WHERE core.reference_type.code = 'reason_code';
"""

REVERSE = """
DELETE FROM core.reference_value WHERE reference_type_fk IN
    (SELECT reference_type_pk FROM core.reference_type WHERE code = 'reason_code');
DELETE FROM core.reference_type WHERE code = 'reason_code';
"""


class Migration(migrations.Migration):
    dependencies = [("hivear", "0002_ar_tables")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
