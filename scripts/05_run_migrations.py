"""Step 5: run schema migrations as the deployer service principal.

This is what your CI/CD pipeline does on every release: sign in as the
deployer service principal (machine-to-machine OAuth, no human involved) and
apply the SQL files in sql/migrations/ in order.

Each migration starts with `SET ROLE store_owner`, so the tables belong to the
store_owner group role and not to the service principal. If you later replace
the deployer service principal, nothing has to change hands.

The migrations are written to be re-runnable (CREATE ... IF NOT EXISTS), so
running this script twice is harmless. For a real project, consider a
migration tool (Alembic, Flyway, Liquibase, sqitch) that records which
migrations have run.

Usage:
  python scripts/05_run_migrations.py                 # production branch
  python scripts/05_run_migrations.py --branch dev    # any other branch
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from lakebase_starter import pg
from lakebase_starter.config import REPO_ROOT, load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.ui import heading, ok, step

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--branch", default="production", help="branch ID to migrate (default: production)")
args = parser.parse_args()

cfg = load_config()
w = workspace_client(cfg)

heading(f"Step 5: schema migrations on branch '{args.branch}'")

step("Signing in as the deployer service principal")
deployer = sign_in_as_service_principal(w, cfg, "deployer")
conn = pg.connect(deployer, cfg, branch_id=args.branch)
ok(f"connected to Postgres as {conn.execute('SELECT current_user').fetchone()[0]}")

step("Applying sql/migrations/*.sql in order")
for path in sorted((REPO_ROOT / "sql" / "migrations").glob("*.sql")):
    pg.run_sql_file(conn, path)
    ok(path.name)

step("Tables in schema `store`")
rows = conn.execute(
    """
    SELECT c.relname,
           pg_get_userbyid(c.relowner) AS owner,
           CASE c.relreplident WHEN 'f' THEN 'full' WHEN 'd' THEN 'default'
                               WHEN 'n' THEN 'nothing' WHEN 'i' THEN 'index' END AS replica_identity,
           has_table_privilege('store_reader', c.oid, 'SELECT') AS reader_select,
           has_table_privilege('store_reader', c.oid, 'INSERT') AS reader_insert,
           has_table_privilege('store_writer', c.oid, 'INSERT') AS writer_insert
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'store' AND c.relkind = 'r'
    ORDER BY c.relname
    """
).fetchall()
print(f"    {'table':<13} {'owner':<13} {'replica id':<11} {'reader SELECT':<14} {'reader INSERT':<14} writer INSERT")
for name, owner, replident, r_sel, r_ins, w_ins in rows:
    print(f"    {name:<13} {owner:<13} {replident:<11} {str(r_sel):<14} {str(r_ins):<14} {w_ins}")
conn.close()

print("\nNext: python scripts/06_sync_uc_to_lakebase.py")
