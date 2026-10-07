"""Create the WorkspaceClient the scripts use, wherever they run."""

from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient

from .config import Config


def workspace_client(cfg: Config) -> WorkspaceClient:
    """Sign in as you.

    On your laptop (or in CI) this uses the CLI profile from your config.
    Inside a Databricks notebook or job there are no CLI profiles; leave
    `profile` empty and the SDK signs in as whoever is running the notebook.
    """
    if not cfg.profile or "DATABRICKS_RUNTIME_VERSION" in os.environ:
        return WorkspaceClient()
    return WorkspaceClient(profile=cfg.profile)
