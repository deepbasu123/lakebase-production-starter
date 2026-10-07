"""Step 4: database roles and permissions (layer 2 of 2: who can touch the data).

What this does, in order
------------------------
1. Connects to Postgres as you, the project owner.
2. Runs sql/01_roles_and_schema.sql: three group roles (store_owner,
   store_writer, store_reader), the `store` schema and default privileges.
3. Creates a login role for each Databricks identity (OAuth roles):
     deployer service principal  -> role named after its application ID
     app service principal       -> role named after its application ID
     analysts group              -> role named after the group
4. Creates a native password role (bi_reader) for a tool that can't do OAuth,
   and stores its password in the Databricks secret scope.
5. Makes each login role a member of exactly one group role:

     login role                        member of      can do
     --------------------------------  -------------  ------------------------
     deployer service principal        store_owner    DDL (migrations)
     app service principal             store_writer   read + insert/update/delete
     analysts group                    store_reader   read only
     bi_reader (password)              store_reader   read only
     you (project owner)               databricks_superuser, store_owner

Re-run it as often as you like: existing roles and grants are left alone.
"""

from __future__ import annotations

import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.postgres import Role, RoleIdentityType, RoleRoleSpec
from psycopg import sql

from lakebase_starter import pg
from lakebase_starter.config import REPO_ROOT, load_config
from lakebase_starter.identities import application_id
from lakebase_starter.secret_scope import ensure_scope, has_secret, put_secret
from lakebase_starter.ui import explain, heading, ok, skip, step

cfg = load_config()
w = WorkspaceClient(profile=cfg.profile)
deployer_role = application_id(w, cfg.deployer_sp)
app_role = application_id(w, cfg.app_sp)

heading("Step 4: database roles, schema and grants")

step("Connecting to Postgres as you (the project owner)")
conn = pg.connect(w, cfg)
ok(f"connected as {conn.execute('SELECT current_user').fetchone()[0]}")

step("Group roles, the store schema and default privileges (sql/01_roles_and_schema.sql)")
pg.run_sql_file(conn, REPO_ROOT / "sql" / "01_roles_and_schema.sql")
ok("store_owner, store_writer, store_reader and schema `store` are in place")

step("OAuth login roles for Databricks identities")
explain(
    """An OAuth role links a Databricks identity to a Postgres role. The identity
    logs in with a one-hour token instead of a password. Roles are created per
    branch: a new branch starts with a copy of its parent's roles."""
)
existing = {r.status.postgres_role for r in w.postgres.list_roles(parent=cfg.branch())}
for postgres_role, identity_type, role_id, label in [
    (deployer_role, RoleIdentityType.SERVICE_PRINCIPAL, "deployer-sp", cfg.deployer_sp),
    (app_role, RoleIdentityType.SERVICE_PRINCIPAL, "app-sp", cfg.app_sp),
    (cfg.analysts_group, RoleIdentityType.GROUP, "analysts-group", cfg.analysts_group),
]:
    if postgres_role in existing:
        skip(f"{label}: role already exists")
        continue
    w.postgres.create_role(
        parent=cfg.branch(),
        role_id=role_id,
        role=Role(spec=RoleRoleSpec(identity_type=identity_type, postgres_role=postgres_role)),
    ).wait()
    ok(f"{label}: created role \"{postgres_role}\" ({identity_type.value})")


def new_password(length: int = 32) -> str:
    """A random password with lower case, upper case, digits and symbols.

    Lakebase rejects weak passwords (it checks for at least 60 bits of entropy).
    The symbols are URL-safe, so the password also works inside a connection URL.
    """
    symbols = "-_.~"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in symbols for c in candidate)
        ):
            return candidate


step(f"Native password role '{cfg.password_role}'")
if not cfg.enable_password_login:
    skip("enable_password_login is false in your config, so no password role is created")
else:
    explain(
        """Password roles are for tools that can't fetch OAuth tokens. They have
        no link to a Databricks identity, so treat the password like any other
        production secret: keep it in a secret store and rotate it."""
    )
    role_exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (cfg.password_role,)).fetchone()
    password_key = f"{cfg.password_role}-password"
    if role_exists and has_secret(w, cfg.secret_scope, password_key):
        skip("role exists and its password is in the secret scope")
    else:
        password = new_password()
        verb = "ALTER" if role_exists else "CREATE"
        conn.execute(
            sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(cfg.password_role), sql.Literal(password)
            )
        )
        ensure_scope(w, cfg.secret_scope)
        put_secret(w, cfg.secret_scope, password_key, password)
        done = "set a new password for the existing role" if role_exists else "created the role"
        ok(f"{done} and stored the password as {cfg.secret_scope}/{password_key}")
    # Guard rails for a BI tool: a few connections at most, no query longer than a minute.
    conn.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT 5").format(sql.Identifier(cfg.password_role)))
    conn.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = '60s'").format(sql.Identifier(cfg.password_role))
    )
    ok("limited to 5 connections and 60-second queries")

step("Group-role membership (this is where access is actually granted)")
memberships = [("store_owner", deployer_role), ("store_writer", app_role), ("store_reader", cfg.analysts_group)]
if cfg.enable_password_login:
    memberships.append(("store_reader", cfg.password_role))
for group_role, member in memberships:
    conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group_role), sql.Identifier(member)))
    ok(f"{group_role:<13} -> {member}")

step("Who is a member of what")
rows = conn.execute(
    """
    -- Postgres 16+ also records an admin-only membership for whoever created a
    -- role. Those don't pass on any privileges, so only count memberships that
    -- let the member use the role (INHERIT) or switch to it (SET).
    SELECT m.rolname AS member, string_agg(DISTINCT g.rolname, ', ' ORDER BY g.rolname) AS member_of
    FROM pg_auth_members am
    JOIN pg_roles g ON g.oid = am.roleid
    JOIN pg_roles m ON m.oid = am.member
    WHERE g.rolname IN ('store_owner', 'store_writer', 'store_reader', 'databricks_superuser')
      AND (am.inherit_option OR am.set_option)
    GROUP BY m.rolname
    ORDER BY m.rolname
    """
).fetchall()
for member, member_of in rows:
    print(f"    {member:<45} {member_of}")
conn.close()

print("\nNext: python scripts/05_run_migrations.py")
