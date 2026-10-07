"""Look up Databricks identities and sign in as a service principal."""

from __future__ import annotations

from databricks.sdk import WorkspaceClient

from .config import Config
from .secret_scope import get_secret


def application_id(w: WorkspaceClient, display_name: str) -> str:
    """A service principal's application ID (a UUID).

    This UUID is also the name of the service principal's Postgres role.
    """
    found = list(w.service_principals.list(filter=f'displayName eq "{display_name}"'))
    if not found:
        raise SystemExit(
            f"Service principal '{display_name}' not found. Create it first "
            "(scripts/02_create_demo_identities.py) or fix the name in your config."
        )
    return found[0].application_id


def sign_in_as_service_principal(w: WorkspaceClient, cfg: Config, secret_prefix: str) -> WorkspaceClient:
    """A WorkspaceClient that acts as a service principal (machine-to-machine OAuth).

    The client ID and secret are read from the Databricks secret scope, under
    the keys <prefix>-client-id and <prefix>-client-secret (prefix "deployer" or
    "app"). scripts/02_create_demo_identities.py fills them in. In CI/CD you would
    read them from your pipeline's secret store instead, typically as the
    DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET environment variables.
    """
    return WorkspaceClient(
        host=w.config.host,
        client_id=get_secret(w, cfg.secret_scope, f"{secret_prefix}-client-id"),
        client_secret=get_secret(w, cfg.secret_scope, f"{secret_prefix}-client-secret"),
        auth_type="oauth-m2m",
    )
