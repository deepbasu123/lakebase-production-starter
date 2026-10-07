# 04. Database roles, schemas and grants

**Scripts:** `scripts/04_setup_database.py`, `scripts/05_run_migrations.py`
**SQL:** `sql/01_roles_and_schema.sql`, `sql/migrations/001_create_store_tables.sql`

This is **layer 2**: who can log in to Postgres, and what they can do with the data. It's the most important page in this repo. Get this right and onboarding, offboarding and audits become simple.

## The pattern: group roles and login roles

Postgres uses one concept, the *role*, for both users and groups. This example splits them into two kinds:

* **Group roles** describe a job. Nobody logs in as them (`NOLOGIN`). All the `GRANT`s go to these.
* **Login roles** are the identities that actually connect. They get no direct grants. They get access only by being a *member* of a group role.

```text
login role (who)                      member of        can do
------------------------------------  ---------------  ---------------------------------------
deployer service principal (OAuth)    store_owner      owns the tables: CREATE, ALTER, DROP
app service principal (OAuth)         store_writer     SELECT, INSERT, UPDATE, DELETE
acme-store-analysts group (OAuth)     store_reader     SELECT
bi_reader (password)                  store_reader     SELECT
you, the project owner (OAuth)        databricks_superuser, store_owner
```

`store_writer` is itself a member of `store_reader`, so read access is only ever granted once, to `store_reader`.

Why bother with the indirection?

* **Onboarding is one statement.** A new reporting tool needs read access? `GRANT store_reader TO new_tool;` and you're done.
* **Offboarding is one statement.** `REVOKE store_reader FROM old_tool;` and nothing is left behind.
* **Audits are readable.** "Who can change orders?" means "who is a member of `store_writer`?", not a trawl through table grants.

## Step 4: roles, the schema and grants

```bash
python scripts/04_setup_database.py
```

### 4a. Group roles and the `store` schema

The script connects as you and runs `sql/01_roles_and_schema.sql`. Open the file: it's short and commented line by line. The key parts:

```sql
CREATE ROLE store_owner NOLOGIN;      -- (inside an IF NOT EXISTS check, so re-runs are safe)
CREATE ROLE store_writer NOLOGIN;
CREATE ROLE store_reader NOLOGIN;
GRANT store_reader TO store_writer;   -- writers can read too

GRANT store_owner TO CURRENT_USER;    -- see "a Postgres 16+ detail" below
CREATE SCHEMA IF NOT EXISTS store AUTHORIZATION store_owner;

GRANT USAGE ON SCHEMA store TO store_reader;

-- Default privileges: rules for tables that don't exist yet.
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT SELECT ON TABLES TO store_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT INSERT, UPDATE, DELETE ON TABLES TO store_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE store_owner IN SCHEMA store
  GRANT USAGE, SELECT ON SEQUENCES TO store_writer;
```

**The schema is owned by a role, not a person.** If the schema belonged to whoever ran the script, you'd have a problem the day they leave. `store_owner` outlives everyone.

**Default privileges** are what make this low-maintenance. Every table a migration creates as `store_owner` automatically gets `SELECT` for readers and `INSERT, UPDATE, DELETE` for writers. Nobody has to remember a `GRANT` after each release. The rule only applies to objects created *by* `store_owner`, which is why migrations switch to that role first (step 5).

**A Postgres 16+ detail.** Lakebase runs Postgres 16 or newer, where the role that creates another role is automatically given an *admin-only* membership in it. Admin-only means you can grant the role to others, but you don't get its privileges and can't switch to it. To create a schema owned by `store_owner`, or set default privileges for it, you need a real membership, hence `GRANT store_owner TO CURRENT_USER`. If you list memberships in `pg_auth_members` you'll see both rows; only the ones with `inherit_option` or `set_option` set actually pass on privileges.

### 4b. OAuth login roles for Databricks identities

An OAuth role ties a Databricks identity to a Postgres role. The role name is fixed by the identity type:

