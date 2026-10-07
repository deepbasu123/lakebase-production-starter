-- =============================================================================
-- Migration 002: let customers leave a delivery note on an order
--
-- A "next" migration that is not in production yet. docs/09-day-2-operations.md
-- uses it to show the safe way to change a production schema: try it on a
-- copy-on-write branch first, then apply it to production.
--
-- Run as : a member of store_owner (the deployer service principal).
-- Runner : scripts/10_test_migration_on_branch.py (tests it on a branch, then
--          applies it to production). Afterwards, move this file into
--          sql/migrations/ so new environments get it too.
-- =============================================================================
SET ROLE store_owner;

-- Adding a nullable column is quick in Postgres: existing rows are not rewritten.
ALTER TABLE store.orders ADD COLUMN IF NOT EXISTS delivery_note text;

RESET ROLE;
