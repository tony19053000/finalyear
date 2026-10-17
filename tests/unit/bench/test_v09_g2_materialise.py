"""V0.9 G2 (0.9.2 item 2): ``build_job`` writes a normal, byte-identical job folder."""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads import (
    MaterialiseError,
    ParamsError,
    build_job,
    get_family,
    list_families,
)
from meshforge.bench.workloads.materialise import (
    ENTRYPOINT,
    KERNEL_PACKAGE,
    NUMPY_REQUIREMENT,
    job_files,
)
from meshforge.common.bundle import pack_to_file
from meshforge.common.manifest import load_manifest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
FAMILIES = list_families()
IDS = [f.name for f in FAMILIES]


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_same_inputs_give_a_byte_identical_folder(family: Any, tmp_path: Path) -> None:
    a = build_job(family.name, {}, 42, tmp_path / "a")
    b = build_job(family, family.smallest_params(), 42, tmp_path / "b")
    assert a.content_digest == b.content_digest
    assert _files(a.path) == _files(b.path)
    # ... and therefore the same bundle digest (one blob, nothing re-uploaded)
    da = pack_to_file(a.path, tmp_path / "a.bundle").digest
    db = pack_to_file(b.path, tmp_path / "b.bundle").digest
    assert da == db


def test_a_different_seed_or_params_is_a_different_folder(tmp_path: Path) -> None:
    base = build_job("cpu.sort", {}, 1, tmp_path / "a")
    seed = build_job("cpu.sort", {}, 2, tmp_path / "b")
    params = build_job("cpu.sort", {"repeats": 19}, 1, tmp_path / "c")
    assert len({base.content_digest, seed.content_digest, params.content_digest}) == 3
    assert base.manifest.name != seed.manifest.name


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_manifest_is_one_the_fabric_accepts(family: Any, tmp_path: Path) -> None:
    built = build_job(family.name, {}, 3, tmp_path / "job")
    manifest = load_manifest(built.path)
    assert manifest == built.manifest
    assert manifest.entrypoint == ENTRYPOINT  # an argv list, never a shell string
    assert manifest.outputs == ("results/",)
    assert manifest.limits.network == "none"
    assert manifest.limits.timeout == 900
    assert manifest.name.startswith(f"bench-{family.name}-")
    params = family.smallest_params()
    if family.gpu:
        assert manifest.env.base == "cuda-pytorch"
        assert manifest.resources.gpu == "required"
        assert manifest.resources.vram_gb == family.vram_gb(params)
        assert manifest.env.requirements is None
    else:
        assert manifest.env.base == "python-cpu"
        assert manifest.resources.gpu == "none"
        assert manifest.resources.vram_gb is None
    assert manifest.resources.ram_gb is not None
    assert manifest.resources.ram_gb >= family.ram_gb(params)
    assert (manifest.env.requirements == "requirements.txt") is family.needs_numpy
    if family.needs_numpy:
        assert (built.path / "requirements.txt").read_text() == NUMPY_REQUIREMENT + "\n"
    else:
        assert not (built.path / "requirements.txt").exists()


def test_the_request_file_is_canonical(tmp_path: Path) -> None:
    built = build_job("cpu.zlib", {"level": 9, "size_mb": 96}, 5, tmp_path / "j")
    text = (built.path / "workload_params.json").read_text()
    assert text == (
        '{"family":"cpu.zlib","params":{"level":9,"repeats":2,"size_mb":96},"seed":5}\n'
    )
    assert json.loads(text)["params"] == built.params


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_folder_holds_only_its_own_kernel(family: Any) -> None:
    from meshforge.bench.workloads.kernels.runner import KERNEL_MODULES

    files = job_files(family.name, {}, 0)
    kernels = sorted(n for n in files if n.startswith(f"{KERNEL_PACKAGE}/"))
    expected = {"__init__", "_support", "runner", KERNEL_MODULES[family.name]}
    assert kernels == sorted(f"{KERNEL_PACKAGE}/{n}.py" for n in expected)


ALLOWED_TOP_LEVEL = set(sys.stdlib_module_names) | {"numpy", "torch", KERNEL_PACKAGE}


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_job_code_imports_no_meshforge_and_nothing_uninstalled(family: Any) -> None:
    """The job env has no MeshForge/pydantic: only stdlib, numpy, torch, itself."""
    for name, data in job_files(family.name, {}, 0).items():
        if not name.endswith(".py"):
            continue
        assert b"\r" not in data, name
        for node in ast.walk(ast.parse(data, filename=name)):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root in ALLOWED_TOP_LEVEL, (name, root)


def test_numpy_pin_matches_the_lock_file() -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\nname = "numpy"\nversion = "([^"]+)"', lock)
    assert match, "numpy not found in uv.lock"
    assert f"numpy=={match.group(1)}" == NUMPY_REQUIREMENT


def test_a_non_empty_destination_is_refused(tmp_path: Path) -> None:
    dest = tmp_path / "busy"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine")
    with pytest.raises(MaterialiseError, match="not an empty folder"):
        build_job("cpu.sort", {}, 1, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    afile = tmp_path / "file"
    afile.write_text("x")
    with pytest.raises(MaterialiseError):
        build_job("cpu.sort", {}, 1, afile)


def test_an_empty_destination_is_used(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    built = build_job("cpu.sort", {}, 1, tmp_path / "empty")
    assert (built.path / "compute.yaml").is_file()


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.0])
def test_bad_seeds_are_refused(seed: Any, tmp_path: Path) -> None:
    with pytest.raises(ParamsError, match="seed"):
        build_job("cpu.sort", {}, seed, tmp_path / "j")
    assert not (tmp_path / "j").exists()


def test_params_outside_the_window_write_nothing(tmp_path: Path) -> None:
    with pytest.raises(ParamsError, match="reference mid laptop"):
        build_job("cpu.sort", {"repeats": 1}, 1, tmp_path / "j")
    assert not (tmp_path / "j").exists()


def test_build_job_accepts_a_family_object_or_name() -> None:
    family = get_family("memory.copy")
    assert job_files(family, {}, 9) == job_files("memory.copy", {}, 9)