| Identity type | Postgres role name | Example |
| --- | --- | --- |
| `USER` | the user's email | `"alex@example.com"` |
| `SERVICE_PRINCIPAL` | the service principal's application ID (a UUID) | `"8c01cfb1-62c9-4a09-88a8-e195f4b01b08"` |
| `GROUP` | the group's display name, exact case | `"acme-store-analysts"` |

The script creates them through the Lakebase API, which suits automation because you can list what exists first:

```python
w.postgres.create_role(
    parent="projects/acme-store/branches/production",
    role_id="app-sp",
    role=Role(spec=RoleRoleSpec(
        identity_type=RoleIdentityType.SERVICE_PRINCIPAL,
        postgres_role="<the app's application ID>",
    )),
).wait()
```

The same thing in SQL, for example in the Lakebase SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;     -- once per database
SELECT databricks_create_role('<application ID>', 'SERVICE_PRINCIPAL');
SELECT databricks_create_role('alex@example.com', 'USER');
SELECT databricks_create_role('acme-store-analysts', 'GROUP');
```

Either way, the new role can log in but has **no permissions at all** until you grant some.

**Group-based login.** When a role exists for a Databricks group, any member of that group (directly or through nested groups) logs in with *their own* token but *the group's role name* as the user name:

```bash
PGPASSWORD="<your own OAuth token>" psql \
  "host=<host> dbname=databricks_postgres user=acme-store-analysts sslmode=require"
```

Inside the session, `current_user` is `acme-store-analysts`. You manage who's in the group in Databricks (or your identity provider), and Postgres never needs to change. Group membership is checked when the connection opens: someone removed from the group can't open new connections, but a connection they already have stays open.

**Roles are per branch.** A new branch starts with a copy of its parent's roles and grants. After that they're independent: a role you add to production later doesn't appear in older branches. Project permissions from [03](03-identities-and-project-permissions.md), by contrast, cover every branch.

### 4c. A native password role for a BI tool

Some tools can't fetch a new OAuth token every hour. For those, Lakebase supports classic Postgres passwords, with these caveats:

* Password logins are **off by default** for new projects. Turn them on per project (`enable_pg_native_login`, which step 1 sets from `enable_password_login` in your config).
* A password role isn't linked to any Databricks identity. Nobody's departure revokes it, so you have to rotate it.
* Lakebase rejects weak passwords. The requirement is at least 12 characters mixing lower case, upper case, digits and symbols, with at least 60 bits of entropy. The script generates a 32-character one.

The script creates it with SQL, so it knows the password and can store it in the secret scope. The Lakebase API can create password roles too, but doesn't return the password; you'd then reset it in the UI to see it.

```sql
CREATE ROLE bi_reader WITH LOGIN PASSWORD '<generated>';
ALTER ROLE bi_reader CONNECTION LIMIT 5;                  -- a BI tool shouldn't need more
ALTER ROLE bi_reader SET statement_timeout = '60s';       -- no runaway reports
GRANT store_reader TO bi_reader;
```

The two `ALTER ROLE` lines are cheap guard rails worth copying for any tool account.

To rotate the password later: `ALTER ROLE bi_reader WITH PASSWORD '<new one>';`, update the secret, update the tool.

One more thing about password roles and branches: when you branch from a *protected* branch, Lakebase gives password roles on the new branch freshly generated passwords, so production's passwords never leak into dev and test copies. `bi_reader`'s stored password works on production only.

### 4d. Memberships

```sql
GRANT store_owner  TO "<deployer application ID>";
GRANT store_writer TO "<app application ID>";
GRANT store_reader TO "acme-store-analysts";
GRANT store_reader TO bi_reader;
```

The double quotes matter: application IDs and group names contain hyphens, and Postgres needs quotes around identifiers like that.

## Step 5: migrations, run by CI/CD

```bash
python scripts/05_run_migrations.py
```

```text
==> Signing in as the deployer service principal
    [ok] connected to Postgres as <deployer application ID>

==> Tables in schema `store`
    table         owner         replica id  reader SELECT  reader INSERT  writer INSERT
    order_items   store_owner   full        True           False          True
    orders        store_owner   full        True           False          True
