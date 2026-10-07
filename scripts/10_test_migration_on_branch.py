"""Step 10: test a schema change on a branch before it touches production.

A Lakebase *branch* is an instant, copy-on-write clone of a database: all the
data, schemas, roles and grants, as of the moment you branch. Creating one
takes seconds and costs nothing until you change data in it. That makes it
the safe place to rehearse a production migration on real data.

What this script does
---------------------
1. Creates branch `migration-test` from production. It expires on its own
   after 4 hours, so a forgotten test branch doesn't linger.
2. Shrinks the branch's compute and lets it scale to zero when idle, so the
   test branch doesn't run (and bill) like production.
3. Runs the proposed migration (sql/proposed/002_add_delivery_note.sql) on the
   branch, as the deployer service principal, exactly as CI/CD would.
4. Tests it: the app service principal places an order that uses the new
   column. Production is untouched at this point.
5. Checks that the test order did NOT reach Unity Catalog: the change data
   feed follows the production branch only.
6. Applies the same migration to production and deletes the test branch.

Usage:
  python scripts/10_test_migration_on_branch.py               # test, then apply to production
  python scripts/10_test_migration_on_branch.py --test-only   # test, leave production alone
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import Branch, BranchSpec, Endpoint, EndpointSpec, EndpointType
from google.protobuf.duration_pb2 import Duration

from lakebase_starter import pg, uc
from lakebase_starter.config import REPO_ROOT, load_config
from lakebase_starter.identities import sign_in_as_service_principal
from lakebase_starter.ui import explain, heading, ok, skip, step

BRANCH_ID = "migration-test"
MIGRATION_NAME = "002_add_delivery_note.sql"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--test-only", action="store_true", help="don't apply the migration to production")
args = parser.parse_args()

# The migration starts in sql/proposed/. After you've applied it to production,
# the docs suggest moving it to sql/migrations/, so look in both places.
candidates = [REPO_ROOT / "sql" / folder / MIGRATION_NAME for folder in ("proposed", "migrations")]
MIGRATION = next((path for path in candidates if path.exists()), None)
if MIGRATION is None:
    raise SystemExit(f"Can't find {MIGRATION_NAME} in sql/proposed/ or sql/migrations/.")

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)
deployer = sign_in_as_service_principal(w, cfg, "deployer")
app = sign_in_as_service_principal(w, cfg, "app")


def has_delivery_note(branch_id: str) -> bool:
    with pg.connect(deployer, cfg, branch_id=branch_id) as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'store' AND table_name = 'orders' AND column_name = 'delivery_note'"
            ).fetchone()
        )


heading(f"Step 10: test {MIGRATION.name} on a branch first")
if has_delivery_note("production"):
    explain(
        """Production already has this migration (an earlier run applied it). This run
        still rehearses the whole branch workflow, but the migration itself is a no-op."""
    )

step(f"Creating branch '{BRANCH_ID}' from production (expires in 4 hours)")
try:
    w.postgres.get_branch(name=cfg.branch(BRANCH_ID))
    skip("already exists")
except NotFound:
    w.postgres.create_branch(
        parent=cfg.project,
        branch_id=BRANCH_ID,
        branch=Branch(spec=BranchSpec(source_branch=cfg.branch(), ttl=Duration(seconds=4 * 3600))),
    ).wait()
    ok("created: a full copy of production's data, roles and grants, made in seconds")

step("Making the branch's compute small and able to scale to zero")
small = EndpointSpec(
    endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
    autoscaling_limit_min_cu=0.5,
    autoscaling_limit_max_cu=1,
    suspend_timeout_duration=Duration(seconds=300),  # scale to zero after 5 idle minutes
)
existing = list(w.postgres.list_endpoints(parent=cfg.branch(BRANCH_ID)))
if existing:
    current = existing[0].status
    suspend = current.suspend_timeout_duration
    ok(
        f"the branch came with a compute: {current.autoscaling_limit_min_cu}-"
        f"{current.autoscaling_limit_max_cu} CU, scale-to-zero "
        f"{'after ' + str(suspend.seconds) + 's' if suspend and suspend.seconds > 0 else 'off'}"
    )
    w.postgres.update_endpoint(
        name=existing[0].name,
        endpoint=Endpoint(spec=small),
        update_mask=FieldMask(
            field_mask=["spec.autoscaling_limit_min_cu", "spec.autoscaling_limit_max_cu", "spec.suspension"]
        ),
    ).wait()
else:
    w.postgres.create_endpoint(
        parent=cfg.branch(BRANCH_ID), endpoint_id="primary", endpoint=Endpoint(spec=small)
    ).wait()
now = list(w.postgres.list_endpoints(parent=cfg.branch(BRANCH_ID)))[0].status
suspend = now.suspend_timeout_duration
ok(
    f"now {now.autoscaling_limit_min_cu}-{now.autoscaling_limit_max_cu} CU, scale-to-zero "
    f"{'after ' + str(suspend.seconds) + 's' if suspend and suspend.seconds > 0 else 'off'}"
)

step("Running the migration on the branch, as the deployer service principal")
production_before = has_delivery_note("production")
with pg.connect(deployer, cfg, branch_id=BRANCH_ID) as conn:
    pg.run_sql_file(conn, MIGRATION)
ok(f"branch has delivery_note: {has_delivery_note(BRANCH_ID)}")
if has_delivery_note("production") != production_before:
    raise SystemExit("Unexpected: running the migration on the branch changed production.")
ok(f"production is unchanged by the branch (delivery_note present: {production_before})")

step("Testing the change on the branch, as the app service principal")
marker = f"branch-test-{uuid.uuid4().hex[:8]}@example.com"
with pg.connect(app, cfg, branch_id=BRANCH_ID) as conn, conn.transaction():
    order_id = conn.execute(
        "INSERT INTO store.orders (customer_email, total_amount, delivery_note) "
        "VALUES (%s, 0, 'Leave it with the neighbour') RETURNING order_id",
        (marker,),
    ).fetchone()[0]
ok(f"the app wrote order {order_id} with a delivery note on the branch")

step("Checking the branch's test data stayed out of Unity Catalog")
explain(
    """The change data feed was set up on the production branch, so writes to other
    branches must not appear in the history tables. We give the feed a minute to
    catch up, then look for the test order."""
)
time.sleep(60)
leaked = uc.run(
    w,
    cfg.warehouse_id,
    f"SELECT count(*) FROM {uc.ident(cfg.catalog, cfg.history_schema, 'lb_orders_history')} "
    f"WHERE customer_email = '{marker}'",
)[0][0]
if leaked != "0":
    raise SystemExit(f"Unexpected: the branch's test order reached Unity Catalog ({leaked} rows).")
ok("no trace of the test order in Unity Catalog")

if args.test_only:
    skip(f"--test-only: production not changed. The branch '{BRANCH_ID}' expires on its own in 4 hours.")
    sys.exit(0)

step("Applying the same migration to production")
with pg.connect(deployer, cfg) as conn:
    pg.run_sql_file(conn, MIGRATION)
ok(f"production has delivery_note: {has_delivery_note('production')}")
explain(
    f"""If {MIGRATION.name} is still in sql/proposed/, move it to sql/migrations/ and
    commit it, so new environments get the column too. Adding a column makes Lakebase
    Change Data Feed re-copy that table into Unity Catalog, with the new column."""
)

step(f"Deleting branch '{BRANCH_ID}'")
w.postgres.delete_branch(name=cfg.branch(BRANCH_ID)).wait()
ok("deleted")

print("\nNext: python scripts/11_verify_end_to_end.py")
