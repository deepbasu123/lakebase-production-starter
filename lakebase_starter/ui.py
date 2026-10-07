"""Plain console output so each script explains what it is doing as it runs."""

from __future__ import annotations

import textwrap


def heading(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def step(message: str) -> None:
    print(f"\n==> {message}")


def ok(message: str) -> None:
    print(f"    [ok] {message}")


def skip(message: str) -> None:
    print(f"    [skip] {message}")


def warn(message: str) -> None:
    print(f"    [warn] {message}")


def explain(message: str) -> None:
    """A short 'why' note for readers who are new to Lakebase."""
    for line in textwrap.wrap(" ".join(message.split()), width=68):
        print(f"    | {line}")
