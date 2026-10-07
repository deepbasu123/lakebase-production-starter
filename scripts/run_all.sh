#!/usr/bin/env bash
# Run every step in order and stop at the first failure.
# Usage: ./scripts/run_all.sh   (from the repo root, with the virtual environment active)
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"
for script in \
  00_check_prerequisites.py \
  01_create_project.py \
  02_create_demo_identities.py \
  03_grant_project_permissions.py \
  04_setup_database.py \
  05_run_migrations.py \
  06_sync_uc_to_lakebase.py \
  07_app_places_orders.py \
  08_sync_lakebase_to_uc.py \
  09_verify_roles.py \
  10_test_migration_on_branch.py \
  11_verify_end_to_end.py \
  12_recover_deleted_data.py
do
  "$PYTHON" "scripts/$script"
done
