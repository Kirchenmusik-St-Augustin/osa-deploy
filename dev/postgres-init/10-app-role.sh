#!/bin/sh
# Creates the least-privilege application role of the dev database. Mounted
# read-only into /docker-entrypoint-initdb.d, so it runs once, on the first
# initialisation of an EMPTY data directory (see the README for migrating an
# existing one).
#
# POSTGRES_USER stays the bootstrap role: it is always a superuser and is for
# administration only. The application connects as osa_app, which owns the
# application database but cannot run COPY ... PROGRAM, read or write server
# files, create roles or install untrusted extensions.
set -eu

: "${OSA_APP_PASSWORD:?OSA_APP_PASSWORD must be set in osa-backend-pg.env}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
	-v app_password="$OSA_APP_PASSWORD" -v app_database="$POSTGRES_DB" <<'SQL'
CREATE ROLE osa_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'app_password';
ALTER DATABASE :"app_database" OWNER TO osa_app;
REVOKE ALL ON DATABASE :"app_database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_database" TO osa_app;
SQL
