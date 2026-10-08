from django.db import migrations

# Default privileges make every table this owner later creates in ar usable
# by the runtime role without a grant in each migration. No DELETE: state is
# never deleted, and a read model is replaced by a new version, not emptied.
FORWARD = """
CREATE SCHEMA IF NOT EXISTS ar;
GRANT USAGE ON SCHEMA ar TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA ar GRANT SELECT, INSERT, UPDATE ON TABLES TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA ar GRANT USAGE, SELECT ON SEQUENCES TO wax_runtime;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = [("wax", "0001_core_schema")]
    operations = [migrations.RunSQL(FORWARD, "DROP SCHEMA ar CASCADE;")]
