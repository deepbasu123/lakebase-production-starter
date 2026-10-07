"""Step 0: check your setup before creating anything.

Checks that:
  * the Databricks CLI profile in your config works,
  * you are a workspace admin (needed only for step 2, which creates the
    demo service principals and group; skip step 2 if you bring your own),
  * the SQL warehouse exists,
  * the Unity Catalog catalog exists and you can create schemas in it,
  * the Lakebase project ID is free (or already belongs to this example).

Two things it can't check for you:
  * that Lakebase Change Data Feed is switched on (a workspace admin enables it
    on the Previews page), and
  * that the catalog doesn't use default storage, which Lakebase Change Data
    Feed can't write to. Check the catalog's storage location in Catalog Explorer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.version import __version__ as sdk_version

from lakebase_starter.config import config_path, load_config
from lakebase_starter.ui import heading, ok, step, warn

problems = []
cfg = load_config()
heading("Step 0: prerequisites")
print(f"    config file: {config_path()}")
print(f"    databricks-sdk {sdk_version}")

step(f"Signing in with profile '{cfg.profile}'")
w = WorkspaceClient(profile=cfg.profile)
me = w.current_user.me()
ok(f"{me.user_name} on {w.config.host}")
if any(g.display == "admins" for g in me.groups or []):
    ok("you are a workspace admin")
else:
    warn("you are not a workspace admin: step 2 can't create service principals or groups")

step(f"SQL warehouse {cfg.warehouse_id}")
try:
    warehouse = w.warehouses.get(id=cfg.warehouse_id)
    ok(f"{warehouse.name} ({warehouse.state.value if warehouse.state else 'unknown state'})")
except Exception as error:  # noqa: BLE001 - report anything the API says
    problems.append(f"warehouse {cfg.warehouse_id}: {error}")

step(f"Unity Catalog catalog {cfg.catalog}")
try:
    catalog = w.catalogs.get(name=cfg.catalog)
    ok(f"exists (storage root: {catalog.storage_root or 'none shown'})")
    privileges = set()
    try:
        # Effective privileges for you on this catalog.
        effective = w.grants.get_effective(securable_type="catalog", full_name=cfg.catalog, principal=me.user_name)
        for assignment in effective.privilege_assignments or []:
            privileges |= {p.privilege.value for p in assignment.privileges or [] if p.privilege}
    except PermissionDenied:
        warn("couldn't read your privileges on the catalog; steps 6 and 8 will tell you if CREATE SCHEMA is missing")
    if privileges & {"ALL_PRIVILEGES", "CREATE_SCHEMA", "MANAGE"} or catalog.owner == me.user_name:
        ok("you can create schemas in it")
    elif privileges:
        # Not a hard failure: privileges that come through a group may not show up here.
        warn(f"couldn't confirm CREATE SCHEMA on {cfg.catalog}; if steps 6 or 8 fail, ask the catalog owner for it")
except NotFound:
    problems.append(f"catalog {cfg.catalog} not found")

step(f"Lakebase project ID '{cfg.project_id}'")
try:
    project = w.postgres.get_project(name=cfg.project)
    tags = {t.key: t.value for t in project.status.custom_tags or []}
    if project.delete_time:
        problems.append(
            f"project {cfg.project_id} is deleted (kept for 7 days). Restore it with "
            f"`databricks postgres undelete-project {cfg.project}` or pick another project_id"
        )
    elif tags.get("managed_by") == "lakebase-production-starter":
        ok("exists already and was created by this example: the steps will reuse it")
    else:
        problems.append(f"project {cfg.project_id} already exists and wasn't created by this example")
except NotFound:
    # A deleted project keeps its ID for 7 days (soft delete), so check those too.
    deleted = [p for p in w.postgres.list_projects(show_deleted=True) if p.name == cfg.project]
    if deleted:
        problems.append(
            f"a deleted project called {cfg.project_id} still holds the name (it's kept for 7 days). "
            f"Restore it with `databricks postgres undelete-project {cfg.project}` or pick another project_id"
        )
    else:
        ok("free")

if problems:
    print("\nFix these before you continue:")
    for problem in problems:
        print(f"  - {problem}")
    sys.exit(1)
print("\nAll good. Next: python scripts/01_create_project.py")
