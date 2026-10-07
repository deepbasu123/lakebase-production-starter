"""Step 3: project permissions (layer 1 of 2: who can manage the infrastructure).

Lakebase has two separate permission layers, and they never sync with each other:

  Layer 1  Project permissions (this script). Databricks ACLs on the project.
           They control platform actions: create branches, resize compute,
           change settings, delete the project.
             CAN CREATE  every workspace user has it, it can't be removed
             CAN USE     view and use resources, create roles and databases
             CAN MANAGE  full control
  Layer 2  Postgres roles and GRANTs (step 4). They control who can log in
           to the database and what data they can read or change.

What this script grants
-----------------------
  * deployer service principal -> CAN MANAGE. CI/CD creates short-lived
    branches to test migrations, which needs CAN MANAGE.
  * app service principal -> nothing, and
  * analysts group -> nothing.
    Logging in to Postgres is decided entirely by layer 2. In our testing, an
    identity with no explicit project permission could still look up the host
    and request a database token for itself; Postgres rejected the token until
    a Postgres role existed for it.

Why not CAN USE for the analysts? CAN USE sounds read-only but isn't: it also
lets people create and delete Postgres roles and databases on a branch, and
view or reset password-role passwords. Give it only to people who administer
database access.

The project owner and workspace admins always have CAN MANAGE.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

from lakebase_starter.config import load_config
from lakebase_starter.identities import application_id
from lakebase_starter.ui import explain, heading, ok, step

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)

heading("Step 3: project permissions (who can manage the Lakebase project)")

deployer_app_id = application_id(w, cfg.deployer_sp)

step("Granting project permissions")
# update() is a PATCH: it adds or raises permissions and never removes anyone.
# To remove or downgrade someone, use w.permissions.set(), which replaces the
# whole explicit list.
w.permissions.update(
    request_object_type="database-projects",
    request_object_id=cfg.project_id,
    access_control_list=[
        AccessControlRequest(
            service_principal_name=deployer_app_id,
            permission_level=PermissionLevel.CAN_MANAGE,
        ),
    ],
)
ok(f"{cfg.deployer_sp} -> CAN_MANAGE")
explain(
    f"""{cfg.app_sp} and {cfg.analysts_group} get no project permission on
    purpose. They only need Postgres roles, which step 4 creates. CAN USE would
    also let them create and delete roles and reset passwords."""
)

step("Current project permissions")
acl = w.permissions.get(request_object_type="database-projects", request_object_id=cfg.project_id)
for entry in acl.access_control_list or []:
    who = entry.user_name or entry.group_name or entry.service_principal_name
    levels = ", ".join(
        f"{p.permission_level.value}{' (inherited)' if p.inherited else ''}" for p in entry.all_permissions or []
    )
    print(f"    {who:<45} {levels}")

print("\nNext: python scripts/04_setup_database.py")
