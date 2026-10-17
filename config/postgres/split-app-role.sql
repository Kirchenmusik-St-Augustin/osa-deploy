-- Role separation: the application must not connect as the bootstrap
-- superuser. deploy.yml runs this script on every deploy as that role inside
-- the database container, with the password from DATABASE_URL exported as
-- OSA_APP_PASSWORD. It is idempotent: it creates the role on a fresh
-- database, enforces its attributes, applies a changed password and moves
-- ownership of any object that still belongs to another role. By hand:
--   podman exec -i -e OSA_APP_PASSWORD osa-backend-pg \
--       sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1' \
--       < split-app-role.sql
-- REASSIGN OWNED BY cannot be used for the move: the bootstrap role also owns
-- the system databases, which PostgreSQL refuses to reassign.
\getenv app_password OSA_APP_PASSWORD

-- An empty password would create a role without any password.
SELECT :'app_password' = '' AS password_missing \gset
\if :password_missing
DO $$ BEGIN RAISE EXCEPTION 'OSA_APP_PASSWORD must not be empty'; END $$;
\endif

SELECT 'CREATE ROLE osa_app LOGIN NOSUPERUSER NOCREATEDB '
       'NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'osa_app')
\gexec

SELECT format('ALTER ROLE osa_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB '
              'NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'app_password')
\gexec

SELECT format('ALTER DATABASE %I OWNER TO osa_app', current_database())
\gexec

-- Only the application role may connect to the application database (the
-- bootstrap role is a superuser and needs no grant).
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database())
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO osa_app', current_database())
\gexec

-- The schema itself. A database that was recreated by a restore leaves public
-- owned by the restoring role and closed to everyone else, which locks the
-- application role out of it; a database created by initdb is not affected.
ALTER SCHEMA public OWNER TO osa_app;

DO $$
DECLARE
    obj record;
BEGIN
    -- Relations (tables, partitioned tables, views, materialized views,
    -- foreign tables, stand-alone sequences). Sequences that belong to a
    -- column follow their table and cannot be changed on their own.
    FOR obj IN
        SELECT format('ALTER %s %s OWNER TO osa_app',
                      CASE c.relkind
                          WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE'
                          WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                          WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'S' THEN 'SEQUENCE'
                      END, c.oid::regclass) AS ddl
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.objid = c.oid AND d.deptype IN ('a', 'i', 'e'))
    LOOP
        EXECUTE obj.ddl;
    END LOOP;

    -- Enum and domain types.
    FOR obj IN
        SELECT format('ALTER TYPE %s OWNER TO osa_app', t.oid::regtype) AS ddl
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.objid = t.oid AND d.deptype = 'e')
    LOOP
        EXECUTE obj.ddl;
    END LOOP;

    -- Functions and procedures that do not belong to an extension.
    FOR obj IN
        SELECT format('ALTER %s %s OWNER TO osa_app',
                      CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
                      p.oid::regprocedure) AS ddl
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.objid = p.oid AND d.deptype = 'e')
    LOOP
        EXECUTE obj.ddl;
    END LOOP;
END
$$;
