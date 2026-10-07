# 08. Prove the permissions

**Script:** `scripts/09_verify_roles.py`

Permissions you haven't tested are permissions you're guessing about. This step logs in as every identity, for real, and tries the same six operations. Each attempt runs inside a transaction that is always rolled back, so the script changes nothing. It's low risk to run against production (in CI, after every migration, for example): the only heavy operation is the deployer's `ALTER TABLE`, which needs a brief exclusive lock on `store.orders` and gives up after 1 second if the table is busy. On a very busy database, run it at a quiet time.

```bash
python scripts/09_verify_roles.py
```

This is the output from our test run:

```text
==> Logging in as each identity
    [ok] analysts          logged in as acme-store-analysts
    [ok] app (writer)      logged in as <app application ID>
    [ok] deployer (owner)  logged in as <deployer application ID>
    [ok] bi_reader         logged in as bi_reader

==> Trying each operation
    operation                     analysts      bi_reader     app (writer)  deployer (owner)
    read store.orders             allowed       allowed       allowed       allowed
    read store_serving.products   allowed       allowed       allowed       denied
    insert into store.orders      denied        denied        allowed       allowed
    update a synced product       denied        denied        denied        denied
    alter table store.orders      denied        denied        denied        allowed
    create a table in store       denied        denied        denied        allowed

==> The app service principal must not be able to log in as the analysts group
    [ok] rejected, as expected (OAuth: User is not authorized)

==> bi_reader guard rails
    [ok] statement_timeout = 1min, connection limit = 5

All permission checks passed.
```

## Reading the grid

* **Four different ways of logging in all work.** The analysts row is *group-based login*: you, logged in with your own OAuth token, using the group's role name. The app and deployer use service principal tokens. `bi_reader` uses a password.
* **Readers read.** Both the analysts group and the BI tool can query orders and products, and nothing else.
* **The app changes rows, never structure.** It can insert orders but can't alter or create tables. A bug, or an attacker who gets the app's credentials, can't drop your schema.
* **The deployer can't read products.** That's on purpose. Migrations only need to change the `store` schema, so `store_owner` was never given access to `store_serving`. Least privilege works in every direction.
* **Nobody can update a synced table.** Not even the app. The copy in Postgres is refreshed from Unity Catalog, and writing to it would just be overwritten on the next sync.
* **Group login is checked against real membership.** The app's service principal isn't in the analysts group, so Postgres refuses to let it log in as `acme-store-analysts`, even with a perfectly valid token.

## How the checks work

Each operation is wrapped like this:

```python
def attempt(conn, statement) -> bool:
    try:
        with conn.transaction(force_rollback=True):        # always rolled back
            conn.execute("SET LOCAL lock_timeout = '1s'")  # don't queue behind the live app
            conn.execute(statement)
        return True
    except errors.InsufficientPrivilege:                   # Postgres error 42501
        return False
```

Only a *permission* error counts as "denied". Anything else (a syntax error, a lock timeout) stops the script, so a broken test can't pass by accident.

One side effect to know about: an `INSERT` that's rolled back still uses up a value from the `order_id` sequence. Postgres sequences never roll back. Gaps in IDs are normal and harmless.

## Checking by hand

You can ask Postgres directly what a role can do without logging in as it:

```sql
SELECT has_table_privilege('store_reader', 'store.orders', 'INSERT');        -- false
SELECT has_table_privilege('store_writer', 'store.orders', 'INSERT');        -- true
SELECT has_schema_privilege('store_writer', 'store', 'CREATE');              -- false
SELECT has_database_privilege('store_reader', 'databricks_postgres', 'CONNECT');
```

These answer "would this be allowed?" from the catalog. The script goes one step further and actually tries, through the same login path your applications use.

## Extend it

When you add a role or a table, add a row to `OPERATIONS` and a column to `EXPECTED` in `scripts/09_verify_roles.py`. The script exits with an error if any result differs from what you expect, so it works as a CI check.

Next: [09. Day-2 operations](09-day-2-operations.md)
