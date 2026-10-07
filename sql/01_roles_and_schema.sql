-- =============================================================================
-- 01_roles_and_schema.sql
-- One-time platform setup: group roles, the application schema, and grants.
--
-- Run as : the project owner, connected to the databricks_postgres database on
--          the production branch. Keep running it as the same identity: in
--          Postgres 16+ only a role's creator (or someone the creator granted
--          ADMIN OPTION) may grant that role to others.
-- Re-run : safe. Everything here is idempotent.
-- Runner : scripts/04_setup_database.py runs this for you. You can also paste
--          it into the Lakebase SQL editor.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Group roles ("job titles")
-- -----------------------------------------------------------------------------
-- These roles are NOLOGIN: nobody signs in as them. They are bundles of
-- permissions. People and applications get permissions by being made a
-- MEMBER of one of them (scripts/04_setup_database.py does that part).
--
--   store_owner   Owns the tables. Used only by schema migrations (DDL).
--   store_writer  Reads and changes rows. The application at runtime.
--   store_reader  Reads only. Analysts, BI tools, support staff.
--
-- Why not grant straight to people? People and apps come and go. Grant to a
-- group role once, then onboarding or offboarding is a single GRANT or REVOKE.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'store_owner') THEN
    CREATE ROLE store_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'store_writer') THEN
    CREATE ROLE store_writer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'store_reader') THEN
    CREATE ROLE store_reader NOLOGIN;
  END IF;
END
$$;

-- A writer can do everything a reader can. Making store_writer a member of
-- store_reader means read grants only ever need to be given to store_reader.
GRANT store_reader TO store_writer;

-- Let the person running this script act as store_owner. Postgres requires
-- membership in a role before you can create objects owned by it or set
-- default privileges for it.
GRANT store_owner TO CURRENT_USER;

-- -----------------------------------------------------------------------------
-- 2. The application schema
-- -----------------------------------------------------------------------------
-- `store` holds the tables the application writes (orders, order_items).
-- It is owned by store_owner, not by a person, so it survives staff changes.
-- Lakebase Change Data Feed streams every change in this schema to Unity Catalog.
CREATE SCHEMA IF NOT EXISTS store AUTHORIZATION store_owner;
COMMENT ON SCHEMA store IS 'Acme store: application tables (written by the app, streamed to Unity Catalog by Lakebase Change Data Feed)';

-- -----------------------------------------------------------------------------
-- 3. Who can connect and see the schema
-- -----------------------------------------------------------------------------
GRANT CONNECT ON DATABASE databricks_postgres TO store_owner, store_writer, store_reader;
GRANT USAGE ON SCHEMA store TO store_reader;   -- store_writer inherits this

-- -----------------------------------------------------------------------------
-- 4. Default privileges: grants for tables that do not exist yet
-- -----------------------------------------------------------------------------
-- Migrations create tables as store_owner. These rules give the reader and
-- writer roles access to every future table automatically, so nobody has to
-- remember to run GRANT after each migration.
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT SELECT ON TABLES TO store_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT INSERT, UPDATE, DELETE ON TABLES TO store_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT USAGE, SELECT ON SEQUENCES TO store_writer;

-- The same grants for tables that already exist (a no-op on a fresh database).
GRANT SELECT ON ALL TABLES IN SCHEMA store TO store_reader;
GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA store TO store_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA store TO store_writer;
