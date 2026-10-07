"""Step 2 (test workspaces only): create the Databricks identities the example uses.

In a real deployment you probably already have these. Put their names in
your config, store their credentials in the secret scope (docs/03, "Bring your
own identities"), and skip this script.

What this creates
-----------------
  * Service principal `acme-store-deployer`: the identity your CI/CD pipeline
    uses to run schema migrations.
  * Service principal `acme-store-app`: the identity the application runs as.
    (If your app is a Databricks App, Databricks creates this service
    principal for you when you create the app.)
  * Group `acme-store-analysts`: people who may read the data. You are added
    as a member so you can test group-based login in step 9.
  * Secret scope `acme-store`: holds the service principals' OAuth secrets.
    Secrets live in Databricks, not in files on your laptop.

A service principal is a non-human identity. It signs in with an OAuth
client ID and secret (machine-to-machine OAuth), never with a password.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import iam

from lakebase_starter.config import load_config
from lakebase_starter.secret_scope import ensure_scope, has_secret, put_secret
from lakebase_starter.ui import explain, heading, ok, skip, step

SECRET_LIFETIME = "7776000s"  # 90 days. Rotate before it expires (see docs/09-day-2-operations.md).

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)
me = w.current_user.me()

heading("Step 2: demo identities (service principals, group, secret scope)")

step(f"Secret scope '{cfg.secret_scope}'")
if ensure_scope(w, cfg.secret_scope):
    ok("created")
else:
    skip("already exists")


def ensure_service_principal(display_name: str, secret_prefix: str) -> iam.ServicePrincipal:
    step(f"Service principal '{display_name}'")
    found = list(w.service_principals.list(filter=f'displayName eq "{display_name}"'))
    if found:
        sp = found[0]
        skip(f"already exists (application ID {sp.application_id})")
    else:
        sp = w.service_principals.create(display_name=display_name, active=True)
        ok(f"created (application ID {sp.application_id})")

    if has_secret(w, cfg.secret_scope, f"{secret_prefix}-client-secret"):
        skip("OAuth secret already stored in the secret scope")
    else:
        secret = w.service_principal_secrets_proxy.create(
            service_principal_id=sp.id, lifetime=SECRET_LIFETIME
        )
        put_secret(w, cfg.secret_scope, f"{secret_prefix}-client-id", sp.application_id)
        put_secret(w, cfg.secret_scope, f"{secret_prefix}-client-secret", secret.secret)
        ok(f"OAuth secret created (expires {secret.expire_time}) and stored as "
           f"{cfg.secret_scope}/{secret_prefix}-client-secret")
    return sp


ensure_service_principal(cfg.deployer_sp, "deployer")
ensure_service_principal(cfg.app_sp, "app")

step(f"Group '{cfg.analysts_group}'")
found = list(w.groups.list(filter=f'displayName eq "{cfg.analysts_group}"'))
if found:
    group = found[0]
    skip("already exists")
else:
    group = w.groups.create(display_name=cfg.analysts_group)
    ok("created")

members = {m.value for m in (w.groups.get(id=group.id).members or [])}
if me.id in members:
    skip(f"{me.user_name} is already a member")
else:
    w.groups.patch(
        id=group.id,
        operations=[iam.Patch(op=iam.PatchOp.ADD, path="members", value=[{"value": me.id}])],
        schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
    )
    ok(f"added {me.user_name} as a member")
explain(
    """Being in the group lets you test group-based login later: any member of
    a Databricks group can sign in to Postgres as the group's role using their
    own OAuth token."""
)

print("\nNext: python scripts/03_grant_project_permissions.py")
