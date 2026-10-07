# Databricks notebook source
# MAGIC %md
# MAGIC # Lakebase in production: a guided walkthrough
# MAGIC
# MAGIC This notebook runs the [Lakebase production starter](https://github.com/deepbasu123/lakebase-production-starter) step by step, inside your Databricks workspace. Each step runs the same script you'd run from a laptop or a CI/CD pipeline, then shows you what it built.
# MAGIC
# MAGIC **What you'll end up with**
# MAGIC
# MAGIC * A Lakebase project, `acme-store`, with production settings: Postgres 17, a protected production branch, autoscaling with scale-to-zero off.
# MAGIC * Two permission layers. Project permissions decide who can manage the infrastructure. Postgres roles decide who can touch the data: a CI/CD service principal, an app service principal, an analysts group and a password role for a BI tool.
# MAGIC * Data flowing both ways. A **synced table** copies `products` from Unity Catalog into Postgres. **Lakebase Change Data Feed** streams every change to `orders` back into Unity Catalog.
# MAGIC * Proof: a permission matrix, a migration rehearsed on a branch, and a deleted order recovered with point-in-time recovery.
# MAGIC
# MAGIC **Before you run it**
# MAGIC
# MAGIC * Open this notebook from a **Git folder** of the repo (Workspace > Create > Git folder, then paste the repo URL), so it can find the scripts.
# MAGIC * You need: workspace admin (step 2 creates service principals and a group), `CREATE SCHEMA` on a catalog that doesn't use default storage, a SQL warehouse, and Lakebase Change Data Feed switched on by a workspace admin on the **Previews** page.
# MAGIC * **Run step 2 once from a terminal before you start** (`python scripts/02_create_demo_identities.py`), or have the identities and their secrets ready ([Bring your own identities](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/03-identities-and-project-permissions.md#bring-your-own-identities)). In our test, creating service principal OAuth secrets was refused with the notebook's credentials. See the step 2 cell below.
# MAGIC * It creates real, billable resources. The last cell shows a clean-up plan, and removes everything only if you switch its widget. (We tested the delete itself from a laptop.)
# MAGIC * The workspace must be able to reach github.com to create the Git folder. If it can't, import the repo's files another way.
# MAGIC * Names are fixed (project `acme-store`, the service principals, the group, the schemas). If several people use one workspace, give each person their own **project_id**, **catalog** and **secret_scope**.
# MAGIC
# MAGIC Run the cells in order. You can re-run any step: each one skips what already exists. Steps 7, 11 and 12 add a few test orders each time they run.
# MAGIC
# MAGIC Each script ends with a hint such as `Next: python scripts/...`. That's for people running it from a terminal; here, just run the next cell.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Install the Python packages
# MAGIC The Databricks SDK (with the Lakebase API) and psycopg, the Postgres driver. Python restarts afterwards so the new versions are used.
# MAGIC
# MAGIC pip may print an `ERROR: pip's dependency resolver ...` line about `protobuf` and another preinstalled package. In our test it was harmless: everything below ran fine.

# COMMAND ----------

# MAGIC %pip install -q -r ../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Settings
# MAGIC Run the next cell once: it creates the widgets at the top of the notebook and stops, asking for **warehouse_id**. Fill in **catalog** (one that doesn't use default storage) and **warehouse_id**, then run the cell again. Everything else has a sensible default, and every setting is explained in [config.toml](https://github.com/deepbasu123/lakebase-production-starter/blob/main/config.toml).

# COMMAND ----------

import json
import os
import runpy
import sys
from pathlib import Path

dbutils.widgets.text("catalog", "main", "1. Unity Catalog catalog")
dbutils.widgets.text("warehouse_id", "", "2. SQL warehouse ID")
dbutils.widgets.text("project_id", "acme-store", "3. Lakebase project ID")
dbutils.widgets.text("secret_scope", "acme-store", "4. Secret scope")
dbutils.widgets.dropdown("teardown", "plan only", ["plan only", "delete everything"], "5. Last cell")

settings = {name: dbutils.widgets.get(name).strip() for name in ("catalog", "warehouse_id", "project_id", "secret_scope")}
if not settings["warehouse_id"]:
    raise ValueError("Set the warehouse_id widget (SQL Warehouses > your warehouse > Connection details).")

# The repo root is two levels up from this notebook: <repo>/notebooks/<this notebook>.
notebook_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
REPO = Path("/Workspace" + notebook_path).parent.parent
sys.path.insert(0, str(REPO))

