"""V0.8 G4 (D177): the CLI follows the ONE project spec the browser submit shares.

``frontend/src/pages/jobs/submit/projectSpec.json`` states how a project folder
becomes a job - ignore rules, size and path limits, the tree manifest's chunking,
``compute.yaml`` reading; ``projectSpecVectors.json`` holds its test vectors. The
dashboard's vitest suite runs the same vectors through the browser code; this test
runs them through the CLI's own
code (``common/walk.py``, ``common/tree.py``, ``common/manifest.py``), so the two
cannot drift apart unnoticed: a change on either side breaks one of the two tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from meshforge.cli.transfer import HAVE_BATCH
from meshforge.common import cas, manifest, tree, walk

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SUBMIT = ROOT / "frontend" / "src" / "pages" / "jobs" / "submit"
SPEC_FILE = SUBMIT / "projectSpec.json"
SPEC: dict[str, Any] = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
VECTORS: dict[str, Any] = json.loads(
    (SUBMIT / "projectSpecVectors.json").read_text(encoding="utf-8")
)
WINDOWS = sys.platform.startswith("win")


def test_constants_match_the_cli() -> None:
    assert SPEC["version"] == 1
    assert SPEC["manifest_file"] == manifest.MANIFEST_FILE_NAME
    assert SPEC["max_manifest_file_bytes"] == manifest.MAX_MANIFEST_BYTES
    assert tuple(SPEC["inferred_entrypoints"]) == manifest.INFERRED_ENTRYPOINTS
    assert SPEC["max_name_chars"] == manifest.MAX_NAME_CHARS
    assert tuple(SPEC["builtin_ignores"]) == walk.BUILTIN_IGNORES
    assert tuple(SPEC["ignore_file_names"]) == walk.IGNORE_FILE_NAMES
    assert SPEC["max_ignore_file_bytes"] == walk.MAX_IGNORE_FILE_BYTES
    assert SPEC["max_path_bytes"] == walk.MAX_PATH_BYTES
    assert SPEC["max_component_bytes"] == walk.MAX_COMPONENT_BYTES
    assert SPEC["tree_version"] == tree.TREE_VERSION
    assert SPEC["max_entries"] == tree.DEFAULT_MAX_ENTRIES
    assert SPEC["max_total_bytes"] == tree.DEFAULT_MAX_TOTAL_BYTES
    assert SPEC["max_tree_manifest_bytes"] == tree.DEFAULT_MAX_MANIFEST_BYTES
    assert SPEC["chunk_size"] == cas.DEFAULT_CHUNK_SIZE
    assert SPEC["min_chunk_size"] == cas.MIN_CHUNK_SIZE
    assert SPEC["max_chunk_size"] == cas.MAX_CHUNK_SIZE
    assert SPEC["have_batch"] == HAVE_BATCH


def _content(spec: str | dict[str, Any]) -> tuple[bytes, bool]:
    if isinstance(spec, str):
        return spec.encode("utf-8"), False
    if "text" in spec:
        return str(spec["text"]).encode("utf-8"), bool(spec.get("exec"))
    unit = str(spec["repeat"]).encode("utf-8")
    size = int(spec["size"])
    return (unit * (size // len(unit) + 1))[:size], bool(spec.get("exec"))


def _materialise(root: Path, vector: dict[str, Any]) -> None:
    for rel, spec in vector["files"].items():
        data, executable = _content(spec)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if not WINDOWS:
            path.chmod(0o755 if executable else 0o644)
    for rel in vector.get("dirs", []):
        (root / rel).mkdir(parents=True, exist_ok=True)


def _portable(vector: dict[str, Any]) -> bool:
    """Windows has no exec bit (a packed script reads exec: false there)."""
    return not any(
        isinstance(s, dict) and s.get("exec") for s in vector["files"].values()
    )


@pytest.mark.parametrize("vector", VECTORS["walk_vectors"], ids=lambda v: v["name"])
def test_walk_vectors(vector: dict[str, Any], tmp_path: Path) -> None:
    if WINDOWS and not _portable(vector):
        pytest.skip("exec bits are POSIX-only")
    root = tmp_path / "project"
    root.mkdir()
    _materialise(root, vector)
    built = tree.build_tree(
        root, chunk_size=vector.get("chunk_size", cas.DEFAULT_CHUNK_SIZE)
    )
    assert [e.to_json() for e in built.tree.entries] == vector["expect_entries"]
    assert built.ignored == vector["expect_ignored"]
    assert str(built.tree.tree_id) == vector["expect_tree_id"]


@pytest.mark.parametrize("vector", VECTORS["yaml_vectors"], ids=lambda v: v["name"])
def test_yaml_vectors(vector: dict[str, Any]) -> None:
    loader = (
        manifest._StrictSafeLoader
    )  # the CLI's own loader (no aliases, no duplicates)
    if vector.get("error"):
        with pytest.raises(yaml.YAMLError):
            yaml.load(vector["text"], Loader=loader)  # noqa: S506 - SafeLoader subclass
        return
    assert yaml.load(vector["text"], Loader=loader) == vector["expect"]  # noqa: S506


def test_every_vector_kind_is_present() -> None:
    """Guard against a spec edit that silently drops the hard cases."""
    names = {v["name"] for v in VECTORS["walk_vectors"]}
    hard = {"nested_ignore_files", "negation_and_dir_patterns", "chunked_files"}
    assert hard | {"empty_dirs"} <= names
    assert any(v.get("error") for v in VECTORS["yaml_vectors"])
    assert any(v.get("browser_unsupported") for v in VECTORS["yaml_vectors"])
    assert os.fspath(SPEC_FILE).endswith("projectSpec.json")