```

This is what a release pipeline does: sign in as the deployer service principal and apply `sql/migrations/*.sql` in order. Each migration begins with:

```sql
SET ROLE store_owner;
```

so the tables belong to `store_owner`, and the default privileges from step 4 grant access to readers and writers automatically. The output above shows it worked: the deployer created the tables, yet `store_owner` owns them, readers can select but not insert, and writers can insert.

The migrations are written to be re-runnable (`CREATE TABLE IF NOT EXISTS`). For a real project, a migration tool (Alembic, Flyway, Liquibase, sqitch) is worth it, because it records which migrations ran.

`001_create_store_tables.sql` also sets `REPLICA IDENTITY FULL` on both tables. Lakebase Change Data Feed needs that to stream changes to Unity Catalog ([07](07-sync-lakebase-to-unity-catalog.md)). **Any table you add to `store` later needs it too**, so put it in every migration that creates a table.

## Everyday recipes

All of these run as the project owner, the identity that ran step 4. (Postgres 16+ only lets a role's creator, or someone the creator granted `ADMIN OPTION`, grant that role to others.)

**Give a new person read access.** Add them to the `acme-store-analysts` group in Databricks. That's it.

**Give a new application write access.**

```sql
SELECT databricks_create_role('<new app application ID>', 'SERVICE_PRINCIPAL');
GRANT store_writer TO "<new app application ID>";
```

**Remove an application.**

```sql
REVOKE store_writer FROM "<application ID>";
DROP ROLE "<application ID>";   -- fails if the role still owns objects; see below
```

**A role owns objects and won't drop.** Move the objects first. In SQL, `REASSIGN OWNED BY "<old>" TO store_owner;` needs you to be a member of both roles. Or let Lakebase do it as part of the delete: `databricks postgres delete-role <role resource name> --reassign-owned-to <other role resource name>`. Get resource names from `databricks postgres list-roles projects/acme-store/branches/production`. Roles created through the API keep the ID you gave them (`.../roles/app-sp`); roles created in SQL get a generated one (`.../roles/rol-xxxx-xxxxxxxxxx`). This is one more reason to have migrations create objects as `store_owner`: then login roles own nothing and drop cleanly.

**Who can do what?**

```sql
-- Members of each group role
SELECT g.rolname AS group_role, m.rolname AS member
FROM pg_auth_members am
JOIN pg_roles g ON g.oid = am.roleid
JOIN pg_roles m ON m.oid = am.member
WHERE g.rolname LIKE 'store_%' AND (am.inherit_option OR am.set_option)
ORDER BY 1, 2;

-- What a role can do to each table
SELECT tablename,
       has_table_privilege('store_writer', 'store.' || tablename, 'INSERT') AS can_insert,
       has_table_privilege('store_reader', 'store.' || tablename, 'INSERT') AS reader_can_insert
FROM pg_tables WHERE schemaname = 'store';
```

## What `databricks_superuser` is

Every project has a `databricks_superuser` role, and the project owner is a member. It has broad privileges (it inherits `pg_read_all_data`, `pg_write_all_data` and `pg_monitor`, and can create roles and databases), but it is **not** a real Postgres superuser. Treat membership like production admin access: give it to very few people, and never to an application.

## Official docs

* [Create Postgres roles](https://docs.databricks.com/aws/en/oltp/projects/postgres-roles)
* [Manage roles](https://docs.databricks.com/aws/en/oltp/projects/manage-roles)
* [Manage database permissions](https://docs.databricks.com/aws/en/oltp/projects/manage-roles-permissions)
* [Transfer object ownership](https://docs.databricks.com/aws/en/oltp/projects/transfer-object-ownership)
* PostgreSQL: [roles](https://www.postgresql.org/docs/current/user-manag.html), [privileges](https://www.postgresql.org/docs/current/ddl-priv.html), [ALTER DEFAULT PRIVILEGES](https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html)

Next: [05. Sync Unity Catalog to Lakebase](05-sync-unity-catalog-to-lakebase.md)
