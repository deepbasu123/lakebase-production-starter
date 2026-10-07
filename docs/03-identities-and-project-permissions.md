# 03. Identities and project permissions

**Scripts:** `scripts/02_create_demo_identities.py`, `scripts/03_grant_project_permissions.py`

This page covers **who** is involved and **layer 1** of the permissions: what each identity can do to the Lakebase *infrastructure*. Database access (layer 2) comes in [04](04-database-roles-schemas-and-grants.md).

## The cast

| Identity | Kind | Its job | Project permission | Postgres role it will get |
| --- | --- | --- | --- | --- |
| You | User | Set everything up, break-glass admin | CAN MANAGE (as project owner) | your email, a member of `databricks_superuser` |
| `acme-store-deployer` | Service principal | CI/CD: runs schema migrations, creates test branches | CAN MANAGE | its application ID, a member of `store_owner` |
| `acme-store-app` | Service principal | The application at runtime | none | its application ID, a member of `store_writer` |
| `acme-store-analysts` | Group | People who need to read the data | none | the group name, a member of `store_reader` |
| `bi_reader` | Postgres password role | A BI tool that can't use OAuth | n/a (not a Databricks identity) | `bi_reader`, a member of `store_reader` |

**Why service principals for CI/CD and the app?** A service principal is an identity for software. Nothing breaks when a person changes teams or leaves, its access is limited to what the software needs, and its secret can be rotated on a schedule. Running production jobs under a person's account is how outages start when that person goes on leave.

## Step 2: create the demo identities (test workspaces only)

```bash
python scripts/02_create_demo_identities.py
```

