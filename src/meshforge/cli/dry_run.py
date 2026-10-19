"""``compute run --dry-run``: what would be submitted, without submitting (V0.10 G1).

Reads the manifest and builds the project's tree exactly like ``compute run``,
then prints - on stdout, one ``key  value`` line each - the job name, the
project size and, for a ``split:`` job, the items the controller will resolve
(same resolver, :mod:`meshforge.common.split_items`, same order): the count,
the first and last item and the chunking. Nothing is uploaded, no controller is
contacted, no settings are needed.

**What a dry run cannot see.** Items inside a named input (``inputs:``) live in
the controller's store: a ``list_file``/``lines`` file under an input mount is
reported as "resolved at submit", and a ``glob`` that can reach into a mount
counts only the project's matches and says so. The controller's
``[controller.split]`` caps may be lower than the built-in ones used here.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import IO, TYPE_CHECKING

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit

if TYPE_CHECKING:
    from meshforge.common.manifest import Manifest, SplitSpec
    from meshforge.common.tree import BuiltTree

__all__ = ["dry_run"]

_PAD = 10


def _row(key: str, value: object) -> None:
    render.line(f"{key:<{_PAD}} {value}")


def _over_text(spec: SplitSpec) -> str:
    over = spec.over
    if over.glob is not None:
        return f"glob {over.glob}"
    if over.list is not None:
        return f"list ({len(over.list)} inline items)"
    if over.list_file is not None:
        return f"list_file {over.list_file}"
    if over.range is not None:
        r = over.range
        return f"range start={r.start} stop={r.stop} step={r.step}"
    return f"lines {over.lines}" + (
        " (header repeated per chunk)" if over.header else ""
    )


def _under_mount(path: str, mounts: list[str]) -> str | None:
    for mount in mounts:
        if path.startswith(mount + "/"):
            return mount
    return None


def _combine_text(spec: SplitSpec) -> str:
    if spec.combine.builtin is not None:
        return spec.combine.builtin
    return "script " + " ".join(spec.combine.script or ())


def _split(manifest: Manifest, built: BuiltTree) -> None:
    from meshforge.common.manifest import ManifestError
    from meshforge.common.split_items import (
        chunk_count,
        glob_parts,
        may_enter,
        resolve,
    )

    spec = manifest.split
    assert spec is not None  # noqa: S101 - the caller checks
    mounts = [item.mount for item in manifest.inputs]
    _row("split", _over_text(spec))
    file_path = spec.over.list_file or spec.over.lines
    if file_path is not None and (mount := _under_mount(file_path, mounts)):
        _row("items", f"resolved by the controller at submit (named input {mount})")
        return
    reachable: list[str] = []
    if spec.over.glob is not None:
        parts = glob_parts(spec.over.glob)
        reachable = [m for m in mounts if may_enter(parts, m)]

    def _open(sha256_hex: str) -> IO[bytes]:
        return built.sources[sha256_hex].open("rb")

    try:
        items = resolve(spec, built.tree, {}, _open)
    except ManifestError as exc:
        if reachable and spec.over.glob is not None:
            _row("items", "0 in the project; named inputs are resolved at submit")
            return
        cli._die(f"compute.yaml: {exc.message}", CliExit.FAILURE)
    if items.over == "lines":
        shown = f"(records 1..{items.count})"
    else:
        shown = f"(first {items.items[0]}, last {items.items[-1]})"
    extra = (
        " + matches in named inputs " + ", ".join(reachable) + " (resolved at submit)"
        if reachable
        else ""
    )
    _row("items", f"{items.count} {shown}{extra}")
    if items.item_bytes is not None:
        _row("item data", render.format_bytes(items.item_bytes))
    if spec.chunk.items is None:
        _row("chunking", "auto (the planner sizes chunks at dispatch)")
    else:
        chunks = chunk_count(items.count, spec.chunk.items)
        _row("chunking", f"{spec.chunk.items} items per chunk -> {chunks} chunks")
    _row("combine", _combine_text(spec))
    nodes = f"min {spec.min_nodes}, max " + (
        "all suitable" if spec.max_nodes is None else str(spec.max_nodes)
    )
    _row("nodes", nodes)


def dry_run(project: Path, name: str | None) -> None:
    """Print what ``compute run`` would submit (module docstring)."""
    from meshforge.common.manifest import ManifestError, load_manifest, sanitize_name
    from meshforge.common.tree import TreeError, build_tree

    try:
        manifest = load_manifest(project)
    except ManifestError as exc:
        cli._die(f"compute.yaml: {exc.message}", CliExit.FAILURE)
    try:
        built = build_tree(project)
    except (TreeError, OSError) as exc:
        reason = exc.message if isinstance(exc, TreeError) else type(exc).__name__
        cli._die(f"cannot read {project}: {reason}", CliExit.FAILURE)
    render.note("dry run: nothing uploaded, no job submitted")
    _row("job", sanitize_name(name) if name is not None else manifest.name)
    _row("entrypoint", shlex.join(manifest.entrypoint))
    tree = built.tree
    _row(
        "project",
        f"{sum(1 for _ in tree.files())} files, "
        f"{render.format_bytes(tree.total_bytes)} (tree {tree.tree_id})",
    )
    if manifest.split is None:
        _row("split", "no (the job runs whole on one node)")
        return
    _split(manifest, built)
