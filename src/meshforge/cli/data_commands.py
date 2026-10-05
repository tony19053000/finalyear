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
from typing import Annotated, Any, Final

import typer

from meshforge.cli import app as cli
from meshforge.common.errors import ExitCode

__all__ = ["data_app"]

data_app = typer.Typer(
    name="data",
    help="Named inputs: datasets and models registered once, used by name.",
    no_args_is_help=True,
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
            ExitCode.USAGE_ERROR,
        )
    return value


def _ref(value: str) -> tuple[str, int]:
    match = _REF_RE.fullmatch(value)
    if match is None:
        cli._die("give NAME@VERSION (see `compute data ls`)", ExitCode.USAGE_ERROR)
    return match.group("name"), int(match.group("version"))


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"  # pragma: no cover


def _row(item: dict[str, Any]) -> str:
    pin = "pinned" if item.get("pinned") else ""
    return (
        f"{item.get('ref', '')!s:<32} {item.get('tree_id', '')!s:<71} "
        f"{item.get('files', 0)!s:>7} {_size(int(item.get('size', 0))):>10} {pin}"
    )


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
        cli._die(f"{path} is not a folder", ExitCode.USAGE_ERROR)
    try:
        built = build_tree(folder)
    except (TreeError, OSError) as exc:
        reason = exc.message if isinstance(exc, TreeError) else type(exc).__name__
        cli._die(f"cannot read {path}: {reason}", ExitCode.FAILURE)
    settings = cli._settings()
    with cli._client(settings) as client:
        try:
            report = cli._fetch(lambda: upload_tree(client, built))
        except (TransferError, OSError) as exc:
            reason = (
                exc.message if isinstance(exc, TransferError) else type(exc).__name__
            )
            cli._die(f"upload failed: {reason}", ExitCode.FAILURE)
        typer.echo(
            f"{cli.PROG_NAME}: uploaded {report.uploaded_blobs} files / "
            f"{report.uploaded_bytes} bytes, skipped {report.skipped_blobs}",
            err=True,
        )
        done = cli._fetch(
            lambda: client.push_input(name, str(built.tree.tree_id), pinned=pin)
        )
    verb = "registered" if done.get("created") else "already registered as"
    typer.echo(
        f"{verb} {done.get('ref')} ({done.get('files')} files, "
        f"{_size(int(done.get('size', 0)))}){' pinned' if done.get('pinned') else ''}"
    )


@data_app.command("ls")
def ls(
    as_json: Annotated[bool, typer.Option("--json", help="Raw JSON.")] = False,
) -> None:
    """Every registered input version."""
    import json

    settings = cli._settings()
    with cli._client(settings) as client:
        listed = cli._fetch(client.inputs)
    if as_json:
        typer.echo(json.dumps(listed, indent=2, sort_keys=True))
        return
    items = listed.get("inputs") or []
    if not items:
        typer.echo("no named inputs (compute data push NAME DIR)")
        return
    typer.echo(f"{'REF':<32} {'TREE':<71} {'FILES':>7} {'SIZE':>10}")
    for item in items:
        if isinstance(item, dict):
            typer.echo(_row(item))


@data_app.command("show")
def show(
    name: Annotated[str, typer.Argument(metavar="NAME")],
    as_json: Annotated[bool, typer.Option("--json", help="Raw JSON.")] = False,
) -> None:
    """One input's versions."""
    import json

    _name(name)
    settings = cli._settings()
    with cli._client(settings) as client:
        listed = cli._fetch(lambda: client.input_versions(name))
    if as_json:
        typer.echo(json.dumps(listed, indent=2, sort_keys=True))
        return
    typer.echo(f"{'REF':<32} {'TREE':<71} {'FILES':>7} {'SIZE':>10}")
    for item in listed.get("versions") or []:
        if isinstance(item, dict):
            typer.echo(_row(item))


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
    typer.echo(f"{done.get('ref')} {'pinned' if done.get('pinned') else 'unpinned'}")


@data_app.command("rm")
def rm(ref: Annotated[str, typer.Argument(metavar="NAME@VERSION")]) -> None:
    """Remove one version (refused while an unfinished job uses it)."""
    name, version = _ref(ref)
    settings = cli._settings()
    with cli._client(settings) as client:
        done = cli._fetch(lambda: client.remove_input(name, version))
    typer.echo(f"removed {done.get('ref')}")
