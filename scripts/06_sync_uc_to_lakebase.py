"""Step 6: sync a Unity Catalog table INTO Lakebase (synced tables).

The idea
--------
Your lakehouse already has curated data the app needs: here, a product
catalog. Apps need millisecond lookups and lots of small concurrent reads,
which is what Postgres is built for. A *synced table* keeps a read-only copy
of a Unity Catalog table in Lakebase, refreshed by a managed pipeline.

    <catalog>.store_gold.products          (Delta, in Unity Catalog)
            |  synced table, TRIGGERED mode
            v
    store_serving.products                 (Postgres table, read-only)

What this script does
---------------------
1. Creates the gold products table in Unity Catalog with change data feed
   on (uc_sql/01_gold_products.sql).
2. Creates the synced table <catalog>.store_serving.products. Its Unity
   Catalog schema name (store_serving) is also the Postgres schema it lands in.
3. Waits for the first sync to finish.
4. Grants the store_reader group role SELECT on the new Postgres schema.
   (store_writer inherits it.) Synced tables are owned by an internal
   Databricks role, so the default privileges from step 4 don't cover them.

Sync modes, in short:
  SNAPSHOT    full copy every time you sync. Cheap for small tables or big changes.
  TRIGGERED   incremental: only changed rows, when you trigger it or on a schedule.
  CONTINUOUS  streams changes within seconds. Lowest lag, highest cost.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import (
    NewPipelineSpec,
    SyncedTable,
    SyncedTableState,
    SyncedTableSyncedTableSpec,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy as SchedulingPolicy,
)
from psycopg import sql

from lakebase_starter import pg, uc
from lakebase_starter.config import REPO_ROOT, load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.ui import explain, heading, ok, skip, step

cfg = load_config()
w = workspace_client(cfg)

# Plain names for the Lakebase API, quoted names for SQL.
source_table = f"{cfg.catalog}.{cfg.gold_schema}.products"
synced_table_id = f"{cfg.catalog}.{cfg.serving_schema}.products"
source_table_sql = uc.ident(cfg.catalog, cfg.gold_schema, "products")

heading("Step 6: sync Unity Catalog -> Lakebase (synced table)")

step(f"Gold table {source_table} (uc_sql/01_gold_products.sql)")
uc.run_file(
    w,
    cfg.warehouse_id,
    REPO_ROOT / "uc_sql" / "01_gold_products.sql",
    {
        "catalog": uc.ident(cfg.catalog),
        "gold_schema": uc.ident(cfg.gold_schema),
        "serving_schema": uc.ident(cfg.serving_schema),
    },
)
count = uc.run(w, cfg.warehouse_id, f"SELECT count(*) FROM {source_table_sql}")[0][0]
ok(f"{count} products, change data feed enabled")

step(f"Synced table {synced_table_id}")
try:
    w.postgres.get_synced_table(name=f"synced_tables/{synced_table_id}")
    skip("already exists")
except NotFound:
    w.postgres.create_synced_table(
        synced_table_id=synced_table_id,
        synced_table=SyncedTable(
            spec=SyncedTableSyncedTableSpec(
                source_table_full_name=source_table,
                primary_key_columns=["product_id"],
                scheduling_policy=SchedulingPolicy.TRIGGERED,
                branch=cfg.branch(),
                postgres_database=cfg.database,
                # Create the Postgres schema (store_serving) if it doesn't exist yet.
                create_database_objects_if_missing=True,
                # Where the managed pipeline keeps its own bookkeeping in Unity Catalog.
                new_pipeline_spec=NewPipelineSpec(
                    storage_catalog=cfg.catalog, storage_schema=cfg.serving_schema
                ),
            )
        ),
    ).wait()
    ok("created")

step("Waiting for the first sync to finish (usually a few minutes)")
explain(
    """Behind the scenes Databricks starts a managed Lakeflow pipeline that copies
    the table into Postgres. Later syncs in TRIGGERED mode copy only the rows that
    changed since the last sync."""
)
# A TRIGGERED table that has finished a sync reports ..._ONLINE_NO_PENDING_UPDATE.
# (A CONTINUOUS one would settle on ..._ONLINE_CONTINUOUS_UPDATE.)
healthy = {
    SyncedTableState.SYNCED_TABLE_ONLINE,
    SyncedTableState.SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE,
    SyncedTableState.SYNCED_TABLE_ONLINE_CONTINUOUS_UPDATE,
}
failed = {
    SyncedTableState.SYNCED_TABLE_OFFLINE_FAILED,
    SyncedTableState.SYNCED_TABLE_ONLINE_PIPELINE_FAILED,
}
offline_polls = 0  # plain OFFLINE only counts as a failure if it lasts
last_state = None
deadline = time.time() + 30 * 60
while True:
    table = w.postgres.get_synced_table(name=f"synced_tables/{synced_table_id}")
    state = table.status.detailed_state
    if state != last_state:
        print(f"    state: {state.value if state else 'unknown'}")
        last_state = state
    if state in healthy:
        break
    offline_polls = offline_polls + 1 if state == SyncedTableState.SYNCED_TABLE_OFFLINE else 0
    if state in failed or offline_polls >= 4:  # 4 polls is about a minute
        raise SystemExit(f"Sync is {state.value}: {table.status.message}")
    if time.time() > deadline:
        raise SystemExit("Timed out after 30 minutes. Check the pipeline in the Databricks UI.")
    time.sleep(15)
ok(f"online (pipeline {table.status.pipeline_id})")

step(f"Granting read access on Postgres schema `{cfg.serving_schema}`")
conn = pg.connect(w, cfg)
schema = sql.Identifier(cfg.serving_schema)
conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO store_reader").format(schema))
conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO store_reader").format(schema))
ok("store_reader (and so store_writer) can read the synced tables")
explain(
    """Re-run this step after you add more synced tables to the schema: GRANT ... ON
    ALL TABLES only covers tables that exist when it runs."""
)

step("A peek at the data, straight from Postgres")
rows = conn.execute(
    sql.SQL("SELECT product_id, name, price, in_stock FROM {}.products ORDER BY product_id LIMIT 5").format(schema)
).fetchall()
for product_id, name, price, in_stock in rows:
    print(f"    {product_id}  {name:<36} {price:>8}  stock {in_stock}")
owner = conn.execute(
    "SELECT tableowner FROM pg_tables WHERE schemaname = %s AND tablename = 'products'", (cfg.serving_schema,)
).fetchone()[0]
print(f"    (Postgres table owner: {owner}, an internal role that keeps the table in sync)")
conn.close()

print("\nNext: python scripts/07_app_places_orders.py")
