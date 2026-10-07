"""Step 7: the application goes live and takes orders.

This script plays the part of your application. It signs in as the app
service principal and uses a connection pool that refreshes OAuth tokens, the
same pattern you would use in a web service.

As store_writer, the app can:
  * read the product catalog from store_serving.products (synced from Unity Catalog)
  * insert and update rows in store.orders and store.order_items

It cannot create, change or drop tables. scripts/09_verify_roles.py proves that.

Usage:
  python scripts/07_app_places_orders.py              # place 5 orders
  python scripts/07_app_places_orders.py --orders 20
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg import sql

from lakebase_starter import pg
from lakebase_starter.config import load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.ui import heading, ok, step

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--orders", type=int, default=5, help="how many orders to place (default 5)")
parser.add_argument("--branch", default="production", help="branch to write to (default production)")
args = parser.parse_args()

CUSTOMERS = ["alex@example.com", "sam@example.com", "jordan@example.com", "riley@example.com", "casey@example.com"]

cfg = load_config()
w = workspace_client(cfg)
products_table = sql.Identifier(cfg.serving_schema, "products")

heading("Step 7: the app places orders")

step("Signing in as the app service principal and opening a connection pool")
app = sign_in_as_service_principal(w, cfg, "app")
pool = pg.connection_pool(app, cfg, branch_id=args.branch)
with pool.connection() as conn:
    ok(f"connected as {conn.execute('SELECT current_user').fetchone()[0]}")

step(f"Placing {args.orders} orders")
placed = []
for _ in range(args.orders):
    with pool.connection() as conn, conn.transaction():
        # Read live prices from the synced product catalog.
        products = conn.execute(
            sql.SQL(
                "SELECT product_id, name, price FROM {} WHERE in_stock > 0 ORDER BY random() LIMIT %s"
            ).format(products_table),
            (random.randint(1, 3),),
        ).fetchall()
        lines = [(product_id, name, price, random.randint(1, 2)) for product_id, name, price in products]
        total = sum(price * qty for _, _, price, qty in lines)
        customer = random.choice(CUSTOMERS)
        order_id = conn.execute(
            "INSERT INTO store.orders (customer_email, total_amount) VALUES (%s, %s) RETURNING order_id",
            (customer, total),
        ).fetchone()[0]
        for line_no, (product_id, _, price, qty) in enumerate(lines, start=1):
            conn.execute(
                "INSERT INTO store.order_items (order_id, line_no, product_id, quantity, unit_price) "
                "VALUES (%s, %s, %s, %s, %s)",
                (order_id, line_no, product_id, qty, price),
            )
    placed.append(order_id)
    ok(f"order {order_id}: {customer:<20} {len(lines)} item(s), total {total}")

step("Moving orders through their lifecycle (updates show up in the change feed)")
with pool.connection() as conn, conn.transaction():
    for order_id in placed:
        status = random.choices(["paid", "shipped", "cancelled"], weights=[5, 4, 1])[0]
        conn.execute(
            "UPDATE store.orders SET status = %s, updated_at = now() WHERE order_id = %s", (status, order_id)
        )
        ok(f"order {order_id} -> {status}")

with pool.connection() as conn:
    count, revenue = conn.execute(
        "SELECT count(*), coalesce(sum(total_amount), 0) FROM store.orders WHERE status <> 'cancelled'"
    ).fetchone()
print(f"\n    store.orders now holds {count} order(s) that aren't cancelled, worth {revenue} in total.")
pool.close()

print("\nNext: python scripts/08_sync_lakebase_to_uc.py")
