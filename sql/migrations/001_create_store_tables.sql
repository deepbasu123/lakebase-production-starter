-- =============================================================================
-- Migration 001: create the store tables
--
-- Run as : a member of store_owner. In this example that is the "deployer"
--          service principal, the identity your CI/CD pipeline uses.
-- Runner : scripts/05_run_migrations.py
-- =============================================================================

-- Act as store_owner for the rest of this file, so the tables are owned by the
-- group role, not by whichever person or service principal ran the migration.
-- The default privileges set in 01_roles_and_schema.sql then hand out
-- SELECT/INSERT/UPDATE/DELETE to the reader and writer roles automatically.
SET ROLE store_owner;

-- Column types are deliberately boring (bigint, integer, text, numeric,
-- timestamptz). Lakebase Change Data Feed, which streams these tables to Unity
-- Catalog, converts a documented list of Postgres types to Delta types. Stick
-- to common types and check that list before using anything unusual.
CREATE TABLE IF NOT EXISTS store.orders (
  order_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_email  text           NOT NULL,
  status          text           NOT NULL DEFAULT 'placed'
                  CHECK (status IN ('placed', 'paid', 'shipped', 'cancelled')),
  total_amount    numeric(12, 2) NOT NULL CHECK (total_amount >= 0),
  created_at      timestamptz    NOT NULL DEFAULT now(),
  updated_at      timestamptz    NOT NULL DEFAULT now()
);
COMMENT ON TABLE store.orders IS 'One row per customer order. Written by the app.';

CREATE TABLE IF NOT EXISTS store.order_items (
  order_id    bigint         NOT NULL REFERENCES store.orders (order_id) ON DELETE CASCADE,
  line_no     integer        NOT NULL,
  product_id  bigint         NOT NULL,   -- matches store_serving.products, which is synced from Unity Catalog
  quantity    integer        NOT NULL CHECK (quantity > 0),
  unit_price  numeric(10, 2) NOT NULL CHECK (unit_price >= 0),
  PRIMARY KEY (order_id, line_no)
);
COMMENT ON TABLE store.order_items IS 'Order lines. Prices are copied from store_serving.products at order time.';

-- Index the lookups the app does most ("show me my recent orders").
CREATE INDEX IF NOT EXISTS orders_customer_created_idx
  ON store.orders (customer_email, created_at DESC);

-- Lakebase Change Data Feed requires REPLICA IDENTITY FULL on every table it streams.
-- It makes Postgres write the whole old row to its change log on UPDATE and
-- DELETE, so Unity Catalog receives before-and-after images of each change.
ALTER TABLE store.orders      REPLICA IDENTITY FULL;
ALTER TABLE store.order_items REPLICA IDENTITY FULL;

RESET ROLE;
