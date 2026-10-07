-- =============================================================================
-- uc_sql/02_history_views.sql  (Databricks SQL: runs on a SQL warehouse)
--
-- Lakebase Change Data Feed writes one Delta table per Postgres table:
--   store.orders       ->  {{catalog}}.{{history_schema}}.lb_orders_history
--   store.order_items  ->  {{catalog}}.{{history_schema}}.lb_order_items_history
--
-- Each history table keeps EVERY change, one row per event:
--   insert            a new row (also used for rows copied by the first snapshot)
--   update_preimage   the row as it was before an UPDATE
--   update_postimage  the row as it is after an UPDATE
--   delete            a deleted row
--
-- That full history is great for audit. Most reports want the current state,
-- so these views keep the latest version of each row and drop deleted ones.
-- The result matches what the app sees in Postgres, delayed by the few
-- seconds the feed needs to catch up.
--
-- scripts/08_sync_lakebase_to_uc.py fills in the {{placeholders}}.
-- =============================================================================

CREATE OR REPLACE VIEW {{catalog}}.{{history_schema}}.orders_current
COMMENT 'Current state of store.orders, rebuilt from the Lakebase change data feed'
AS SELECT * EXCEPT (rn, _pg_change_type, _pg_lsn, _pg_xid, _timestamp, _sort_by)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY _sort_by DESC) AS rn
  FROM {{catalog}}.{{history_schema}}.lb_orders_history
  WHERE _pg_change_type IN ('insert', 'update_postimage', 'delete')
)
WHERE rn = 1 AND _pg_change_type <> 'delete';

CREATE OR REPLACE VIEW {{catalog}}.{{history_schema}}.order_items_current
COMMENT 'Current state of store.order_items, rebuilt from the Lakebase change data feed'
AS SELECT * EXCEPT (rn, _pg_change_type, _pg_lsn, _pg_xid, _timestamp, _sort_by)
FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY order_id, line_no ORDER BY _sort_by DESC) AS rn
  FROM {{catalog}}.{{history_schema}}.lb_order_items_history
  WHERE _pg_change_type IN ('insert', 'update_postimage', 'delete')
)
WHERE rn = 1 AND _pg_change_type <> 'delete';

-- An analytics view that joins operational data (orders, from Lakebase) with
-- lakehouse data (products, the gold table). This is the payoff of syncing
-- both ways: one SQL query across your app's data and your analytics data.
CREATE OR REPLACE VIEW {{catalog}}.{{history_schema}}.daily_revenue_by_category
COMMENT 'Revenue per day and product category, excluding cancelled orders'
AS SELECT
  DATE(o.created_at)                    AS order_date,
  p.category,
  COUNT(DISTINCT o.order_id)            AS orders,
  SUM(i.quantity)                       AS units,
  SUM(i.quantity * i.unit_price)        AS revenue
FROM {{catalog}}.{{history_schema}}.orders_current o
JOIN {{catalog}}.{{history_schema}}.order_items_current i ON i.order_id = o.order_id
JOIN {{catalog}}.{{gold_schema}}.products p ON p.product_id = i.product_id
WHERE o.status <> 'cancelled'
GROUP BY DATE(o.created_at), p.category;
