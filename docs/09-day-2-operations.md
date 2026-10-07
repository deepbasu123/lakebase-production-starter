# 09. Day-2 operations

**Scripts:** `scripts/10_test_migration_on_branch.py`, `scripts/12_recover_deleted_data.py`, `scripts/99_teardown.py`

Day 1 is getting it running. Day 2 is everything after: changing the schema without breaking anything, getting data back when someone deletes it, scaling, monitoring, rotating secrets and, eventually, cleaning up. We ran each code snippet on this page (or the script it comes from) against the example project, unless the text says otherwise.

## Change the schema safely: rehearse on a branch

```bash
python scripts/10_test_migration_on_branch.py
```

```text
==> Creating branch 'migration-test' from production (expires in 4 hours)
    [ok] created: a full copy of production's data, roles and grants, made in seconds
==> Making the branch's compute small and able to scale to zero
    [ok] the branch came with a compute: 1.0-1.0 CU, scale-to-zero after 86400s
    [ok] now 0.5-1.0 CU, scale-to-zero after 300s
==> Running the migration on the branch, as the deployer service principal
    [ok] branch has delivery_note: True
    [ok] production is unchanged by the branch (delivery_note present: False)
==> Testing the change on the branch, as the app service principal
    [ok] the app wrote order 8 with a delivery note on the branch
==> Checking the branch's test data stayed out of Unity Catalog
    [ok] no trace of the test order in Unity Catalog
==> Applying the same migration to production
    [ok] production has delivery_note: True
==> Deleting branch 'migration-test'
    [ok] deleted
```

A branch is a full copy of production (data, schemas, roles and grants) that appears in seconds and only stores what you change. That makes it the right place to find out how a migration behaves on real data, with real permissions, before production sees it:

```python
w.postgres.create_branch(
    parent="projects/acme-store",
    branch_id="migration-test",
    branch=Branch(spec=BranchSpec(
        source_branch="projects/acme-store/branches/production",
        ttl=Duration(seconds=4 * 3600),     # deletes itself after 4 hours (max 30 days)
    )),
).wait()
```

Things we learned running it:

