"""Step 12: get deleted data back with point-in-time recovery.

Someone (or some bug) deletes an order. Lakebase keeps a history of every
change for the restore window (7 days here), so you can create a branch that
shows the database exactly as it was a moment before the mistake, copy the
lost rows out of it, and delete the branch. Production keeps running the
whole time.

What this script does
---------------------
1. Picks the newest order, keeps a safety copy of it in memory, and notes the time.
2. Simulates the accident: the app's service principal deletes the order
   (its order_items go with it, through ON DELETE CASCADE).
3. Creates a recovery branch of production *as of the noted time*.
4. Reads the order and its items from the recovery branch.
5. Puts them back in production, keeping the original order_id. (If the
   recovery branch fails for any reason, it uses the safety copy instead, so
   the demo can't lose data.)
6. Deletes the recovery branch.

The restore runs as you, the project owner: recovering data is an operator's
job, not the application's.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import Branch, BranchSpec
from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp
from psycopg import sql

from lakebase_starter import pg
from lakebase_starter.config import load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.ui import explain, heading, ok, skip, step, warn

cfg = load_config()
w = workspace_client(cfg)
branch_id = f"recovery-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

heading("Step 12: recover a deleted order with point-in-time recovery")

step("Picking an order and noting the time")
with pg.connect(w, cfg) as conn:
    columns = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'store' AND table_name = 'orders' ORDER BY ordinal_position"
    ).fetchall()]
    column_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    select_order = sql.SQL("SELECT {} FROM store.orders WHERE order_id = %s").format(column_list)
    select_items = sql.SQL(
        "SELECT order_id, line_no, product_id, quantity, unit_price FROM store.order_items WHERE order_id = %s"
    )
    latest = conn.execute("SELECT max(order_id) FROM store.orders").fetchone()[0]
    if latest is None:
        raise SystemExit("store.orders is empty. Run scripts/07_app_places_orders.py first.")
    # A safety copy, kept in memory. If the recovery branch fails for any reason,
    # the script puts these rows back instead, so running the demo never loses data.
    saved_order = conn.execute(select_order, (latest,)).fetchone()
    saved_items = conn.execute(select_items, (latest,)).fetchall()
order_id = latest
time.sleep(5)  # make sure the noted time is clearly after the order was written
before_the_mistake = datetime.now(timezone.utc)
time.sleep(5)
ok(f"order {order_id} with {len(saved_items)} item(s); the time is {before_the_mistake:%H:%M:%S} UTC")

step("Oops: the app deletes the order")
app = sign_in_as_service_principal(w, cfg, "app")
with pg.connect(app, cfg) as conn:
    conn.execute("DELETE FROM store.orders WHERE order_id = %s", (order_id,))
with pg.connect(w, cfg) as conn:
    left = conn.execute("SELECT count(*) FROM store.orders WHERE order_id = %s", (order_id,)).fetchone()[0]
ok(f"order {order_id} rows left in production: {left}")

used_safety_copy = False
try:
    step(f"Creating branch '{branch_id}' as production looked at {before_the_mistake:%H:%M:%S} UTC")
    explain(
        """A point-in-time branch is built from Lakebase's change history, so it takes
        seconds, whatever the size of the database. It must be within the project's
        restore window."""
    )
    source_time = Timestamp()
    source_time.FromDatetime(before_the_mistake)
    w.postgres.create_branch(
        parent=cfg.project,
        branch_id=branch_id,
        branch=Branch(
            spec=BranchSpec(
                source_branch=cfg.branch(),
                source_branch_time=source_time,
                ttl=Duration(seconds=2 * 3600),  # gone by itself in 2 hours if we forget
            )
        ),
    ).wait()
    ok("created")

    step("Reading the lost rows from the recovery branch")
    with pg.connect(w, cfg, branch_id=branch_id) as conn:
        order = conn.execute(select_order, (order_id,)).fetchone()
        items = conn.execute(select_items, (order_id,)).fetchall()
    if order is None:
        raise RuntimeError("the order isn't on the recovery branch")
    ok(f"found order {order_id} with {len(items)} item(s)")
except Exception as error:  # noqa: BLE001 - whatever went wrong, don't leave the order deleted
    warn(f"recovery from the branch failed ({error}); using the safety copy instead")
    order, items = saved_order, saved_items
    used_safety_copy = True

step("Putting them back in production")
with pg.connect(w, cfg) as conn, conn.transaction():
    # order_id is GENERATED ALWAYS AS IDENTITY, so keeping the original ID
    # needs OVERRIDING SYSTEM VALUE.
    conn.execute(
        sql.SQL("INSERT INTO store.orders ({}) OVERRIDING SYSTEM VALUE VALUES ({})").format(
            column_list, sql.SQL(", ").join(sql.Placeholder() * len(columns))
        ),
        order,
    )
    for item in items:
        conn.execute(
            "INSERT INTO store.order_items (order_id, line_no, product_id, quantity, unit_price) "
            "VALUES (%s, %s, %s, %s, %s)",
            item,
        )
with pg.connect(w, cfg) as conn:
    back = conn.execute("SELECT count(*) FROM store.order_items WHERE order_id = %s", (order_id,)).fetchone()[0]
ok(f"order {order_id} is back in production with {back} item(s)")
explain(
    """Lakebase Change Data Feed records all of this: the delete, then the insert.
    The history in Unity Catalog shows exactly what happened and when."""
)

step(f"Deleting branch '{branch_id}'")
try:
    w.postgres.delete_branch(name=cfg.branch(branch_id)).wait()
    ok("deleted")
except NotFound:
    skip("there was no branch to delete")

if used_safety_copy:
    sys.exit("\nThe order is back, but from the safety copy: point-in-time recovery itself failed (see above).")
print("\nWhen you're done: python scripts/99_teardown.py")
