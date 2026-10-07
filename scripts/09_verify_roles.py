"""Step 9: prove the permissions work by trying things as each identity.

Every identity logs in for real (OAuth for the Databricks identities, a
password for bi_reader) and attempts the same operations. Each attempt runs
in a transaction that is always rolled back, so this script changes nothing.
The one heavy operation is the deployer's ALTER TABLE, which needs a brief
exclusive lock on store.orders; it gives up after 1 second if the table is
busy. On a very busy production database, run it at a quiet time.

You should see a grid like this, where every result matches what we expect:

    operation                      analysts   bi_reader   app (writer)   deployer (owner)
    read store.orders              allowed    allowed     allowed        allowed
    read store_serving.products    allowed    allowed     allowed        denied
    insert into store.orders       denied     denied      allowed        allowed
    update a synced product        denied     denied      denied         denied
    alter table store.orders       denied     denied      denied         allowed
    create a table in store        denied     denied      denied         allowed

Two more checks follow: the app service principal must NOT be able to log in
as the analysts group (it isn't a member), and bi_reader's guard rails
(connection limit, statement timeout) must be in place.

Exits with an error if anything doesn't match, so you can run it in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg
from psycopg import errors, sql

from lakebase_starter import pg
from lakebase_starter.config import load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.secret_scope import get_secret
from lakebase_starter.ui import heading, ok, step, warn

cfg = load_config()
w = workspace_client(cfg)
products = sql.Identifier(cfg.serving_schema, "products")

OPERATIONS = [
    ("read store.orders", sql.SQL("SELECT count(*) FROM store.orders")),
    ("read store_serving.products", sql.SQL("SELECT count(*) FROM {}").format(products)),
    (
        "insert into store.orders",
        sql.SQL("INSERT INTO store.orders (customer_email, total_amount) VALUES ('roles-test@example.com', 0)"),
    ),
    ("update a synced product", sql.SQL("UPDATE {} SET price = price WHERE product_id = 1001").format(products)),
    ("alter table store.orders", sql.SQL("ALTER TABLE store.orders ADD COLUMN roles_test integer")),
    ("create a table in store", sql.SQL("CREATE TABLE store.roles_test (id integer)")),
]

# What each identity should be able to do, in the order of OPERATIONS.
EXPECTED = {
    "analysts": [True, True, False, False, False, False],
    "bi_reader": [True, True, False, False, False, False],
    "app (writer)": [True, True, True, False, False, False],
    "deployer (owner)": [True, False, True, False, True, True],
}


def attempt(conn: psycopg.Connection, statement: sql.Composable) -> bool:
    """True if the statement is allowed. The transaction is always rolled back."""
    try:
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL lock_timeout = '1s'")  # don't queue behind the live app
            conn.execute(statement)
        return True
    except errors.InsufficientPrivilege:
        return False


heading("Step 9: verify roles by trying things as each identity")

step("Logging in as each identity")
app = sign_in_as_service_principal(w, cfg, "app")
deployer = sign_in_as_service_principal(w, cfg, "deployer")
try:
    # Group-based login: YOUR token, but the group's role name as the user name.
    analysts = pg.connect(w, cfg, role=cfg.analysts_group)
except psycopg.OperationalError:
    raise SystemExit(
        f"Couldn't log in as the '{cfg.analysts_group}' group role. This check uses your own token, "
        "so you must be a member of that Databricks group. Add yourself to it and run this again."
    ) from None
connections = {
    "analysts": analysts,
    "app (writer)": pg.connect(app, cfg),
    "deployer (owner)": pg.connect(deployer, cfg),
}
if cfg.enable_password_login:
    connections["bi_reader"] = pg.connect_with_password(
        w, cfg, role=cfg.password_role, password=get_secret(w, cfg.secret_scope, f"{cfg.password_role}-password")
    )
for label, conn in connections.items():
    current_user, session_user = conn.execute("SELECT current_user, session_user").fetchone()
    ok(f"{label:<17} logged in as {current_user}")

step("Trying each operation")
labels = [label for label in EXPECTED if label in connections]
print(f"    {'operation':<30}" + "".join(f"{label:<19}" for label in labels))
mismatches = []
for index, (operation, statement) in enumerate(OPERATIONS):
    cells = []
    for label in labels:
        allowed = attempt(connections[label], statement)
        expected = EXPECTED[label][index]
        mark = "allowed" if allowed else "denied"
        if allowed != expected:
            mark += " (!)"
            mismatches.append(f"{label}: '{operation}' was {'allowed' if allowed else 'denied'}")
        cells.append(f"{mark:<19}")
    print(f"    {operation:<30}" + "".join(cells))

step("The app service principal must not be able to log in as the analysts group")
try:
    pg.connect(app, cfg, role=cfg.analysts_group).close()
    mismatches.append("the app service principal logged in as the analysts group role")
    warn("it could. That is wrong.")
except psycopg.OperationalError as error:
    reason = next((line.split("ERROR:", 1)[1].strip() for line in str(error).splitlines() if "ERROR:" in line), "")
    ok(f"rejected, as expected ({reason or 'connection refused'})")

if "bi_reader" in connections:
    step("bi_reader guard rails")
    conn = connections["bi_reader"]
    timeout = conn.execute("SHOW statement_timeout").fetchone()[0]
    limit = conn.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user").fetchone()[0]
    if timeout == "1min" and limit == 5:
        ok(f"statement_timeout = {timeout}, connection limit = {limit}")
    else:
        mismatches.append(f"bi_reader guard rails: statement_timeout={timeout}, connection limit={limit}")

for conn in connections.values():
    conn.close()

if mismatches:
    print("\nFAILED. These results don't match the intended permissions:")
    for mismatch in mismatches:
        print(f"  - {mismatch}")
    sys.exit(1)
print("\nAll permission checks passed.")
print("\nNext: python scripts/10_test_migration_on_branch.py")