In a real environment these identities usually exist already (created by your platform team, or synced from your identity provider). In that case, see [Bring your own identities](#bring-your-own-identities) below instead of running this step.

The script:

1. Creates a **secret scope** called `acme-store`. Secret scopes are Databricks' built-in vault. The scripts read the service principal secrets and the `bi_reader` password from here, so no secret is ever written to a file on your laptop.
2. Creates the two **service principals** and an **OAuth secret** for each, valid for 90 days, and stores the client ID and secret in the scope.
3. Creates the **group** `acme-store-analysts` and adds you to it, so you can test logging in as the group later.

**Run this step from a terminal, not a notebook.** When we ran it inside a Databricks notebook, creating the first service principal worked, but creating its OAuth secret came back `PermissionDenied`; the same call worked from a laptop after `databricks auth login`. Another way to get the secrets is the UI: click your username, **Settings > Identity and access**, next to **Service principals** click **Manage**, pick the service principal, open the **Secrets** tab and click **Generate secret**. Set a lifetime, choose the scopes (selecting all APIs is simplest for a test; the docs recommend restricting scopes), click **Generate**, and copy the secret and client ID, which are shown only once. Then store them in the secret scope as shown in [Bring your own identities](#bring-your-own-identities). That uses the Databricks CLI, so it still needs a terminal on some machine.

### How a service principal signs in

A service principal never has a password. It exchanges its client ID and secret for a short-lived workspace token (this is called machine-to-machine, or M2M, OAuth), then uses that token to ask Lakebase for a one-hour database token:

```python
w = WorkspaceClient(
    host="https://<your-workspace-url>",
    client_id="<application ID>",
    client_secret="<OAuth secret>",
    auth_type="oauth-m2m",
)
token = w.postgres.generate_database_credential(
    endpoint="projects/acme-store/branches/production/endpoints/primary"
).token
```

`lakebase_starter/identities.py` does exactly this. In a CI/CD pipeline you would set the environment variables `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` from your pipeline's secret store, and `WorkspaceClient()` picks them up with no arguments.

### If your app is a Databricks App

Databricks creates a service principal for each Databricks App automatically. When you add a Lakebase database to the app as a resource (the `postgres` resource, with `CAN_CONNECT_AND_CREATE`), Databricks also creates a Postgres role for it and puts the connection details (`PGHOST`, `PGDATABASE`, `PGUSER` and so on) in the app's environment. Everything in this example still applies: make that role a member of `store_writer`. See [06. Connect an application](06-connect-an-application.md).

### Bring your own identities

If your service principals and group already exist, don't run step 2. Instead:

1. Put their names in the `[identities]` section of `config.local.toml`.
2. Create the secret scope and store each service principal's client ID (its application ID) and OAuth secret under the key names the scripts expect. Without `--string-value`, the CLI prompts for the value, so the secret doesn't end up in your shell history:

```bash
databricks secrets create-scope acme-store --profile my-workspace
databricks secrets put-secret acme-store deployer-client-id     --profile my-workspace
databricks secrets put-secret acme-store deployer-client-secret --profile my-workspace
databricks secrets put-secret acme-store app-client-id          --profile my-workspace
databricks secrets put-secret acme-store app-client-secret      --profile my-workspace
```

Step 4 adds one more key, `bi_reader-password`, when it creates the password role.

3. Add yourself to the analysts group, so step 9 can test group login with your own token.
4. Run the steps one at a time, skipping step 2. (`scripts/run_all.sh` includes step 2.) If you'd rather keep these secrets in your own vault, change `lakebase_starter/secret_scope.py` and `lakebase_starter/identities.py`; nothing else reads them.

### Account groups and workspace groups

Databricks has account-level groups (usually synced from your identity provider) and older workspace-local groups. In our test workspace, step 2's group came out **workspace-local**. Lakebase accepted it for group login, but Unity Catalog didn't recognise it, so the Unity Catalog grant in step 8 had to be skipped. **Unity Catalog grants need account-level groups**, so use those in production. (We couldn't create an account-level group in the test workspace, so we didn't test one with Lakebase group login. The Lakebase docs support any group assigned to the workspace that owns the project.)

## Step 3: project permissions

```bash
python scripts/03_grant_project_permissions.py
```

```text
==> Current project permissions
    users                                         CAN_CREATE (inherited)
    you@example.com                               CAN_MANAGE
    <deployer application ID>                     CAN_MANAGE
    admins                                        CAN_MANAGE (inherited)
```

### The three levels

| Level | Who has it | Highlights of what it allows |
| --- | --- | --- |
| CAN CREATE | Every workspace user, automatically. You can't grant or remove it | See and list projects, branches and computes; create new projects |
| CAN USE | Granted | Everything above, plus: view the connection URI, **create and delete Postgres roles and databases on a branch, view and reset password-role passwords**, create snapshots |
| CAN MANAGE | The project owner, workspace admins, and anyone you grant it to | Everything: create and delete branches, start, stop, resize and delete computes, restore, change project settings, grant permissions, delete the project |

The full list of actions per level is in the [Lakebase project ACLs](https://docs.databricks.com/aws/en/security/auth/access-control/#lakebase-project-acls).

### What we grant, and why

* **Deployer: CAN MANAGE.** CI/CD creates short-lived branches to test migrations (step 10), and creating a branch needs CAN MANAGE.
* **App: nothing.** This surprises people, so here's what we tested. With no explicit permission (only the CAN CREATE everyone inherits), the app's service principal could read the compute's hostname and request a database token for itself. When it tried to log in, Postgres replied `password authentication failed for user '<application ID>'`, because no Postgres role existed for it yet. **Project permissions don't decide who can log in and read or change data. Postgres roles do.** An application needs a Postgres role, not a project permission. (CAN USE and CAN MANAGE do let people manage those roles, which is why they're worth guarding; see below.)
* **Analysts: nothing, for the same reason.** They connect from a SQL client with the host name and their own token, both of which CAN CREATE allows (`databricks postgres get-endpoint ...` and `databricks postgres generate-database-credential ...`).

**Be careful with CAN USE.** It sounds read-only, but it also lets people create and delete Postgres roles and databases on a branch and view or reset password-role passwords. Someone with CAN USE could reset `bi_reader`'s password or delete the app's role. It does unlock conveniences such as the connection details in the Lakebase UI's **Connect** dialog, so give it to the people who administer database access, not to everyone who reads data.

### Granting and revoking

```python
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

w.permissions.update(                       # PATCH: adds or raises, never removes
    request_object_type="database-projects",
    request_object_id="acme-store",
    access_control_list=[
        AccessControlRequest(service_principal_name="<application ID>",
                             permission_level=PermissionLevel.CAN_MANAGE),
        # For example, a small group of people who administer database access:
        AccessControlRequest(group_name="acme-store-db-admins",
                             permission_level=PermissionLevel.CAN_USE),
    ],
)
```

`update()` can't downgrade or remove anyone. To do that, use `w.permissions.set()`, which replaces the whole explicit list, so include everyone who should keep access. Inherited permissions (the owner, workspace admins, CAN CREATE) aren't affected either way.

The CLI equivalent:

```bash
databricks permissions get database-projects acme-store --profile my-workspace
databricks permissions update database-projects acme-store --profile my-workspace --json '{
  "access_control_list": [
    {"group_name": "acme-store-db-admins", "permission_level": "CAN_USE"}
  ]
}'
```

## Official docs

* [Manage project permissions](https://docs.databricks.com/aws/en/oltp/projects/manage-project-permissions)
* [Grant permissions programmatically](https://docs.databricks.com/aws/en/oltp/projects/grant-permissions-programmatically)
* [Authentication](https://docs.databricks.com/aws/en/oltp/projects/authentication)
* [Tutorial: grant project and database access to a new user](https://docs.databricks.com/aws/en/oltp/projects/grant-user-access-tutorial)

Next: [04. Database roles, schemas and grants](04-database-roles-schemas-and-grants.md)
