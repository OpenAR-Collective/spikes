#!/bin/sh
# Runs once, when the database volume is first created. The application
# connects as hivear_app, a member of wax_runtime, which can append events
# and maintain state but cannot alter the schema or change history. The same
# roles as the Python side, so the two run under the same privileges.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE wax_runtime NOLOGIN;
CREATE ROLE hivear_app LOGIN PASSWORD '${HIVEAR_APP_PASSWORD}' IN ROLE wax_runtime;
SQL
