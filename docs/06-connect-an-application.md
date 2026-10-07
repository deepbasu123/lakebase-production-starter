# 06. Connect an application

**Script:** `scripts/07_app_places_orders.py`
**Helpers:** `lakebase_starter/pg.py` (`connect()` and `connection_pool()`)

Step 7 plays the part of the store application. It signs in as the app's service principal, reads prices from the synced catalog and writes orders. This page explains how to connect an application to Lakebase properly, because the OAuth tokens behave differently from a normal database password.

```bash
python scripts/07_app_places_orders.py --orders 5
```

```text
==> Signing in as the app service principal and opening a connection pool
    [ok] connected as <app application ID>
==> Placing 5 orders
    [ok] order 1: riley@example.com    3 item(s), total 1725.00
    ...
```

## What a connection needs

| Setting | Value |
| --- | --- |
| Host | The compute's hostname, from `get_endpoint(...).status.hosts.host` or the **Connect** dialog in the Lakebase UI |
| Port | 5432 |
| Database | `databricks_postgres` |
| User | The Postgres role: an email, a service principal's application ID, a group name, or a password role |
| Password | A database OAuth token (or the password, for a password role) |
| SSL | Required. Use `sslmode=require` |

Getting a token is one call. The identity behind the `WorkspaceClient` gets a token for itself:

```python
w = WorkspaceClient()        # a user login, or a service principal's client ID and secret
token = w.postgres.generate_database_credential(
    endpoint="projects/acme-store/branches/production/endpoints/primary"
).token
```

## How long things last

| Thing | Lifetime | What it means for your app |
| --- | --- | --- |
| Database OAuth token | 1 hour | Only checked when a connection is opened. A connection that's already open keeps working after its token expires |
| Idle connection | Closed after 24 hours with no activity | Expect it and reconnect |
| Any connection | May be closed after 3 days, busy or not | Recycle connections on a schedule |
| Compute restarts | Usually a few seconds, during your update window, a resize or an HA failover | Retry on connection errors |

So an application needs two habits: **get a new token whenever it opens a new connection**, and **expect connections to drop now and then**.

## Pattern 1: a script or notebook

Open one connection, do the work, close it. `lakebase_starter.pg.connect()` does this:

```python
conn = psycopg.connect(
    host=host, port=5432, dbname="databricks_postgres",
    user=w.current_user.me().user_name,           # your email, or the SP's application ID
    password=w.postgres.generate_database_credential(endpoint=endpoint).token,
    sslmode="require",
)
```

## Pattern 2: a long-running service (use this in production)

Use a connection pool that fetches a fresh token every time it opens a physical connection. This is the pattern from the Lakebase docs, and it's what `lakebase_starter.pg.connection_pool()` builds:

```python
from psycopg_pool import ConnectionPool

class TokenConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        kwargs["password"] = w.postgres.generate_database_credential(endpoint=endpoint).token
        return super().connect(conninfo, **kwargs)

pool = ConnectionPool(
    conninfo=f"host={host} port=5432 dbname=databricks_postgres user={user} sslmode=require",
    connection_class=TokenConnection,
    min_size=1,
    max_size=5,
    max_lifetime=3600,                        # recycle connections every hour
    check=ConnectionPool.check_connection,    # test a connection before handing it out
)

with pool.connection() as conn, conn.transaction():
    conn.execute("INSERT INTO store.orders (customer_email, total_amount) VALUES (%s, %s)", ...)
```

Why each setting:

* **`connection_class`** puts a fresh token on every new connection, so tokens never go stale inside the pool.
* **`max_lifetime=3600`** closes connections after an hour, well inside the 24-hour idle and 3-day limits.
* **`check`** pings a connection before your code gets it. After a compute restart you get a reconnect instead of an error.
* **`max_size`** should stay modest. The number of connections a compute accepts depends on its size (a few hundred at 1 CU), and each synced table also uses up to 16 of them.

If you use SQLAlchemy, the Lakebase authentication docs show the same idea with a `do_connect` event that refreshes the token shortly before it expires.

## The built-in connection pooler (PgBouncer)

Lakebase includes PgBouncer, a server-side connection pooler. Each compute has a second hostname for it: the endpoint ID with `-pooler` appended (it's also in `get_endpoint(...).status.hosts.read_write_pooled_host`). Two things to know before reaching for it:

* **It only works with native password roles, not OAuth roles.** An app that logs in with OAuth tokens, like the one here, should pool on the client side as shown above.
* It runs in *transaction mode*, so session features don't work through it: session-level `SET`, SQL `PREPARE`, advisory locks, `LISTEN`/`NOTIFY`, `WITH HOLD` cursors. Run migrations and `pg_dump` over a direct connection.

## If your app is a Databricks App

Databricks Apps make most of this automatic:

1. Add the Lakebase database to the app as a **resource**. Choose the project, branch and database. The only permission level is *Can connect and create*. You need CAN MANAGE on the project to add it.
2. Databricks creates a Postgres role for the app's service principal and puts the connection details in environment variables: `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` (the service principal's client ID, which is also its role name), `PGSSLMODE` and `PGAPPNAME`.
3. Your code still fetches the token itself with `generate_database_credential()`. The `WorkspaceClient()` inside an app is already signed in as the app's service principal. You need the endpoint name for that call; passing it in as an app setting is the simplest way.

In a Declarative Automation Bundle the resource looks like this:

```yaml
resources:
  apps:
    acme_store_app:
      name: acme-store-app
      source_code_path: ./app
      resources:
        - name: lakebase-db
          postgres:
            branch: projects/acme-store/branches/production
            database: projects/acme-store/branches/production/databases/databricks-postgres
            permission: CAN_CONNECT_AND_CREATE
```

Then, to apply this example's permission model, make the app's role a member of `store_writer`:

```sql
GRANT store_writer TO "<the app's service principal client ID>";
```

*Can connect and create* also lets the app create its own schema. That's handy for prototypes. For production, this example keeps schema changes in CI/CD (the deployer runs migrations as `store_owner`), so the app only ever changes rows.

## Apps outside Databricks

An app running on your own servers works exactly like step 7: give it a service principal, store the client ID and secret in your secret manager, set `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET`, and use the pool above. If a firewall sits between the app and Databricks, allow-list the Lakebase IP addresses.

## Official docs

* [Authentication](https://docs.databricks.com/aws/en/oltp/projects/authentication), including the pool and SQLAlchemy examples
* [Connection pooling](https://docs.databricks.com/aws/en/oltp/projects/connection-pooling)
* [Connect overview](https://docs.databricks.com/aws/en/oltp/projects/connect-overview)
* [Add a Lakebase resource to a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/lakebase)

Next: [07. Sync Lakebase to Unity Catalog](07-sync-lakebase-to-unity-catalog.md)
