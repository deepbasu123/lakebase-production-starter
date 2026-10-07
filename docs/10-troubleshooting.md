# 10. Troubleshooting

Every error on this page is one we either hit while building this example or that the docs call out. Search the page for the message you're seeing.

## Logging in

**`password authentication failed for user '<application ID>'`** (or an email, or a group name)
The token is fine, but no Postgres role exists for that identity on this branch. Create one (step 4, or `SELECT databricks_create_role(...)`). Remember roles are per branch: a role created on production after a branch was made doesn't exist on that branch. We saw this when the app's service principal tried to connect before step 4: it could get a token but not log in.

**`OAuth: User is not authorized` when logging in as a group**
The identity whose token you're using isn't a member of that Databricks group (directly or through a nested group). That's the exact error step 9 gets when the app's service principal tries to log in as the analysts group. Group membership is checked when the connection opens, and the group must be assigned to the same workspace as the project.

**`refresh token is invalid` / `cannot get access token` from the scripts**
Your Databricks CLI login expired. Run `databricks auth login --profile <your profile>` again. It happened to us halfway through a run.

**Connection errors right after a restart, failover or (on non-production branches) scale-to-zero**
Expected now and then. Retry the connection. The pool in `lakebase_starter/pg.py` checks connections before handing them out, which turns most of these into a quiet reconnect.

**Connections without TLS**
Lakebase requires TLS. Always connect with `sslmode=require`.

## Permissions

