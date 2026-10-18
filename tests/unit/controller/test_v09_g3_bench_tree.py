"""V0.9 G3 lane B item 3: the in-memory benchmark tree is exactly the tree
`compute run` would build from the materialised folder (same tree id), so a
forced run and a hand-submitted one share blobs, envs and fingerprints."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.bench.workloads import ParamsError, build_job, get_family
from meshforge.common.manifest import MIN_PRIORITY
from meshforge.common.tree import build_tree, parse_tree
from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX, bench_tree

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "family", ["cpu.matmul", "cpu.sort", "memory.copy", "gpu.matmul"]
)
def test_bench_tree_equals_the_folder_tree(tmp_path: Path, family: str) -> None:
    params = get_family(family).params_dict(get_family(family).smallest_params())
    built = bench_tree(family, params, 11)
    folder = build_job(family, params, 11, tmp_path / "job")
    expected = build_tree(folder.path)
    assert built.tree.tree_id == expected.tree.tree_id
    assert built.manifest_bytes == expected.manifest
    parsed = parse_tree(built.manifest_bytes, expected=built.tree.tree_id)
    assert parsed.blob_sizes() == {k: len(v) for k, v in built.blobs.items()}
    assert built.manifest.name.startswith("bench-")


def test_bench_tree_refuses_bad_params() -> None:
    with pytest.raises(ParamsError):
        bench_tree("cpu.matmul", {"n": 1}, 1)


def test_constants() -> None:
    assert MIN_PRIORITY == -10
    assert IDEMPOTENCY_PREFIX == "bench:"
