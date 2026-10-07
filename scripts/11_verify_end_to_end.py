"""Step 11: watch data flow both ways, end to end.

Round trip 1, Unity Catalog -> Lakebase
  Change a product's price in the gold Delta table, trigger the synced table,
  and see the new price in Postgres.

Round trip 2, Lakebase -> Unity Catalog
  The app (as its service principal) places an order and then marks it paid.
  Within seconds the insert and both halves of the update land in
  lb_orders_history, and orders_current shows the latest status.

Exits with an error if either round trip doesn't complete.
"""

from __future__ import annotations

import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.pipelines import UpdateInfoState
from psycopg import sql

from lakebase_starter import pg, uc
from lakebase_starter.config import load_config
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.ui import explain, heading, ok, step

PRODUCT_ID = 1001

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)
gold = uc.ident(cfg.catalog, cfg.gold_schema, "products")
history = uc.ident(cfg.catalog, cfg.history_schema)
synced_table_name = f"synced_tables/{cfg.catalog}.{cfg.serving_schema}.products"

heading("Step 11: end-to-end check, both directions")

# ---------------------------------------------------------------------------
step("Round trip 1: change a price in Unity Catalog")
new_price = f"{random.randint(500, 599)}.00"
uc.run(
    w,
    cfg.warehouse_id,
    f"UPDATE {gold} SET price = {new_price}, updated_at = current_timestamp() WHERE product_id = {PRODUCT_ID}",
)
ok(f"{gold}: product {PRODUCT_ID} now costs {new_price}")

step("Triggering the synced table (TRIGGERED mode only syncs when asked)")
explain(
    """In production you would trigger this from a Lakeflow job: either on a schedule,
    or with a 'table update' trigger that fires when the gold table changes. Here we
    start the synced table's pipeline directly."""
)
pipeline_id = w.postgres.get_synced_table(name=synced_table_name).status.pipeline_id
update_id = w.pipelines.start_update(pipeline_id=pipeline_id).update_id
deadline = time.time() + 20 * 60
while True:
    state = w.pipelines.get_update(pipeline_id=pipeline_id, update_id=update_id).update.state
    if state == UpdateInfoState.COMPLETED:
        break
    if state in (UpdateInfoState.FAILED, UpdateInfoState.CANCELED) or time.time() > deadline:
        raise SystemExit(f"Synced table refresh ended in state {state}")
    time.sleep(10)
ok("refresh completed")

with pg.connect(w, cfg) as conn:
    price = conn.execute(
        sql.SQL("SELECT price FROM {} WHERE product_id = %s").format(sql.Identifier(cfg.serving_schema, "products")),
        (PRODUCT_ID,),
    ).fetchone()[0]
if f"{price}" != new_price:
    raise SystemExit(f"Postgres still shows {price}, expected {new_price}")
ok(f"Postgres {cfg.serving_schema}.products: product {PRODUCT_ID} costs {price}")

# ---------------------------------------------------------------------------
step("Round trip 2: the app places an order and marks it paid")
app = sign_in_as_service_principal(w, cfg, "app")
marker = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
with pg.connect(app, cfg) as conn:
    with conn.transaction():
        order_id = conn.execute(
            "INSERT INTO store.orders (customer_email, total_amount) VALUES (%s, %s) RETURNING order_id",
            (marker, new_price),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO store.order_items (order_id, line_no, product_id, quantity, unit_price) "
            "VALUES (%s, 1, %s, 1, %s)",
            (order_id, PRODUCT_ID, new_price),
        )
    conn.execute("UPDATE store.orders SET status = 'paid', updated_at = now() WHERE order_id = %s", (order_id,))
ok(f"order {order_id} for {marker}: placed, then paid")

step("Waiting for the change data feed to deliver it to Unity Catalog")
deadline = time.time() + 5 * 60
while True:
    changes = dict(
        uc.run(
            w,
            cfg.warehouse_id,
            f"SELECT _pg_change_type, count(*) FROM {history}.lb_orders_history "
            f"WHERE order_id = {order_id} GROUP BY 1",
        )
    )
    if {"insert", "update_preimage", "update_postimage"} <= changes.keys():
        break
    if time.time() > deadline:
        raise SystemExit(f"Only saw {changes or 'nothing'} after 5 minutes")
    time.sleep(10)
for change_type in ("insert", "update_preimage", "update_postimage"):
    ok(f"lb_orders_history has an '{change_type}' row for order {order_id}")

status = uc.run(w, cfg.warehouse_id, f"SELECT status FROM {history}.orders_current WHERE order_id = {order_id}")[0][0]
if status != "paid":
    raise SystemExit(f"orders_current shows status {status}, expected 'paid'")
ok(f"{history}.orders_current shows order {order_id} as '{status}'")

print("\nBoth directions work: Unity Catalog -> Lakebase and Lakebase -> Unity Catalog.")
print("\nNext: python scripts/12_recover_deleted_data.py")
