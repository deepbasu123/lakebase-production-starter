"""Remove everything this example created.

Without --yes this only prints the plan. Nothing is deleted.

Safety checks, so a typo in your config can't delete someone else's work:
  * The project must carry the tag managed_by=lakebase-production-starter,
    which step 1 sets. Other projects are left alone.
  * A Unity Catalog schema is only dropped if its comment starts with
    "Acme store:", which steps 6 and 8 set. Other schemas are left alone.
  * Identities are only deleted with --delete-identities, and only when the
    project check passed.
  --force skips these checks. Don't use it unless you're sure.

Order matters:
  1. Stop the change data feed (the Delta history tables stay until step 3).
  2. Delete the synced table, so its pipeline stops.
  3. Drop the Unity Catalog schemas the example created (gold, serving, history).
  4. Unprotect the production branch. Lakebase won't delete a project that has
     a protected branch.
  5. Delete the project. By default this is a soft delete: you can restore the
     project for 7 days, and its ID stays reserved until then. --purge deletes
     it immediately and for good.
  6. With --delete-identities: delete the secret scope, both service principals
     and the analysts group. Only use this in a test workspace where step 2
     created them.

Usage:
  python scripts/99_teardown.py                                   # show the plan
  python scripts/99_teardown.py --yes                             # soft delete
  python scripts/99_teardown.py --yes --purge --delete-identities # remove everything now
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import Branch, BranchSpec

from lakebase_starter import uc
from lakebase_starter.config import load_config
from lakebase_starter.workspace import workspace_client
from lakebase_starter.ui import heading, ok, skip, step, warn

TAG = ("managed_by", "lakebase-production-starter")
SCHEMA_COMMENT_PREFIX = "Acme store:"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--yes", action="store_true", help="actually delete things")
parser.add_argument("--purge", action="store_true", help="delete the project permanently, no 7-day recovery")
parser.add_argument("--delete-identities", action="store_true", help="also delete the demo SPs, group and secret scope")
parser.add_argument("--force", action="store_true", help="skip the safety checks (dangerous)")
args = parser.parse_args()

cfg = load_config()
w = workspace_client(cfg)
synced_table = f"synced_tables/{cfg.catalog}.{cfg.serving_schema}.products"

heading("Teardown" + ("" if args.yes else " (plan only, nothing will be deleted)"))

# ---------------------------------------------------------------------------
# Decide what is safe to delete
# ---------------------------------------------------------------------------
try:
    project = w.postgres.get_project(name=cfg.project)
    tags = {t.key: t.value for t in project.status.custom_tags or []}
    project_ours = tags.get(TAG[0]) == TAG[1] or args.force
    print(f"    Lakebase project : {cfg.project} -> ", end="")
    if project_ours:
        print("delete (" + ("purge" if args.purge else "soft delete, recoverable for 7 days") + ")")
    else:
        print(f"KEEP: it has no {TAG[0]}={TAG[1]} tag, so this example didn't create it")
except NotFound:
    project = None
    project_ours = False
    print(f"    Lakebase project : {cfg.project} -> not found")

schemas_to_drop = []
for schema in (cfg.history_schema, cfg.serving_schema, cfg.gold_schema):
    full_name = f"{cfg.catalog}.{schema}"
    try:
        comment = w.schemas.get(full_name=full_name).comment or ""
    except NotFound:
        print(f"    UC schema        : {full_name} -> not found")
        continue
    if comment.startswith(SCHEMA_COMMENT_PREFIX) or args.force:
        schemas_to_drop.append(schema)
        print(f"    UC schema        : {full_name} -> drop, with every table in it")
    else:
        print(f"    UC schema        : {full_name} -> KEEP: its comment doesn't start with '{SCHEMA_COMMENT_PREFIX}'")

delete_identities = args.delete_identities and project_ours
if args.delete_identities:
    if delete_identities:
        print(f"    Identities       : {cfg.deployer_sp}, {cfg.app_sp}, {cfg.analysts_group}, "
              f"secret scope {cfg.secret_scope} -> delete")
    else:
        print("    Identities       : KEEP: only deleted together with this example's project")

if not args.yes:
    print("\nNothing deleted. Re-run with --yes to go ahead.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Delete, in an order that leaves nothing half-connected
# ---------------------------------------------------------------------------
if project_ours:
    step("Stopping the change data feed")
    stopped = False
    for database in w.postgres.list_databases(parent=cfg.branch()):
        try:
            configs = list(w.postgres.list_cdf_configs(parent=database.name))
        except NotFound:
            configs = []
        for config in configs:
            w.postgres.delete_cdf_config(name=config.name).wait()
            ok(f"deleted {config.name}")
            stopped = True
    if not stopped:
        skip("no change data feed found")

    step("Deleting the synced table")
    try:
        w.postgres.delete_synced_table(name=synced_table).wait()
        ok(f"deleted {synced_table}")
    except NotFound:
        skip("not found")

step("Dropping Unity Catalog schemas")
if not schemas_to_drop:
    skip("nothing to drop")
for schema in schemas_to_drop:
    uc.run(w, cfg.warehouse_id, f"DROP SCHEMA IF EXISTS {uc.ident(cfg.catalog, schema)} CASCADE")
    ok(f"dropped {cfg.catalog}.{schema}")

step("Deleting the Lakebase project")
if project_ours:
    w.postgres.update_branch(
        name=cfg.branch(),
        branch=Branch(spec=BranchSpec(is_protected=False)),
        update_mask=FieldMask(field_mask=["spec.is_protected"]),
    ).wait()
    ok("production branch unprotected")
    w.postgres.delete_project(name=cfg.project, purge=args.purge or None).wait()
    ok(f"deleted {cfg.project}" + ("" if args.purge else " (restore within 7 days with undelete-project)"))
elif project is not None:
    warn("kept: this example didn't create it (no managed_by tag)")
else:
    skip("project not found")

if delete_identities:
    step("Deleting demo identities")
    try:
        w.secrets.delete_scope(scope=cfg.secret_scope)
        ok(f"secret scope {cfg.secret_scope}")
    except NotFound:
        skip(f"secret scope {cfg.secret_scope} not found")
    for name in (cfg.deployer_sp, cfg.app_sp):
        for sp in w.service_principals.list(filter=f'displayName eq "{name}"'):
            w.service_principals.delete(id=sp.id)
            ok(f"service principal {name}")
    for group in w.groups.list(filter=f'displayName eq "{cfg.analysts_group}"'):
        w.groups.delete(id=group.id)
        ok(f"group {cfg.analysts_group}")

print("\nDone.")