* **A new branch gets a compute automatically, with the project's default settings**, not production's. In our test that was 1 CU, scaling to zero after 24 hours idle. The script shrinks it to 0.5 to 1 CU with a 5-minute scale-to-zero so a test branch costs very little.
* **Roles and grants come along**, so the deployer and app service principals log in to the branch exactly as they do to production. After branching, the two copies' roles are independent. One exception: because production is a *protected* branch, password roles such as `bi_reader` get new generated passwords on the copy, so production's password doesn't work there.
* **Lakebase Change Data Feed ignores the branch.** Test data written there never reached Unity Catalog.
* **Adding a column re-snapshots the table in Lakebase Change Data Feed.** See [07](07-sync-lakebase-to-unity-catalog.md#schema-changes-rewrite-the-history-table) before you change a streamed table.
* Branches are limited (the docs list 500 per project), so give test branches an expiry. The UI also has a **Schema diff** view that compares a branch's schema with its parent.

The migration file lives in `sql/proposed/` because it's not in production when you first clone this repo. After step 10 applies it, move it into `sql/migrations/` so new environments get it too (step 10 looks in both folders, so re-running it still works). On a re-run the migration is a no-op, since production already has the column, and the script says so.

## Get deleted data back: point-in-time recovery

```bash
python scripts/12_recover_deleted_data.py
```

```text
==> Picking an order and noting the time
    [ok] order 8 with 1 item(s); the time is 05:01:27 UTC
==> Oops: the app deletes the order
    [ok] order 8 rows left in production: 0
==> Creating branch 'recovery-20261007-050116' as production looked at 05:01:27 UTC
    [ok] created
==> Reading the lost rows from the recovery branch
    [ok] found order 8 with 1 item(s)
==> Putting them back in production
    [ok] order 8 is back in production with 1 item(s)
```

Lakebase keeps the history of every change for the project's restore window (7 days here, configurable from 2 to 30). You can create a branch that shows the database at any moment in that window:

```python
when = Timestamp()
when.FromDatetime(datetime(2026, 10, 7, 5, 1, 27, tzinfo=timezone.utc))
w.postgres.create_branch(
    parent="projects/acme-store",
    branch_id="recovery-0501",
    branch=Branch(spec=BranchSpec(
        source_branch="projects/acme-store/branches/production",
        source_branch_time=when,
        ttl=Duration(seconds=2 * 3600),
    )),
).wait()
```

Then read the lost rows from the branch and insert them back into production. The order's ID is an identity column, so keeping the original ID needs `INSERT ... OVERRIDING SYSTEM VALUE`. Production stays online throughout, and only the rows you copy back change.

Lakebase Change Data Feed records the whole story. For order 8 the history table showed `insert`, `update_preimage`, `update_postimage` (it was paid), `delete` (the accident), then `insert` (the restore).

For bigger accidents, the Lakebase UI has **Restore from history**, which builds a whole new branch as of a moment you choose. You then point your app at it. A project can have at most 3 root branches, so tidy up old ones.

### Snapshots (Beta)

A snapshot is a named, point-in-time copy of a branch that you can restore later by creating a branch from it. Taking one before a risky release is cheap insurance:

```python
snap = w.postgres.create_snapshot(
    parent="projects/acme-store",
    snapshot_id="before-release",
    snapshot=Snapshot(spec=SnapshotSpec(
        source_branch="projects/acme-store/branches/production",
        ttl=Duration(seconds=24 * 3600),
    )),
).wait()

# To restore: create a branch from it (not run in our test)
# w.postgres.create_branch(parent="projects/acme-store", branch_id="restored",
#     branch=Branch(spec=BranchSpec(source_snapshot=snap.name, no_expiry=True))).wait()
```

We tested creating, listing and deleting a snapshot. Manual snapshots can only be taken of root branches (such as `production`), and a project can hold 10 of them. You can also schedule snapshots per root branch (`update-snapshot-schedule`, Beta); scheduled ones don't count towards the 10. Snapshot storage is billed.

## Scale

Change the autoscaling range at any time. Scaling inside the range needs no restart; changing the range itself may briefly interrupt connections.

```python
w.postgres.update_endpoint(
    name="projects/acme-store/branches/production/endpoints/primary",
    endpoint=Endpoint(spec=EndpointSpec(
        endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
        autoscaling_limit_min_cu=2, autoscaling_limit_max_cu=8)),
    update_mask=FieldMask(field_mask=["spec.autoscaling_limit_min_cu", "spec.autoscaling_limit_max_cu"]),
).wait()
```

This is the same call step 1 makes when you re-run it; we ran it with 1 to 4 CU, not the 2 to 8 shown here. Or edit `min_cu` and `max_cu` in your config and re-run `scripts/01_create_project.py`. The range can be at most 16 CU wide.

## High availability

HA adds standby computes in other availability zones. If the primary fails, a standby takes over automatically, committed transactions are kept, and the connection string stays the same (open connections drop, so apps must reconnect). We turned it on and off again through the API:

```python
# On: 2 computes in total (1 primary + 1 standby), standby also serves reads
w.postgres.update_endpoint(
    name="projects/acme-store/branches/production/endpoints/primary",
    endpoint=Endpoint(spec=EndpointSpec(
        endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
        no_suspension=True,                                   # HA requires scale-to-zero off
        group=EndpointGroupSpec(min=2, max=2, enable_readable_secondaries=True))),
    update_mask=FieldMask(field_mask=["spec.group", "spec.suspension"]),
).wait()

# Off again: back to a single compute
w.postgres.update_endpoint(
    name="projects/acme-store/branches/production/endpoints/primary",
    endpoint=Endpoint(spec=EndpointSpec(
        endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
        group=EndpointGroupSpec(min=1, max=1, enable_readable_secondaries=False))),
    update_mask=FieldMask(field_mask=["spec.group"]),
).wait()
```

With readable secondaries on, the endpoint also gets a read-only hostname (the endpoint ID with `-ro` appended, in `status.hosts.read_only_host`). HA uses 2 to 4 computes, and you pay for each one. The docs recommend two or more readable secondaries if apps depend on the read-only host, since with one, reads on that host stop during a failover.

## Read replicas

A read replica is a separate read-only compute on the same storage, sized on its own and able to scale to zero. Use it to keep reporting or heavy reads away from the app's compute:

```python
w.postgres.create_endpoint(
    parent="projects/acme-store/branches/production",
    endpoint_id="reporting",
    endpoint=Endpoint(spec=EndpointSpec(
        endpoint_type=EndpointType.ENDPOINT_TYPE_READ_ONLY,
        autoscaling_limit_min_cu=0.5, autoscaling_limit_max_cu=1,
        suspend_timeout_duration=Duration(seconds=300))),
).wait()
```

In our test the replica had its own hostname, `SELECT pg_is_in_recovery()` returned true, and an `INSERT` failed with `cannot execute INSERT in a read-only transaction`. Replicas replicate asynchronously, so they can be a moment behind.

**Watch out:** you can't delete a compute on a protected branch. Deleting our replica failed with `endpoint cannot be deleted for a protected branch` until we unprotected production, deleted the replica and protected production again. Plan replica changes as a short, deliberate maintenance step.

## Monitor

* **Monitoring page** in the Lakebase UI: CPU, memory, connections, cache hit rate, working set size, active queries, the slowest and most frequent queries, and a log of platform operations. Metrics go back up to 7 days. Keep the cache hit rate at 99% or better for OLTP workloads; if it drops, raise the minimum CU.
* **`pg_stat_statements`** for query statistics in SQL (tested):

  ```sql
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
  SELECT calls, round(mean_exec_time::numeric, 2) AS avg_ms, left(query, 80) AS query
  FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
  ```

  The statistics live in memory and reset when the compute restarts.
* **The syncs:** `w.postgres.get_synced_table(...)` shows a synced table's state and last sync time, and `w.postgres.list_cdf_statuses(...)` shows each streamed table's state and last sync time.
* **Beta options:** Lakebase telemetry in system tables (`system.lakebase`) and Insights, both switched on from the Previews page, and OpenTelemetry export of metrics and logs to tools such as Grafana Cloud, New Relic and Datadog.

## Maintenance

Lakebase restarts computes to apply updates (Postgres minor versions, security patches). A restart typically takes a few seconds and happens at least every 28 days. Pick the day and hour in the project's **Settings > Updates** so it lands in your quiet time. Apps with retry logic, like the connection pool in [06](06-connect-an-application.md), ride through it.

## Rotate secrets

| Secret | How it rotates |
| --- | --- |
| Database OAuth tokens | Automatically: each lasts an hour and the pool fetches new ones |
| Service principal OAuth secrets (90 days here) | Create a new secret, update your secret store, then delete the old secret |
| The `bi_reader` password | `ALTER ROLE bi_reader WITH PASSWORD '...'`, update the secret scope, update the tool |

```python
new = w.service_principal_secrets_proxy.create(service_principal_id=sp.id, lifetime="7776000s")
# ...store new.secret, deploy, then remove the old one:
for s in w.service_principal_secrets_proxy.list(service_principal_id=sp.id):
    if s.id != new.id:
        w.service_principal_secrets_proxy.delete(service_principal_id=sp.id, secret_id=s.id)
```

The rotation snippet above wasn't part of our test run; the create call is the same one step 2 uses.

## Keep costs in check

* Production runs all the time by design. Everything else should scale to zero: give dev and test branches small computes with short suspend timeouts, and an expiry.
* Pick the cheapest synced table mode that's fresh enough. TRIGGERED on a schedule is usually right; CONTINUOUS costs the most.
* Tag projects (`app`, `env`, `team`). Tags appear in `system.billing.usage`.
* Snapshots and long restore windows use storage, which is billed.

## Clean up

```bash
python scripts/99_teardown.py                                     # shows the plan, deletes nothing
python scripts/99_teardown.py --yes                               # soft delete
python scripts/99_teardown.py --yes --purge --delete-identities   # remove everything right now
```

What it does, in order: stops Lakebase Change Data Feed, deletes the synced table, drops the three Unity Catalog schemas, unprotects the production branch (Lakebase won't delete a project that has a protected branch), and deletes the project.

It has safety checks, so a typo in your config can't delete someone else's work: the project must carry the `managed_by=lakebase-production-starter` tag that step 1 sets, a schema is only dropped if its comment starts with "Acme store:", and identities are only deleted together with the example's project. The plan it prints shows each decision. `--force` skips the checks; you'd need it, for example, to delete the identities after the project is already gone.

* **Soft delete** (the default): the project can be restored for 7 days with `databricks postgres undelete-project projects/acme-store`, and its ID stays reserved, so you can't create a new `acme-store` project until then.
* **`--purge`** deletes it permanently and frees the name at once.
* **`--delete-identities`** also removes the secret scope, both service principals and the analysts group. Only use it in a test workspace where step 2 created them.

We ran the full teardown with `--purge --delete-identities` and then rebuilt everything from step 0, to make sure both directions work from a clean slate.

## Official docs

* [Manage branches](https://docs.databricks.com/aws/en/oltp/projects/manage-branches) and [Branch-based development](https://docs.databricks.com/aws/en/oltp/projects/dev-workflow-tutorial)
* [Point-in-time restore](https://docs.databricks.com/aws/en/oltp/projects/point-in-time-restore) and [Snapshots](https://docs.databricks.com/aws/en/oltp/projects/snapshots)
* [High availability](https://docs.databricks.com/aws/en/oltp/projects/high-availability) and [Read replicas](https://docs.databricks.com/aws/en/oltp/projects/read-replicas)
* [Monitor](https://docs.databricks.com/aws/en/oltp/projects/monitor), [Metrics](https://docs.databricks.com/aws/en/oltp/projects/metrics), [pg_stat_statements](https://docs.databricks.com/aws/en/oltp/projects/pg-stat-statements)
* [Updates](https://docs.databricks.com/aws/en/oltp/projects/updates)

Next: [10. Troubleshooting](10-troubleshooting.md)