# Write a config file for the scripts. profile is empty: inside Databricks they
# sign in as whoever runs this notebook.
q = json.dumps  # JSON strings are valid TOML strings
config_text = f"""
[databricks]
profile = ""
warehouse_id = {q(settings["warehouse_id"])}

[lakebase]
project_id = {q(settings["project_id"])}
display_name = "Acme Store (production)"
pg_version = 17
min_cu = 1
max_cu = 4
history_retention_days = 7
enable_password_login = true

[unity_catalog]
catalog = {q(settings["catalog"])}
gold_schema = "store_gold"
serving_schema = "store_serving"
history_schema = "store_history"

[identities]
deployer_sp = "acme-store-deployer"
app_sp = "acme-store-app"
analysts_group = "acme-store-analysts"
password_role = "bi_reader"
secret_scope = {q(settings["secret_scope"])}
"""
config_file = Path("/tmp/lakebase_starter_config.toml")
config_file.write_text(config_text)
os.environ["LAKEBASE_STARTER_CONFIG"] = str(config_file)


def run_step(script: str, *args: str) -> None:
    """Run one of the repo's scripts, exactly as it runs from a terminal."""
    path = REPO / "scripts" / script
    saved_argv = sys.argv
    sys.argv = [str(path), *args]
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as stop:
        if stop.code not in (None, 0):
            raise RuntimeError(f"{script} stopped: {stop.code}") from None
    finally:
        sys.argv = saved_argv


from lakebase_starter import pg, uc
from lakebase_starter.config import load_config
from lakebase_starter.workspace import workspace_client

cfg = load_config()
w = workspace_client(cfg)
history = uc.ident(cfg.catalog, cfg.history_schema)  # the Unity Catalog schema Lakebase Change Data Feed writes to
print(f"Repo: {REPO}\nSigned in as: {w.current_user.me().user_name}\nProject: {cfg.project}")

# COMMAND ----------

import pandas as pd


def postgres_table(query: str, params: tuple = ()) -> pd.DataFrame:
    """Run a query in Lakebase as you, and return the rows as a table."""
    with pg.connect(w, cfg) as conn:
        cursor = conn.execute(query, params or None)
        return pd.DataFrame(cursor.fetchall(), columns=[column.name for column in cursor.description])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 0: check the prerequisites
# MAGIC Checks your sign-in, whether you're a workspace admin, the warehouse, the catalog and your permissions on it, and whether the project name is free. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/01-before-you-start.md)

# COMMAND ----------

