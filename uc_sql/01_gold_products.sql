-- =============================================================================
-- uc_sql/01_gold_products.sql  (Databricks SQL: runs on a SQL warehouse)
--
-- The lakehouse side of the Unity Catalog -> Lakebase sync: a curated "gold"
-- products table. In a real project a pipeline would build this table from
-- raw data. Here we create it with a few sample rows.
--
-- scripts/06_sync_uc_to_lakebase.py fills in {{catalog}}, {{gold_schema}} and
-- {{serving_schema}} from your config. To run this file by hand in the
-- Databricks SQL editor, replace those placeholders first.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS {{catalog}}.{{gold_schema}}
COMMENT 'Acme store: curated (gold) tables. products is synced into Lakebase.';

CREATE SCHEMA IF NOT EXISTS {{catalog}}.{{serving_schema}}
COMMENT 'Acme store: synced tables (read-only copies served from Lakebase) and their pipeline bookkeeping.';

CREATE TABLE IF NOT EXISTS {{catalog}}.{{gold_schema}}.products (
  product_id  BIGINT        NOT NULL COMMENT 'Primary key. Also the primary key of the synced Postgres table.',
  sku         STRING        NOT NULL,
  name        STRING        NOT NULL,
  category    STRING        NOT NULL,
  price       DECIMAL(10,2) NOT NULL COMMENT 'Current selling price',
  in_stock    INT           NOT NULL COMMENT 'Units available to sell',
  rating      DOUBLE                 COMMENT 'Average review score from 1 to 5',
  updated_at  TIMESTAMP     NOT NULL,
  CONSTRAINT products_pk PRIMARY KEY (product_id)
)
COMMENT 'Acme product catalog. Curated in the lakehouse, synced to Lakebase for the store app.'
-- Triggered and Continuous synced tables read the table's change data feed,
-- so it has to be switched on. (Snapshot mode doesn't need it.)
TBLPROPERTIES (delta.enableChangeDataFeed = true);

-- Seed rows. MERGE only inserts products that aren't there yet, so re-running
-- this file never duplicates rows or undoes later price changes.
MERGE INTO {{catalog}}.{{gold_schema}}.products AS t
USING (
  SELECT * FROM VALUES
    (1001, 'TNT-2P-ULT', 'Ridgeline 2P Ultralight Tent',      'Tents',      549.00, 42, 4.7),
    (1002, 'TNT-4P-FAM', 'Basecamp 4P Family Tent',           'Tents',      699.00, 18, 4.5),
    (1003, 'TNT-1P-BIV', 'Summit Bivvy Shelter',              'Tents',      289.00, 30, 4.2),
    (1004, 'SLP-BAG-M5', 'Alpine -5C Down Sleeping Bag',      'Sleeping',   429.00, 25, 4.8),
    (1005, 'SLP-BAG-S10','Coastal 10C Synthetic Sleeping Bag','Sleeping',   149.00, 60, 4.1),
    (1006, 'SLP-MAT-INF','Cloudrest Inflatable Mat',          'Sleeping',   179.00, 75, 4.4),
    (1007, 'SLP-PIL-CMP','Packable Camp Pillow',              'Sleeping',    39.00, 140, 4.0),
    (1008, 'PCK-DAY-25', 'Trailmate 25L Day Pack',            'Packs',      129.00, 90, 4.6),
    (1009, 'PCK-HIK-55', 'Overland 55L Hiking Pack',          'Packs',      319.00, 34, 4.7),
    (1010, 'PCK-HIK-70', 'Expedition 70L Pack',               'Packs',      389.00, 12, 4.5),
    (1011, 'LGT-HDL-400','Beacon 400 Headlamp',               'Lighting',    69.00, 200, 4.6),
    (1012, 'LGT-LNT-SOL','Solar Camp Lantern',                'Lighting',    59.00, 110, 4.3),
    (1013, 'CKG-STV-CAN','Flashpoint Canister Stove',         'Cooking',     89.00, 80, 4.5),
    (1014, 'CKG-POT-TI', 'Titanium 900ml Pot',                'Cooking',     79.00, 65, 4.7),
    (1015, 'CKG-FLT-H2O','Clearstream Water Filter',          'Cooking',     69.00, 120, 4.8),
    (1016, 'CLT-JKT-RN', 'Stormline Rain Jacket',             'Clothing',   249.00, 55, 4.4),
    (1017, 'CLT-FLC-MID','Highland Fleece Midlayer',          'Clothing',   119.00, 70, 4.3),
    (1018, 'CLT-SCK-MER','Merino Hiking Socks (2 pack)',      'Clothing',    35.00, 300, 4.9),
    (1019, 'NAV-GPS-HND','Waypoint Handheld GPS',             'Navigation', 459.00, 15, 4.2),
    (1020, 'NAV-CMP-BAS','Baseplate Compass',                 'Navigation',  29.00, 160, 4.6)
  AS v(product_id, sku, name, category, price, in_stock, rating)
) AS s
ON t.product_id = s.product_id
WHEN NOT MATCHED THEN INSERT (product_id, sku, name, category, price, in_stock, rating, updated_at)
  VALUES (s.product_id, s.sku, s.name, s.category, s.price, s.in_stock, s.rating, current_timestamp());
