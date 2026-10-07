"""Load the example's settings from a TOML file.

The scripts read `config.local.toml` if it exists, otherwise `config.toml`.
So once you've made config.local.toml, edit that file, not config.toml.
Copy `config.toml` to `config.local.toml` and edit the copy: git ignores it,
so your workspace details never end up in version control.

You can also point at any file with the LAKEBASE_STARTER_CONFIG environment variable.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    # [databricks]
    profile: str
    warehouse_id: str
    # [lakebase]
    project_id: str
    display_name: str
    pg_version: int
    database: str
    min_cu: float
    max_cu: float
    history_retention_days: int
    enable_password_login: bool
    # [unity_catalog]
    catalog: str
    gold_schema: str
    serving_schema: str
    history_schema: str
    # [identities]
    deployer_sp: str
    app_sp: str
    analysts_group: str
    password_role: str
    secret_scope: str

    # Lakebase resources have hierarchical names, like file paths:
    #   projects/<project>/branches/<branch>/endpoints/<endpoint>
    @property
    def project(self) -> str:
        return f"projects/{self.project_id}"

    def branch(self, branch_id: str = "production") -> str:
        return f"{self.project}/branches/{branch_id}"

    def endpoint(self, branch_id: str = "production", endpoint_id: str = "primary") -> str:
        return f"{self.branch(branch_id)}/endpoints/{endpoint_id}"


def config_path() -> Path:
    if os.environ.get("LAKEBASE_STARTER_CONFIG"):
        return Path(os.environ["LAKEBASE_STARTER_CONFIG"]).expanduser().resolve()
    local = REPO_ROOT / "config.local.toml"
    return local if local.exists() else REPO_ROOT / "config.toml"


def load_config() -> Config:
    path = config_path()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    db, lb, uc, ids = raw["databricks"], raw["lakebase"], raw["unity_catalog"], raw["identities"]
    return Config(
        profile=db["profile"],
        warehouse_id=db["warehouse_id"],
        project_id=lb["project_id"],
        display_name=lb["display_name"],
        pg_version=int(lb.get("pg_version", 17)),
        # Fixed: sql/01_roles_and_schema.sql grants CONNECT on databricks_postgres.
        database="databricks_postgres",
        min_cu=float(lb["min_cu"]),
        max_cu=float(lb["max_cu"]),
        history_retention_days=int(lb.get("history_retention_days", 7)),
        enable_password_login=bool(lb.get("enable_password_login", False)),
        catalog=uc["catalog"],
        gold_schema=uc["gold_schema"],
        serving_schema=uc["serving_schema"],
        history_schema=uc["history_schema"],
        deployer_sp=ids["deployer_sp"],
        app_sp=ids["app_sp"],
        analysts_group=ids["analysts_group"],
        password_role=ids["password_role"],
        secret_scope=ids["secret_scope"],
    )
