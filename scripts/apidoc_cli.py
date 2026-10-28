"""Typer apps -> Markdown CLI reference from ``--help`` (V1.0 G5, D414).

Walks the Click command tree Typer builds and records each command's own
``--help`` text at a fixed width, so the page is byte-identical on every
machine (no terminal size, no colour: Typer's rich help is off in this
project, and ``NO_COLOR``/``COLUMNS`` cannot reach a Click context made here).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePath, PurePosixPath
from typing import Any, Final

import typer

WIDTH: Final = 100


def _posix(value: Any) -> Any:
    """``value`` with every :class:`~pathlib.PurePath` written with ``/``."""
    if isinstance(value, PurePath):
        return PurePosixPath(value.as_posix())
    if isinstance(value, (list, tuple)):
        return type(value)(_posix(v) for v in value)
    return value


@contextmanager
def _portable_defaults(command: Any) -> Iterator[None]:
    """Show ``Path`` defaults the same on every OS while the help is built.

    Click prints a default with ``str()``, so ``Path("/etc/x.toml")`` reads
    ``\\etc\\x.toml`` on Windows and the page would differ per machine. The
    defaults are swapped for their POSIX spelling and restored afterwards (the
    app object is the real one, imported from ``meshforge``).
    """
    saved: list[tuple[Any, Any]] = []
    for param in getattr(command, "params", ()):
        portable = _posix(param.default)
        if portable is not param.default:
            saved.append((param, param.default))
            param.default = portable
    try:
        yield
    finally:
        for param, default in saved:
            param.default = default


def _walk(
    command: Any, path: tuple[str, ...], parent: Any
) -> Iterator[tuple[tuple[str, ...], str]]:
    # The command's own Context class: Typer vendors its Click (typer._click),
    # so the installed ``click`` package's classes are not the ones it uses.
    ctx = command.context_class(
        command,
        parent=parent,
        info_name=path[-1],
        terminal_width=WIDTH,
        max_content_width=WIDTH,
    )
    # Plain Click help for every tool: rich mode (the agent and controller
    # apps) prints boxes to a console sized by the terminal instead of
    # returning text. Same words, a stable layout.
    saved = getattr(command, "rich_markup_mode", None)
    if saved is not None:
        command.rich_markup_mode = None
    try:
        with _portable_defaults(command):
            text = command.get_help(ctx)
    finally:
        if saved is not None:
            command.rich_markup_mode = saved
    yield path, text
    # Typer's group is a plain click.Command with group methods (typer 0.2x),
    # so groups are recognised by what they can do, not by their class.
    lister = getattr(command, "list_commands", None)
    getter = getattr(command, "get_command", None)
    if callable(lister) and callable(getter):
        for name in sorted(lister(ctx)):
            sub = getter(ctx, name)
            if sub is None or sub.hidden:
                continue
            yield from _walk(sub, (*path, name), ctx)


def help_pages(app: typer.Typer, name: str) -> list[tuple[str, str]]:
    """``(full command, help text)`` for ``name`` and every sub-command."""
    root = typer.main.get_command(app)
    return [
        (" ".join(path), text.rstrip()) for path, text in _walk(root, (name,), None)
    ]


def render_tool(name: str, app: typer.Typer, intro: str) -> list[str]:
    pages = help_pages(app, name)
    lines = [f"## `{name}`", "", intro, "", f"{len(pages)} commands and groups.", ""]
    for command, text in pages:
        lines += [f"### `{command}`", "", "```text", text, "```", ""]
    return lines