run_step("00_check_prerequisites.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1: create the project with production settings
# MAGIC A **project** is one Postgres service. Creating it also creates the `production` **branch** (your live data) and its `primary` **compute** (the Postgres server). The script protects the branch, sets autoscaling and turns scale-to-zero off. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/02-create-a-production-project.md)

# COMMAND ----------

run_step("01_create_project.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Steps 2 and 3: identities and project permissions (layer 1)
# MAGIC Step 2 creates two service principals (one for CI/CD, one for the app), an analysts group with you in it, and a secret scope for their OAuth secrets. Skip it if you bring your own identities.
# MAGIC
# MAGIC **Run step 2 once from a terminal first.** In our test, creating the service principals' OAuth secrets with the notebook's own credentials came back `PermissionDenied`, while the same call worked from a laptop after `databricks auth login`. So run `python scripts/02_create_demo_identities.py` from your laptop (or follow the error message's UI route). After that, this cell finds everything in place and skips it.
# MAGIC
# MAGIC Step 3 grants **project permissions**, which control the *infrastructure* (branches, compute, settings). Only CI/CD gets one. The app and the analysts don't need any, because logging in to Postgres is decided by layer 2. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/03-identities-and-project-permissions.md)

# COMMAND ----------

run_step("02_create_demo_identities.py")
run_step("03_grant_project_permissions.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Steps 4 and 5: Postgres roles, schema and tables (layer 2)
# MAGIC Three **group roles** describe jobs: `store_owner` (owns the tables), `store_writer` (changes rows), `store_reader` (reads). Nobody logs in as them. Each application, pipeline and tool identity gets exactly one of them as a membership; you, the project owner, also keep admin rights. Then CI/CD, as the deployer service principal, runs the migration that creates the tables, owned by `store_owner`. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/04-database-roles-schemas-and-grants.md)

# COMMAND ----------

run_step("04_setup_database.py")
run_step("05_run_migrations.py")

# COMMAND ----------

# MAGIC %md
# MAGIC Who is a member of which group role, straight from Postgres:

# COMMAND ----------

display(postgres_table("""
    SELECT m.rolname AS login_role, string_agg(DISTINCT g.rolname, ', ' ORDER BY g.rolname) AS member_of
    FROM pg_auth_members am
    JOIN pg_roles g ON g.oid = am.roleid
    JOIN pg_roles m ON m.oid = am.member
    WHERE g.rolname IN ('store_owner', 'store_writer', 'store_reader', 'databricks_superuser')
      AND (am.inherit_option OR am.set_option)
    GROUP BY m.rolname
    ORDER BY m.rolname
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6: sync Unity Catalog into Lakebase (synced table)
# MAGIC The product catalog lives in the lakehouse as a Delta table. A **synced table** keeps a read-only copy in Postgres, where the app can read it in milliseconds. The first sync takes a few minutes. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/05-sync-unity-catalog-to-lakebase.md)

# COMMAND ----------

run_step("06_sync_uc_to_lakebase.py")

# COMMAND ----------

# MAGIC %md
# MAGIC The copy in Postgres (`store_serving.products`), queried with psycopg:

# COMMAND ----------

display(postgres_table(f'SELECT product_id, name, category, price, in_stock FROM "{cfg.serving_schema}".products ORDER BY product_id'))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7: the app takes orders
# MAGIC Plays the part of the application: it signs in as the app service principal, uses a connection pool that fetches a fresh one-hour OAuth token for each new connection, reads prices from the synced table and writes orders. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/06-connect-an-application.md)

# COMMAND ----------

run_step("07_app_places_orders.py")

# COMMAND ----------

display(postgres_table("SELECT order_id, customer_email, status, total_amount, created_at FROM store.orders ORDER BY order_id DESC LIMIT 10"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8: stream Lakebase changes into Unity Catalog (Lakebase Change Data Feed)
# MAGIC Lakebase Change Data Feed reads Postgres' change log and writes every insert, update and delete in the `store` schema to Delta tables, about every 15 seconds. No pipeline to run. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/07-sync-lakebase-to-unity-catalog.md)
# MAGIC
# MAGIC Expect one `[warn]` line here when you use the group that step 2 creates: Unity Catalog can't grant to a workspace-local group, so the analysts' Unity Catalog grant is skipped. With an account-level group it goes through.

# COMMAND ----------

run_step("08_sync_lakebase_to_uc.py")

# COMMAND ----------

# MAGIC %md
# MAGIC The change history in Unity Catalog, one row per change, read with Spark:

# COMMAND ----------

display(spark.sql(f"""
    SELECT order_id, status, total_amount, _pg_change_type, _timestamp
    FROM {history}.lb_orders_history
    ORDER BY _sort_by DESC
    LIMIT 20
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC And the payoff of syncing both ways: orders from the app joined with products from the lakehouse, in one query.

# COMMAND ----------

display(spark.sql(f"SELECT * FROM {history}.daily_revenue_by_category ORDER BY order_date DESC, revenue DESC"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9: prove the permissions
# MAGIC Every identity logs in for real and tries the same six operations, inside transactions that are always rolled back. The grid it prints should show the app able to insert orders but not alter tables, the readers able to read only, and nobody able to edit the synced table. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/08-prove-the-permissions.md)

# COMMAND ----------

run_step("09_verify_roles.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 10: test a migration on a branch before production
# MAGIC Creates an instant copy-on-write **branch** of production, runs the proposed migration there as CI/CD would, tests it as the app, checks the test data stayed out of Unity Catalog, then applies it to production and deletes the branch. [Guide](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/09-day-2-operations.md)

# COMMAND ----------

run_step("10_test_migration_on_branch.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 11: round trip in both directions
# MAGIC Changes a price in Unity Catalog and watches it reach Postgres. Then the app places and pays an order, and the script watches the change land in Unity Catalog.

# COMMAND ----------

run_step("11_verify_end_to_end.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 12: recover a deleted order (point-in-time recovery)
# MAGIC The app deletes an order by mistake. The script creates a branch of production *as it was a few seconds earlier*, copies the order back and deletes the branch. Production keeps running throughout.

# COMMAND ----------

run_step("12_recover_deleted_data.py")

# COMMAND ----------

# MAGIC %md
# MAGIC Lakebase Change Data Feed recorded the whole story for that order: placed, paid, deleted by mistake, then restored.

# COMMAND ----------

import time

restored = int(postgres_table("SELECT max(order_id) AS order_id FROM store.orders")["order_id"][0])
query = f"""
    SELECT order_id, status, _pg_change_type, _timestamp
    FROM {history}.lb_orders_history
    WHERE order_id = {restored}
    ORDER BY _sort_by
"""
# The feed writes in batches about every 15 seconds, so give the restore a moment to arrive.
for _ in range(12):
    changes = [row["_pg_change_type"] for row in spark.sql(query).collect()]
    if "delete" in changes and changes[-1] == "insert":
        break
    time.sleep(10)
display(spark.sql(query))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Clean up
# MAGIC With the **5. Last cell** widget on *plan only* (the default), this prints what would be deleted and deletes nothing. (It says "Re-run with --yes"; in this notebook, that means switching the widget.) Switch it to *delete everything* and run the cell again to remove the project, the Unity Catalog schemas, the service principals, the group and the secret scope. The project is purged straight away, so its name is free again. [Details](https://github.com/deepbasu123/lakebase-production-starter/blob/main/docs/09-day-2-operations.md#clean-up)

# COMMAND ----------

if dbutils.widgets.get("teardown") == "delete everything":
    run_step("99_teardown.py", "--yes", "--purge", "--delete-identities")
else:
    run_step("99_teardown.py", "--purge", "--delete-identities")
