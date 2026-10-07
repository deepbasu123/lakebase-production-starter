"""Run Unity Catalog SQL on a Databricks SQL warehouse.

Uses the Statement Execution API, so you don't need a cluster or a notebook.
"""

from __future__ import annotations

import time
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

_DONE = {StatementState.SUCCEEDED, StatementState.FAILED, StatementState.CANCELED, StatementState.CLOSED}


def run(w: WorkspaceClient, warehouse_id: str, statement: str) -> list[list[str | None]]:
    """Run one SQL statement and return its rows (every value comes back as a string)."""
    resp = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=statement, wait_timeout="50s"
    )
    # A stopped serverless warehouse needs a few seconds to start, so poll until done.
    while resp.status.state not in _DONE:
        time.sleep(3)
        resp = w.statement_execution.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        error = resp.status.error.message if resp.status.error else resp.status.state
        raise RuntimeError(f"SQL failed: {error}\n--- statement ---\n{statement}")
    return (resp.result.data_array if resp.result else None) or []


def ident(*parts: str) -> str:
    """Quote a Unity Catalog name with backticks, e.g. ident("main", "store_gold") -> `main`.`store_gold`.

    Quoting keeps names with hyphens or other special characters working.
    """
    return ".".join("`" + part.replace("`", "``") + "`" for part in parts)


def render(sql_text: str, values: dict[str, str]) -> str:
    """Fill in {{placeholders}} such as {{catalog}} in a .sql file.

    The scripts pass names already quoted with ident(), so the .sql files can
    use plain {{catalog}}.{{gold_schema}}.products.
    """
    for key, value in values.items():
        sql_text = sql_text.replace("{{" + key + "}}", value)
    return sql_text


def run_file(w: WorkspaceClient, warehouse_id: str, path: Path, values: dict[str, str]) -> None:
    """Run each statement of a .sql file in order.

    Statements are separated by a semicolon at the end of a line. Lines that
    start with `--` are comments.
    """
    sql_text = render(path.read_text(), values)
    statements, current = [], []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statements.append("\n".join(current).strip().rstrip(";"))
            current = []
    if "".join(current).strip():
        statements.append("\n".join(current).strip())
    for statement in statements:
        if statement:
            run(w, warehouse_id, statement)
