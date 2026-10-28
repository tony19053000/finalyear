"""Typer apps -> Markdown CLI reference from ``--help`` (V1.0 G5, D414).

Walks the Click command tree Typer builds and records each command's own
``--help`` text at a fixed width, so the page is byte-identical on every
machine (no terminal size, no colour: Typer's rich help is off in this
project, and ``NO_COLOR``/``COLUMNS`` cannot reach a Click context made here).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import typer

WIDTH: Final = 100


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
