"""``compute data push / ls / show / pin / rm`` - named inputs (V0.5 G5, D92).

``compute data push NAME DIR [--pin]`` builds DIR's tree (same ignore rules as
``compute run``), uploads only what the controller lacks (have/need, resumable
parts) and registers it as the next version of NAME - or answers the version
that already names this exact tree. A job then names it in ``compute.yaml``::

    inputs:
      - ref: NAME@VERSION        # or NAME@sha256:<tree id>
        mount: data/NAME         # copied read-only into the job's folder

``--pin`` keeps the input on every node's cache and the controller's store (GC
never evicts it); ``compute data pin NAME@VERSION [--off]`` changes that later.
``compute data rm NAME@VERSION`` is refused while an unfinished job uses it.
REST only, like every ``compute`` command.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.table import Table

from meshforge.cli import app as cli
from meshforge.cli import render
from meshforge.cli.exits import CliExit

__all__ = ["data_app"]

data_app = typer.Typer(
    name="data",
    help="Named inputs: datasets and models registered once, used by name.",
    no_args_is_help=True,
    rich_markup_mode=None,
)

_REF_RE: Final = re.compile(
    r"(?P<name>[a-z0-9][a-z0-9._-]{0,63})@(?P<version>[1-9][0-9]{0,8})"
)


def _name(value: str) -> str:
    from meshforge.common.manifest import INPUT_NAME_RE

    if not INPUT_NAME_RE.fullmatch(value):
        cli._die(
            "NAME is 1-64 lower-case letters, digits, '.', '_', '-' (starting with "
            "a letter or digit)",
            CliExit.USAGE_ERROR,
        )
    return value


def _ref(value: str) -> tuple[str, int]:
    match = _REF_RE.fullmatch(value)
    if match is None:
        cli._die("give NAME@VERSION (see `compute data ls`)", CliExit.USAGE_ERROR)
    return match.group("name"), int(match.group("version"))


def inputs_table(items: object, *, title: str) -> Table:
    """Input versions as the standard table (every server string via ``cell``)."""
    table = render.table("REF", "TREE", "FILES", "SIZE", "PINNED", title=title)
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        table.add_row(
            render.cell(item.get("ref")),
            render.cell(item.get("tree_id")),
            render.cell(item.get("files")),
            render.format_bytes(item.get("size")),
            "yes" if item.get("pinned") is True else "no",
        )
    return table


@data_app.command("push")
def push(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    path: Annotated[
        Path, typer.Argument(metavar="DIR", help="The folder to register.")
    ],
    pin: Annotated[
        bool, typer.Option("--pin", help="Never evict it from any cache.")
    ] = False,
) -> None:
    """Upload DIR and register it as the next version of NAME."""
    from meshforge.cli.transfer import TransferError, upload_tree
    from meshforge.common.tree import TreeError, build_tree

    _name(name)
    folder = path.resolve()
    if not folder.is_dir():
        cli._die(f"{path} is not a folder", CliExit.USAGE_ERROR)
    try:
        built = build_tree(folder)
    except (TreeError, OSError) as exc:
        reason = exc.message if isinstance(exc, TreeError) else type(exc).__name__
        cli._die(f"cannot read {path}: {reason}", CliExit.FAILURE)
    settings = cli._settings()
    with cli._client(settings) as client:
        try:
            report = cli._fetch(lambda: upload_tree(client, built))
        except (TransferError, OSError) as exc:
            reason = (
                exc.message if isinstance(exc, TransferError) else type(exc).__name__
            )
            cli._die(f"upload failed: {reason}", CliExit.FAILURE)
        render.note(
            f"uploaded {report.uploaded_blobs} files / "
            f"{report.uploaded_bytes} bytes, skipped {report.skipped_blobs}"
        )
        done = cli._fetch(
            lambda: client.push_input(name, str(built.tree.tree_id), pinned=pin)
        )
    verb = "registered" if done.get("created") else "already registered as"
    render.line(
        f"{verb} {render.echo(done.get('ref'))} ({render.echo(done.get('files'))} files, "
        f"{render.format_bytes(done.get('size'))})"
        f"{' pinned' if done.get('pinned') else ''}"
    )


@data_app.command("ls")
def ls(as_json: cli.JsonOption = False) -> None:
    """Every registered input version."""
    settings = cli._settings()
    with cli._client(settings) as client:
        listed = cli._fetch(client.inputs)
    if as_json:
        cli._echo_json(listed)
        return
    items = listed.get("inputs") or []
    if not items:
        render.line("no named inputs (compute data push NAME DIR)")
        return
    render.show(inputs_table(items, title="named inputs"))


@data_app.command("show")
def show(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    as_json: cli.JsonOption = False,
) -> None:
    """One input's versions."""
    _name(name)
    settings = cli._settings()
    with cli._client(settings) as client:
        listed = cli._fetch(lambda: client.input_versions(name))
    if as_json:
        cli._echo_json(listed)
        return
    render.show(inputs_table(listed.get("versions"), title=f"input {name}"))


@data_app.command("pin")
def pin(
    ref: Annotated[str, typer.Argument(metavar="NAME@VERSION")],
    off: Annotated[bool, typer.Option("--off", help="Unpin instead.")] = False,
) -> None:
    """Pin (or with --off unpin) one version."""
    name, version = _ref(ref)
    settings = cli._settings()
    with cli._client(settings) as client:
        done = cli._fetch(lambda: client.pin_input(name, version, pinned=not off))
    render.line(
        f"{render.echo(done.get('ref'))} {'pinned' if done.get('pinned') else 'unpinned'}"
    )


@data_app.command("rm")
def rm(ref: Annotated[str, typer.Argument(metavar="NAME@VERSION")]) -> None:
    """Remove one version (refused while an unfinished job uses it)."""
    name, version = _ref(ref)
    settings = cli._settings()
    with cli._client(settings) as client:
        done = cli._fetch(lambda: client.remove_input(name, version))
    render.line(f"removed {render.echo(done.get('ref'))}")
