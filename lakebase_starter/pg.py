"""Connect to Lakebase Postgres.

There are two ways to log in to Lakebase:

1. OAuth (recommended). A Databricks identity (user, service principal or
   group member) asks the Lakebase API for a short-lived database token and
   uses it as the Postgres password. Tokens expire after one hour, so nothing
   long-lived is ever stored.

2. Native Postgres password. A classic username and password for tools that
   cannot fetch OAuth tokens. Disabled by default on new projects.

Both connect to the same host on port 5432 and both require TLS (sslmode=require).
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from databricks.sdk import WorkspaceClient

from .config import Config

APP_NAME = "lakebase-production-starter"


def endpoint_host(w: WorkspaceClient, endpoint_name: str) -> str:
    """Hostname of a Lakebase compute endpoint (what you'd put in PGHOST)."""
    return w.postgres.get_endpoint(name=endpoint_name).status.hosts.host


def connect(
    w: WorkspaceClient,
    cfg: Config,
    *,
    role: str | None = None,
    branch_id: str = "production",
    endpoint_id: str = "primary",
) -> psycopg.Connection:
    """Open an OAuth connection as the identity behind `w`.

    `role` is the Postgres role to log in as. It defaults to the identity's own
    role: the email address for a user, the application ID for a service
    principal. Pass a group name to log in through group-based auth (the caller
    must be a member of that Databricks group).
    """
    endpoint_name = cfg.endpoint(branch_id, endpoint_id)
    host = endpoint_host(w, endpoint_name)
    # The workspace credential (CLI login, or a service principal's OAuth secret)
    # is exchanged for a one-hour database token.
    token = w.postgres.generate_database_credential(endpoint=endpoint_name).token
    return psycopg.connect(
        host=host,
        port=5432,
        dbname=cfg.database,
        user=role or w.current_user.me().user_name,
        password=token,
        sslmode="require",
        application_name=APP_NAME,
        autocommit=True,
    )


def connect_with_password(
    w: WorkspaceClient,
    cfg: Config,
    *,
    role: str,
    password: str,
    branch_id: str = "production",
    endpoint_id: str = "primary",
) -> psycopg.Connection:
    """Open a native Postgres password connection (no Databricks identity involved)."""
    host = endpoint_host(w, cfg.endpoint(branch_id, endpoint_id))
    return psycopg.connect(
        host=host,
        port=5432,
        dbname=cfg.database,
        user=role,
        password=password,
        sslmode="require",
        application_name=APP_NAME,
        autocommit=True,
    )


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    """Run every statement in a .sql file inside one transaction.

    If any statement fails, nothing in the file is applied.
    """
    sql_text = path.read_text()
    with conn.transaction():
        # With no query parameters psycopg uses Postgres' simple query protocol,
        # which accepts many statements (including DO blocks) in one call.
        conn.execute(sql_text)


def connection_pool(
    w: WorkspaceClient,
    cfg: Config,
    *,
    role: str | None = None,
    branch_id: str = "production",
    endpoint_id: str = "primary",
    max_size: int = 5,
):
    """A connection pool for long-running applications (the production pattern).

    OAuth tokens last one hour, but Lakebase only checks the token when a
    connection is opened. So the pool fetches a fresh token each time it
    opens a new connection, and recycles connections every hour. Already-open
    connections are not cut off when the token they used expires.
    """
    from psycopg_pool import ConnectionPool

    endpoint_name = cfg.endpoint(branch_id, endpoint_id)
    host = endpoint_host(w, endpoint_name)
    user = role or w.current_user.me().user_name

    class TokenConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            kwargs["password"] = w.postgres.generate_database_credential(endpoint=endpoint_name).token
            return super().connect(conninfo, **kwargs)

    return ConnectionPool(
        conninfo=psycopg.conninfo.make_conninfo(
            host=host, port=5432, dbname=cfg.database, user=user, sslmode="require", application_name=APP_NAME
        ),
        connection_class=TokenConnection,
        min_size=1,
        max_size=max_size,
        max_lifetime=3600,  # recycle hourly
        # Test each connection before handing it out, so a compute restart
        # (maintenance, resize, failover) costs a reconnect instead of an error.
        check=ConnectionPool.check_connection,
        open=True,
    )
