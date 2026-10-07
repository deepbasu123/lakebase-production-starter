"""Step 1: create the Lakebase project with production settings.

What this creates
-----------------
A Lakebase *project* is the top-level container for one Postgres database
service. Creating it also creates:

  * a `production` branch (the copy of your data that your app uses), and
  * a `primary` read-write compute endpoint on that branch (the Postgres server).

Production settings applied here (all come from your config: config.local.toml,
or config.toml if you haven't made a local copy):

  * Postgres 17 (the default for new projects).
  * The production branch is *protected*, so it cannot be deleted or reset
    by accident.
  * Autoscaling between min_cu and max_cu, with scale-to-zero turned OFF so
    the first request of the day never waits for the database to wake up.
  * A history retention window: how far back you can restore to.
  * Native Postgres password logins: off unless you need them.
  * Tags, so the cost shows up against the right team in billing reports.

Run it again at any time: if the project exists, the script updates these
settings to match your config instead of creating a new project.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.common.types.fieldmask import FieldMask
from databricks.sdk.errors import NotFound
from databricks.sdk.service.postgres import (
    Branch,
    BranchSpec,
    Endpoint,
    EndpointSpec,
    EndpointType,
    InitialBranchSpec,
    InitialEndpointSpec,
    Project,
    ProjectCustomTag,
    ProjectSpec,
)
from google.protobuf.duration_pb2 import Duration

from lakebase_starter.config import load_config
from lakebase_starter.ui import explain, heading, ok, step

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)

heading(f"Step 1: Lakebase project '{cfg.project_id}'")

tags = [
    ProjectCustomTag(key="app", value="acme-store"),
    ProjectCustomTag(key="env", value="prod"),
    ProjectCustomTag(key="managed_by", value="lakebase-production-starter"),
]
project_spec = ProjectSpec(
    display_name=cfg.display_name,
    pg_version=cfg.pg_version,
    history_retention_duration=Duration(seconds=cfg.history_retention_days * 24 * 3600),
    enable_pg_native_login=cfg.enable_password_login,
    custom_tags=tags,
)

try:
    project = w.postgres.get_project(name=cfg.project)
    exists = True
except NotFound:
    exists = False

if not exists:
    step("Creating the project (usually well under a minute)")
    explain(
        """Creating a project also creates the 'production' branch and its
        'primary' compute. We protect the branch and switch scale-to-zero off
        at creation time, so production is never unprotected, not even briefly."""
    )
    project = w.postgres.create_project(
        project_id=cfg.project_id,
        project=Project(
            spec=project_spec,
            initial_branch_spec=InitialBranchSpec(is_protected=True),
            initial_endpoint_spec=InitialEndpointSpec(
                autoscaling_limit_min_cu=cfg.min_cu,
                autoscaling_limit_max_cu=cfg.max_cu,
                no_suspension=True,
            ),
        ),
    ).wait()
    ok(f"created {project.name}")
else:
    step("Project already exists: making its settings match your config")
    w.postgres.update_project(
        name=cfg.project,
        project=Project(spec=project_spec),
        update_mask=FieldMask(
            field_mask=[
                "spec.display_name",
                "spec.history_retention_duration",
                "spec.enable_pg_native_login",
                "spec.custom_tags",
            ]
        ),
    ).wait()
    ok("project settings updated")

    step("Protecting the production branch")
    w.postgres.update_branch(
        name=cfg.branch("production"),
        branch=Branch(spec=BranchSpec(is_protected=True)),
        update_mask=FieldMask(field_mask=["spec.is_protected"]),
    ).wait()
    ok("production branch is protected")

    step(f"Sizing the primary compute: {cfg.min_cu}-{cfg.max_cu} CU, scale-to-zero off")
    w.postgres.update_endpoint(
        name=cfg.endpoint(),
        endpoint=Endpoint(
            spec=EndpointSpec(
                endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
                autoscaling_limit_min_cu=cfg.min_cu,
                autoscaling_limit_max_cu=cfg.max_cu,
                no_suspension=True,
            )
        ),
        update_mask=FieldMask(
            field_mask=[
                "spec.autoscaling_limit_min_cu",
                "spec.autoscaling_limit_max_cu",
                # The mask for no_suspension / suspend_timeout_duration is "spec.suspension".
                "spec.suspension",
            ]
        ),
    ).wait()
    ok("compute updated")

step("What you have now")
project = w.postgres.get_project(name=cfg.project)
branch = w.postgres.get_branch(name=cfg.branch("production"))
endpoint = w.postgres.get_endpoint(name=cfg.endpoint())
print(f"    project            : {project.name} (Postgres {project.status.pg_version})")
print(f"    history retention  : {project.status.history_retention_duration.seconds // 86400} days")
print(f"    password logins    : {'enabled' if project.status.enable_pg_native_login else 'disabled'}")
print(f"    branch             : {branch.name} (protected={branch.status.is_protected})")
print(f"    compute            : {endpoint.name}")
print(f"    host               : {endpoint.status.hosts.host}")
print(
    f"    autoscaling        : {endpoint.status.autoscaling_limit_min_cu}"
    f"-{endpoint.status.autoscaling_limit_max_cu} CU"
)
suspend = endpoint.status.suspend_timeout_duration
print(f"    scale-to-zero      : {'off' if not suspend or suspend.seconds <= 0 else f'after {suspend.seconds}s idle'}")
print("\nNext: python scripts/02_create_demo_identities.py")
