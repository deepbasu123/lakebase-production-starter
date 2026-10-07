# 01. Before you start

This page gets your laptop and workspace ready. It takes about 15 minutes the first time.

## What you need

**A Databricks workspace with Lakebase.** Lakebase is available on AWS and Azure. Use a test or development workspace for your first run: the example creates real resources and real identities.

**Permissions in that workspace:**

| You need | For | If you don't have it |
| --- | --- | --- |
| Workspace admin | Step 2 creates two service principals and a group | Ask an admin to create them, then follow [Bring your own identities](03-identities-and-project-permissions.md#bring-your-own-identities) instead of running step 2 |
| `CREATE SCHEMA` on a Unity Catalog catalog | Steps 6 and 8 create three schemas | Ask the catalog owner, or use a catalog you own |
| A SQL warehouse you can use | Running Unity Catalog SQL | Any serverless warehouse is fine |
| Lakebase Change Data Feed switched on | Step 8 | A workspace admin enables "Lakebase Change Data Feed" on the workspace **Previews** page |

Anyone in a workspace can create a Lakebase project: every workspace user gets the CAN CREATE project permission automatically.

**A catalog that doesn't use default storage.** Lakebase Change Data Feed can't write to catalogs that use default storage, or to managed storage you can only reach through a private endpoint. If you're not sure, open the catalog in Catalog Explorer and check its storage location, or ask your platform team. The step 0 check can't detect this for you, and step 8 is where it would fail.

**On your laptop:**

* Python 3.10 or newer (`python3 --version`)
* The Databricks CLI, a recent v1.x release (`databricks --version`). This example was tested with v1.18.0. Install or upgrade it with the [CLI install guide](https://docs.databricks.com/aws/en/dev-tools/cli/install)
* Optional: `psql`, if you'd like to poke around the database by hand

## Laptop or notebook?

You can run the steps from your laptop (the rest of this page) or from a Databricks notebook in your workspace. For the notebook, create a **Git folder** from `https://github.com/deepbasu123/lakebase-production-starter` (**Workspace > Create > Git folder**), open `notebooks/lakebase_production_walkthrough`, and follow its first cell. It installs the packages, signs in as you and runs the same scripts. You still need the warehouse ID (step 3 below) and a catalog name for its widgets, and the permission requirements above still apply. You also need a terminal once: run step 2 (`python scripts/02_create_demo_identities.py`) from it before you start, because in our test creating service principal OAuth secrets was refused with the notebook's credentials. For that one run, follow steps 1 to 4 below.

## 1. Get the code and install the Python packages

```bash
git clone https://github.com/deepbasu123/lakebase-production-starter.git
cd lakebase-production-starter

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

This installs two packages: `databricks-sdk` (the Databricks Python SDK) and `psycopg` (the Postgres driver).

## 2. Log in to your workspace

```bash
databricks auth login --host https://<your-workspace-url> --profile my-workspace
```

A browser window opens. Log in, and the CLI saves the login under the profile name `my-workspace` in `~/.databrickscfg`. The scripts use this same login through the Python SDK, so there are no tokens to copy around.

Check it worked:

```bash
databricks current-user me --profile my-workspace
```

## 3. Find your SQL warehouse ID

```bash
databricks warehouses list --profile my-workspace
```

Copy the ID (the first column) of the warehouse you want to use. In the UI it's under **SQL Warehouses**, then your warehouse, then **Connection details**.

## 4. Fill in your settings

```bash
cp config.toml config.local.toml
```

Edit `config.local.toml`. At a minimum, change these three:

```toml
[databricks]
profile = "my-workspace"                  # the profile you just logged in with
warehouse_id = "1234567890abcdef"         # from step 3

[unity_catalog]
catalog = "main"                          # a catalog you can create schemas in (not default storage)
```

Everything else has a sensible default. git ignores `config.local.toml`, so your workspace details stay on your machine. Each setting in the file has a comment explaining it.

If you already have service principals or a group you want to use, put their names in the `[identities]` section and follow [Bring your own identities](03-identities-and-project-permissions.md#bring-your-own-identities) instead of running step 2.

## 5. Check everything

```bash
python scripts/00_check_prerequisites.py
```

It checks your login, whether you're a workspace admin, the warehouse, the catalog and your permissions on it, and whether the project name is free (including names still held by a recently deleted project). Fix anything it reports before carrying on.

## A note on cost

These are real, billable resources:

* The production compute runs continuously while the project exists (scale-to-zero is deliberately off), autoscaling between 1 and 4 CU.
* Synced tables run a managed pipeline each time they sync.
* Lakebase Change Data Feed writes to Delta continuously.
* The test branch in step 10 gets a small compute that scales to zero, and the script deletes the branch at the end.

When you're finished, `python scripts/99_teardown.py --yes` removes it all (see [Clean up](09-day-2-operations.md#clean-up)).

Next: [02. Create a production project](02-create-a-production-project.md)
