"""V1.0 G9-E r1 (D438): ``mode3_check.py build`` declares the workload's own
per-item estimate (``split.item_seconds``) so lab parts B/C split on run 1;
``--no-declare`` leaves it out. The folder still loads with the fabric's own
manifest loader, and the printed content digest is the written folder's."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.common.manifest import load_manifest
from mode3_check import files_digest, item_count, main

pytestmark = pytest.mark.unit


def _digest(folder: Path) -> str:
    return files_digest(
        {
            p.relative_to(folder).as_posix(): p.read_bytes()
            for p in folder.rglob("*")
            if p.is_file()
        }
    )


@pytest.mark.parametrize(
    ("family", "param", "items"),
    [
        ("split.hash_files", "files=12", 12),
        ("split.image_classify", "images=12", 12),
        ("split.montecarlo_range", "items=40", 40),
    ],
)
def test_build_declares_item_seconds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    family: str,
    param: str,
    items: int,
) -> None:
    dest = tmp_path / "job"
    assert main(["build", family, str(dest), "--param", param]) == 0
    out = capsys.readouterr().out
    manifest = load_manifest(dest)
    assert manifest.split is not None
    declared = manifest.split.item_seconds
    assert declared is not None and declared > 0
    assert item_count(dest, manifest.split.model_dump(mode="json")) == items
    assert f"declared rate (split.item_seconds): {declared:g} s per item" in out
    assert f"content: {_digest(dest)}" in out
    assert (dest / "compute.yaml").read_text().startswith("# MeshForge benchmark job")


def test_no_declare_leaves_it_out_and_says_warm_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "job"
    args = ["build", "split.hash_files", str(dest), "--param", "files=12"]
    assert main([*args, "--no-declare"]) == 0
    out = capsys.readouterr().out
    manifest = load_manifest(dest)
    assert manifest.split is not None and manifest.split.item_seconds is None
    assert "warm-up run" in out
    assert f"content: {_digest(dest)}" in out
