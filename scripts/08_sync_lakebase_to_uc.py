"""Step 8: stream Lakebase changes OUT to Unity Catalog (Lakebase Change Data Feed).

The idea
--------
The app's orders live in Postgres, but the analysts, dashboards and ML models
live in the lakehouse. Lakebase Change Data Feed (CDF) reads Postgres' own
change log and writes every insert, update and delete to Unity Catalog Delta
tables, about every 15 seconds. You don't run any pipeline or job for it.

    store.orders       (Postgres)  -->  <catalog>.store_history.lb_orders_history       (Delta)
    store.order_items  (Postgres)  -->  <catalog>.store_history.lb_order_items_history  (Delta)

CDF works per Postgres *schema*: every current and future table in `store`
is included. That is why synced tables live in a separate schema.

Before you run this
-------------------
* CDF is in Public Preview. A workspace admin must switch on "Lakebase Change
  Data Feed" on the workspace Previews page.
* Every table needs REPLICA IDENTITY FULL (migration 001 sets it) and at least
  one row (step 7 created some orders).
* The destination catalog must not use default storage.
* You need CAN MANAGE on the project, plus USE CATALOG, USE SCHEMA and CREATE
  TABLE on the destination.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import CdfConfig, CdfState
from psycopg import sql

from lakebase_starter import pg, uc
from lakebase_starter.config import REPO_ROOT, load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.ui import explain, heading, ok, skip, step, warn

# CDF config IDs use underscores ([a-z][a-z0-9_]*), unlike project and branch IDs, which use hyphens.
CDF_CONFIG_ID = "store_to_unity_catalog"

cfg = load_config()
w = workspace_client(cfg)
history = uc.ident(cfg.catalog, cfg.history_schema)  # quoted, for SQL

heading("Step 8: stream Lakebase -> Unity Catalog (Lakebase Change Data Feed)")

step("Checking the Postgres tables are ready for CDF")
conn = pg.connect(w, cfg)
tables = conn.execute(
    """
    SELECT c.relname, c.relreplident = 'f' AS full_identity, c.reltuples
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'store' AND c.relkind = 'r'
    ORDER BY c.relname
    """
).fetchall()
for name, full_identity, _ in tables:
    rows = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("store", name))).fetchone()[0]
    if not full_identity:
        raise SystemExit(f"store.{name} needs REPLICA IDENTITY FULL. Run scripts/05_run_migrations.py.")
    if rows == 0:
        raise SystemExit(f"store.{name} is empty, and CDF skips empty tables. Run scripts/07_app_places_orders.py.")
    ok(f"store.{name}: REPLICA IDENTITY FULL, {rows} rows")
conn.close()

step(f"Destination schema {history}")
uc.run(
    w,
    cfg.warehouse_id,
    f"CREATE SCHEMA IF NOT EXISTS {history} "
    "COMMENT 'Acme store: change history streamed from Lakebase by Lakebase Change Data Feed'",
)
ok("ready")

step("Starting the change data feed for Postgres schema `store`")
# The API wants the database's *resource path*, not its Postgres name.
# The Postgres database databricks_postgres has the resource ID databricks-postgres.
database = next(
    d.name for d in w.postgres.list_databases(parent=cfg.branch()) if d.status.postgres_database == cfg.database
)
try:
    existing = [c for c in w.postgres.list_cdf_configs(parent=database) if c.postgres_schema == "store"]
except NotFound:  # this API returns 404 rather than an empty list when there are no configs
    existing = []
if existing:
    cdf_config = existing[0]
    skip(f"already running ({cdf_config.name})")
else:
    cdf_config = w.postgres.create_cdf_config(
        parent=database,
        cdf_config_id=CDF_CONFIG_ID,
        cdf_config=CdfConfig(catalog=cfg.catalog, schema=cfg.history_schema, postgres_schema="store"),
    ).wait()
    ok(f"created {cdf_config.name}")

step("Waiting for each table to finish its first snapshot and start streaming")
explain(
    """First CDF copies the rows that already exist (SNAPSHOTTING). Then it follows
    the change log (STREAMING). In our tests both tables were streaming within
    seconds; allow longer for big tables."""
)
deadline = time.time() + 20 * 60
while True:
    # Per-table statuses live under the CDF config, not under the database.
    try:
        statuses = list(w.postgres.list_cdf_statuses(parent=cdf_config.name))
    except NotFound:
        statuses = []
    states = {s.postgres_table: s.state for s in statuses}
    print("    " + (", ".join(f"{t}={s.value if s else '?'}" for t, s in sorted(states.items())) or "no tables yet"))
    streaming = [t for t, s in states.items() if s == CdfState.CDF_STATE_STREAMING]
    if len(streaming) >= len(tables):
        break
    bad = {t: s for t, s in states.items() if s in (CdfState.CDF_STATE_SKIPPED, CdfState.CDF_STATE_TERMINATED)}
    if bad:
        details = {s.postgres_table: s.status_detail for s in statuses if s.postgres_table in bad}
        raise SystemExit(f"CDF problem: {details}")
    if time.time() > deadline:
        raise SystemExit("Timed out after 20 minutes. Check the Lakebase CDF tab on the branch in the UI.")
    time.sleep(15)
ok("all tables are streaming")

step("Building current-state views on top of the history (uc_sql/02_history_views.sql)")
values = {
    "catalog": uc.ident(cfg.catalog),
    "history_schema": uc.ident(cfg.history_schema),
    "gold_schema": uc.ident(cfg.gold_schema),
}
# The history tables can take a few seconds to show up in Unity Catalog after
# the first flush, so retry briefly.
for attempt in range(12):
    try:
        uc.run_file(w, cfg.warehouse_id, REPO_ROOT / "uc_sql" / "02_history_views.sql", values)
        break
    except RuntimeError as error:
        if "TABLE_OR_VIEW_NOT_FOUND" not in str(error) or attempt == 11:
            raise
        time.sleep(10)
ok("orders_current, order_items_current, daily_revenue_by_category")

step("Granting the analysts group read access in Unity Catalog")
try:
    group = uc.ident(cfg.analysts_group)
    uc.run(w, cfg.warehouse_id, f"GRANT USE CATALOG ON CATALOG {uc.ident(cfg.catalog)} TO {group}")
    uc.run(w, cfg.warehouse_id, f"GRANT USE SCHEMA, SELECT ON SCHEMA {history} TO {group}")
    ok(f"{cfg.analysts_group}: USE CATALOG, plus USE SCHEMA and SELECT on {history}")
except RuntimeError as error:
    if "PRINCIPAL_DOES_NOT_EXIST" not in str(error):
        raise
    warn(f"Unity Catalog can't see '{cfg.analysts_group}', so this grant was skipped.")
    explain(
        """Unity Catalog only recognises account-level groups. A group created with the
        workspace API in a test workspace (as step 2 does) can be workspace-local:
        Lakebase accepts it, Unity Catalog doesn't. In production, use an account-level
        group, usually synced from your identity provider, and this grant works."""
    )
explain(
    """Unity Catalog permissions are a third, separate layer. Being store_reader in
    Postgres gives no access to the Delta history tables, and the other way round."""
)

step("What Unity Catalog sees")
for change_type, count in uc.run(
    w,
    cfg.warehouse_id,
    f"SELECT _pg_change_type, count(*) FROM {history}.lb_orders_history GROUP BY 1 ORDER BY 1",
):
    print(f"    lb_orders_history  {change_type:<17} {count}")
for row in uc.run(
    w,
    cfg.warehouse_id,
    f"SELECT order_date, category, orders, revenue FROM {history}.daily_revenue_by_category ORDER BY revenue DESC LIMIT 5",
):
    print(f"    daily_revenue  {row[0]}  {row[1]:<11} orders {row[2]:>3}  revenue {row[3]}")

print("\nNext: python scripts/09_verify_roles.py")
