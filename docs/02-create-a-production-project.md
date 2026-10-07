# 02. Create a production project

**Script:** `scripts/01_create_project.py`

## What this step does

It creates the Lakebase project `acme-store`. A new project always comes with:

* a branch called `production`, which holds your live data, and
* a compute called `primary` on that branch, which is the Postgres server your app connects to.

The script applies production settings at creation time, so production is never briefly unprotected. Run it again whenever you change your config (`config.local.toml`): it updates an existing project to match the file instead of creating a second one.

```bash
python scripts/01_create_project.py
```

```text
==> What you have now
    project            : projects/acme-store (Postgres 17)
    history retention  : 7 days
    password logins    : enabled
    branch             : projects/acme-store/branches/production (protected=True)
    compute            : projects/acme-store/branches/production/endpoints/primary
    host               : ep-xxxx-xxxx.database.<region>.cloud.databricks.com
    autoscaling        : 1.0-4.0 CU
    scale-to-zero      : off
```

In our test the project was ready about 15 seconds after the script started.

## The production settings, and why

| Setting | Value here | Why | Change it later? |
| --- | --- | --- | --- |
| Postgres version | 17 | 16, 17 and 18 are available; 17 is the default for new projects | Choose it when you create the project |
| Production branch protected | yes | A protected branch can't be deleted or reset, its computes can't be deleted, and the project itself can't be deleted while it has one. It is also never archived for inactivity | Yes. The teardown script unprotects it before deleting the project |
| Autoscaling | 1 to 4 CU | Lakebase adds and removes memory and CPU within this range as load changes, with no restart. The gap between min and max can be at most 16 CU | Yes, at any time (`min_cu`, `max_cu`) |
| Scale-to-zero | off | New computes shut down after a period of inactivity by default and wake on the next connection. That saves money on dev branches. In production you usually want every request served by a running database | Yes |
| Restore window (history retention) | 7 days | How far back you can restore or branch from. Allowed range is 2 to 30 days. Longer means more storage | Yes (`history_retention_days`) |
| Password logins | on | Off by default for new projects. Turned on here only for the `bi_reader` example. Keep it off if you don't need it | Yes (`enable_password_login`) |
| Tags | `app`, `env`, `managed_by` | Tags appear in `system.billing.usage`, so you can see what the project costs | Yes. Note that sending a new list replaces all tags |

How big should the minimum be? The docs' advice is to make it "large enough to cache your working set in memory", that is, the data your app reads often. Start small, watch the cache hit rate in the project's **Monitoring** page, and raise the minimum if it drops. For OLTP workloads, aim for a cache hit rate of 99% or better.

## How the code does it

The interesting part of `scripts/01_create_project.py`:

```python
w.postgres.create_project(
    project_id="acme-store",
    project=Project(
        spec=ProjectSpec(
            display_name="Acme Store (production)",
            pg_version=17,
            history_retention_duration=Duration(seconds=7 * 24 * 3600),
            enable_pg_native_login=True,
            custom_tags=[ProjectCustomTag(key="env", value="prod"), ...],
        ),
        # Settings for the production branch and its compute, applied at creation.
        initial_branch_spec=InitialBranchSpec(is_protected=True),
        initial_endpoint_spec=InitialEndpointSpec(
            autoscaling_limit_min_cu=1,
            autoscaling_limit_max_cu=4,
            no_suspension=True,          # scale-to-zero off
        ),
    ),
).wait()                                 # create, update and delete calls are long-running
```

Changing settings later uses *update masks*: you send the new values plus a list of the fields you mean to change. Anything not in the mask is left alone.

```python
w.postgres.update_endpoint(
    name="projects/acme-store/branches/production/endpoints/primary",
    endpoint=Endpoint(spec=EndpointSpec(
        endpoint_type=EndpointType.ENDPOINT_TYPE_READ_WRITE,
        autoscaling_limit_min_cu=2,
        autoscaling_limit_max_cu=8,
        no_suspension=True,
    )),
    update_mask=FieldMask(field_mask=[
        "spec.autoscaling_limit_min_cu",
        "spec.autoscaling_limit_max_cu",
        "spec.suspension",               # the mask name for no_suspension
    ]),
).wait()
```

Two things that caught us out:

* The update mask for `no_suspension` is `spec.suspension`, not `spec.no_suspension`.
* `EndpointSpec` requires `endpoint_type`, even when you're only changing the size.

## Look at it yourself

```bash
databricks postgres get-project projects/acme-store --profile my-workspace
databricks postgres list-branches projects/acme-store --profile my-workspace
databricks postgres get-endpoint projects/acme-store/branches/production/endpoints/primary --profile my-workspace
```

Or open the project in the Lakebase UI. The **Branches**, **Computes** and **Roles & Databases** tabs show the same things.

## Production features this example doesn't switch on

These are worth knowing about. [09. Day-2 operations](09-day-2-operations.md) covers how to use them.

* **High availability.** Adds one to three standby computes in other availability zones, with automatic failover. It needs scale-to-zero off, and each standby is a compute you pay for.
* **Read replicas.** Extra read-only computes for reporting or read-heavy traffic, so they don't compete with the app's writes.
* **Maintenance window.** Lakebase restarts computes to apply updates (Postgres minor versions, security patches), typically for a few seconds. You can choose the day and hour in the project's settings.
* **Budget policy.** `budget_policy_id` links the project to a serverless usage policy for cost attribution.

## Official docs

* [Manage projects](https://docs.databricks.com/aws/en/oltp/projects/manage-projects)
* [Manage computes](https://docs.databricks.com/aws/en/oltp/projects/manage-computes) and [Autoscaling](https://docs.databricks.com/aws/en/oltp/projects/autoscaling)
* [Scale to zero](https://docs.databricks.com/aws/en/oltp/projects/scale-to-zero)
* [Protected branches](https://docs.databricks.com/aws/en/oltp/projects/protected-branches)
* [Updates and maintenance](https://docs.databricks.com/aws/en/oltp/projects/updates)

Next: [03. Identities and project permissions](03-identities-and-project-permissions.md)
