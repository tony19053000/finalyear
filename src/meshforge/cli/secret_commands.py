"""``compute secret set | list | delete`` - your job secrets (V0.7 G6, D144).

* ``set NAME`` - the value comes from **stdin** (piped: everything read, one
  trailing newline removed - ``echo $X | compute secret set NAME`` stores
  ``$X``; a terminal: typed without echo) or ``--from-file PATH`` (exact bytes).
  Never from argv: a command line is visible to every process on the laptop
  and lands in shell history. 4 bytes .. 64 KiB.
* ``list`` - names and timestamps (the controller never returns a value).
* ``delete NAME``.

A job uses them with manifest ``secrets: [NAME]`` (``docs/OPERATIONS.md``).
No value is ever printed, logged or sent anywhere but the request body.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Annotated

import typer

from meshforge.cli import app as cli
from meshforge.cli import render
from meshforge.cli.exits import CliExit
from meshforge.common.private_files import PrivateFileError, read_bounded
from meshforge.common.secret_names import (
    MAX_SECRET_BYTES,
    SecretNameError,
    SecretValueError,
    check_secret_value,
    normalise_secret_name,
)
from meshforge.common.secret_redaction import ACTIVE

__all__ = ["secret_app"]

_HOLDER = "cli-secret-set"
"""This process's entry in the active-secrets registry while a value is sent."""

secret_app = typer.Typer(
    name="secret",
    help="Store, list or delete your job secrets (V0.7; values never shown).",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def _name(raw: str) -> str:
    try:
        return normalise_secret_name(raw)
    except SecretNameError as exc:
        cli._die(f"NAME: {exc.message}", CliExit.USAGE_ERROR)


def _strip_newline(data: bytes) -> bytes:
    if data.endswith(b"\r\n"):
        return data[:-2]
    if data.endswith(b"\n"):
        return data[:-1]
    return data


def read_value(from_file: Path | None) -> bytes:
    """The value from ``--from-file`` or stdin (module docstring)."""
    if from_file is not None:
        try:
            data, _info = read_bounded(
                from_file,
                limit=MAX_SECRET_BYTES,
                what="secret file",
                follow_symlinks=True,
            )
        except PrivateFileError as exc:
            cli._die(exc.message, CliExit.USAGE_ERROR)
        return data
    if sys.stdin.isatty():
        typed = getpass.getpass("secret value (not echoed): ")
        return typed.encode("utf-8")
    raw = sys.stdin.buffer.read(MAX_SECRET_BYTES + 3)
    data = _strip_newline(raw)
    if len(data) > MAX_SECRET_BYTES:
        cli._die(
            f"the value on stdin is larger than {MAX_SECRET_BYTES} bytes",
            CliExit.USAGE_ERROR,
        )
    return data


@secret_app.command("set")
def set_secret(
    name: Annotated[str, typer.Argument(help="Secret name (A-Z, 0-9, _).")],
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            help="Read the value from this file (exact bytes) instead of stdin.",
        ),
    ] = None,
) -> None:
    """Store or replace a secret (value from stdin or --from-file, never argv)."""
    checked = _name(name)
    value = read_value(from_file)
    try:
        check_secret_value(value)
    except SecretValueError as exc:
        cli._die(exc.message, CliExit.USAGE_ERROR)
    settings = cli._settings()
    # V0.8 G6: while the value is in flight, any error line that quotes it (a
    # proxy or server echoing the body) prints `[secret:NAME]` instead - the
    # error path scrubs through the same active-secrets registry as the logs.
    ACTIVE.add(_HOLDER, {checked: value})
    try:
        with cli._client(settings) as client:
            payload = cli._fetch(lambda: client.put_secret(checked, value))
    finally:
        ACTIVE.remove(_HOLDER)
    verb = "stored" if payload.get("created") else "replaced"
    render.line(f"{verb} secret {checked}")


@secret_app.command("list")
def list_secrets(json_output: cli.JsonOption = False) -> None:
    """Your secrets: names and when they were set (never the values)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(client.secrets)
    if json_output:
        cli._echo_json(payload)
        return
    rows = payload.get("secrets")
    if not isinstance(rows, list) or not rows:
        render.line("no secrets")
        return
    table = render.table("NAME", "CREATED", "UPDATED", title="secrets")
    for row in rows:
        if isinstance(row, dict):
            table.add_row(
                render.cell(row.get("name")),
                render.cell(row.get("created_at")),
                render.cell(row.get("updated_at")),
            )
    render.show(table)


@secret_app.command("delete")
def delete_secret(
    name: Annotated[str, typer.Argument(help="Secret name.")],
) -> None:
    """Delete one of your secrets."""
    checked = _name(name)
    settings = cli._settings()
    with cli._client(settings) as client:
        cli._fetch(lambda: client.delete_secret(checked))
    render.line(f"deleted secret {checked}")