**`permission denied for table ...` or `permission denied for schema ...` (SQLSTATE 42501)**
The role is missing a grant, which may be correct. Check it with `has_table_privilege('<role>', 'store.orders', 'INSERT')` and the membership query in [04](04-database-roles-schemas-and-grants.md#everyday-recipes). Make sure new tables were created *as `store_owner`* (migrations start with `SET ROLE store_owner`); default privileges only apply to objects that role creates.

**`must be owner of table ...`**
Changing a table's structure needs ownership. That's the deployer's job, through `store_owner`. If an app or a person hits this, the permissions are doing their job.

**Readers can't see the synced tables**
Synced tables are owned by an internal role, so default privileges don't cover them. Re-run the grant from step 6 after adding synced tables: `GRANT SELECT ON ALL TABLES IN SCHEMA store_serving TO store_reader;`

**`must be able to SET ROLE "store_owner"`** (creating the schema) **or `permission denied to change default privileges`**
In Postgres 16 and later, creating a role gives you only an admin-only membership in it, which isn't enough for either. Grant yourself a real membership: `GRANT store_owner TO CURRENT_USER;` (the first SQL file does). We reproduced both errors with a throwaway role to get the exact messages.

**Unity Catalog: `PRINCIPAL_DOES_NOT_EXIST ... Could not find principal with name acme-store-analysts`**
The group is workspace-local. Unity Catalog only recognises account-level groups. Lakebase's group login still works with it. Use an account-level group, usually synced from your identity provider.

**Unity Catalog: `User does not have CREATE CATALOG on Metastore`**
Registering a Lakebase database as a Unity Catalog catalog needs `CREATE CATALOG` on the metastore. This example doesn't need a registered catalog, so you can skip it. See "Optional: register the database in Unity Catalog" below.

## Lakebase API calls

**`EndpointSpec.__init__() missing 1 required positional argument: 'endpoint_type'`**
The Python SDK requires `endpoint_type` on every `EndpointSpec`, even when you're only resizing. Pass `EndpointType.ENDPOINT_TYPE_READ_WRITE` (or `READ_ONLY` for a replica).

**Scale-to-zero setting doesn't change**
The update mask for `no_suspension` and `suspend_timeout_duration` is `spec.suspension`. Don't set both fields in one request, and don't send `no_suspension: false` (it's rejected); send a `suspend_timeout_duration` instead.

**`endpoint cannot be deleted for a protected branch`**
Computes on a protected branch can't be deleted, read replicas included. Unprotect the branch, delete the compute, protect it again.

**Can't delete the project**
A protected branch blocks project deletion. Unprotect the branch first (`scripts/99_teardown.py` does).

**Creating a project fails because the ID is taken, but you just deleted it**
Deleted projects are kept for 7 days and their IDs stay reserved. Restore it with `databricks postgres undelete-project`, wait, or delete with `--purge` next time.

**`409 Conflict`**
Another operation on the project is still running. Wait and retry with backoff. A 409 means the request wasn't accepted, so check the state with a `get` before retrying.

## Unity Catalog to Lakebase (synced tables)

**Triggered or Continuous mode needs a change data feed on the source**
For a Delta source, turn it on with `ALTER TABLE ... SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`. If it's missing, the synced table UI warns you and shows that command.

**The Postgres table isn't where you expected**
The synced table's Unity Catalog schema name becomes the Postgres schema name. `main.store_serving.products` lands as `store_serving.products`.

**Rows are missing**
Rows with a null primary key are skipped. Duplicate keys fail the sync unless you set a `timeseries_key`.

**The sync fails on text data**
Null bytes (`0x00`) in strings break the sync. Clean them in the source, for example `REPLACE(col, CAST(CHAR(0) AS STRING), '')`.

**Changes in the source aren't showing up**
In TRIGGERED mode nothing happens until a sync runs. Click **Sync now**, run a job with a table-update trigger, or start the synced table's pipeline (step 11 shows how).

## Lakebase to Unity Catalog (change data feed)

**`Invalid cdf_config_id '...'. Must match [a-z][a-z0-9_]{0,62}`**
Feed IDs take underscores, not hyphens, unlike project and branch IDs. Use `store_to_unity_catalog`, not `store-to-unity-catalog`.

**`NotFound: database not found` when creating a feed**
`parent` must be the database's resource path (`projects/<p>/branches/<b>/databases/databricks-postgres`), not its Postgres name (`databricks_postgres`). Look it up with `list_databases`.

**`No API found for 'GET .../databases/<db>/cdf-statuses'`**
Statuses are listed under the feed, not the database: `list_cdf_statuses(parent="projects/.../databases/databricks-postgres/cdf-configs/store_to_unity_catalog")`.

**A table never shows up in Unity Catalog**
Check three things: it has at least one row (empty tables aren't streamed), it has `REPLICA IDENTITY FULL`, and it isn't partitioned (not supported). `list_cdf_statuses` shows each table's state (snapshotting, streaming or skipped) and a `status_detail` field.

**The history table "lost" its old changes**
A column change (add, drop or type change) re-snapshots that table: the current version holds one `insert` per current row. Older versions are still there through `VERSION AS OF` until vacuumed. See [07](07-sync-lakebase-to-unity-catalog.md#schema-changes-rewrite-the-history-table).

**The feed stopped writing**
Check you haven't added a row filter or column mask to a history table, or switched on Delta's change data feed on it. Both are unsupported on these tables.

**The destination catalog uses default storage**
Lakebase Change Data Feed doesn't support destination catalogs that use default storage, or managed storage reachable only through a private endpoint. Pick a catalog with its own, publicly reachable storage location.

## Optional: register the database in Unity Catalog

Lakebase can also register a Postgres database as a read-only Unity Catalog catalog, so you can query Postgres tables live from a serverless SQL warehouse and see them in Catalog Explorer:

```python
w.postgres.create_catalog(
    catalog_id="acme_store_pg",
    catalog=Catalog(spec=CatalogCatalogSpec(
        postgres_database="databricks_postgres",
        branch="projects/acme-store/branches/production",
    )),
).wait()
```

It needs `CREATE CATALOG` on the metastore, which our test workspace didn't grant, so **this example doesn't use it and we haven't run this code**. It isn't required for either sync direction. Queries through the registered catalog are governed by Unity Catalog grants; direct Postgres connections still use Postgres roles.

Back to the [README](../README.md)
