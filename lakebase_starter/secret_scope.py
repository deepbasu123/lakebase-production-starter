"""Read and write values in a Databricks secret scope.

The scripts expect these keys in the scope named in your config:

  deployer-client-id, deployer-client-secret   the deployer service principal
  app-client-id,      app-client-secret        the app service principal
  bi_reader-password                           the native password role (step 4 writes it)

scripts/02_create_demo_identities.py fills in the first four. If you bring your
own service principals, put their values there yourself (docs/03 shows how).
"""

from __future__ import annotations

import base64

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceAlreadyExists


def ensure_scope(w: WorkspaceClient, scope: str) -> bool:
    """Create the secret scope if it doesn't exist. Returns True if it was created."""
    try:
        w.secrets.create_scope(scope=scope)
        return True
    except ResourceAlreadyExists:
        return False


def put_secret(w: WorkspaceClient, scope: str, key: str, value: str) -> None:
    w.secrets.put_secret(scope=scope, key=key, string_value=value)


def get_secret(w: WorkspaceClient, scope: str, key: str) -> str:
    try:
        response = w.secrets.get_secret(scope=scope, key=key)
    except NotFound:
        raise SystemExit(
            f"Secret '{scope}/{key}' not found. Run scripts/02_create_demo_identities.py, or store "
            "your own service principal's client ID and secret there (see docs/03)."
        ) from None
    # The Secrets API returns the value base64-encoded.
    return base64.b64decode(response.value).decode()


def has_secret(w: WorkspaceClient, scope: str, key: str) -> bool:
    try:
        return any(s.key == key for s in w.secrets.list_secrets(scope=scope))
    except NotFound:
        return False
