"""Unit tests for ``spikes._s6.venv`` and ``spikes._s6.facts`` (0.2.10):
pure helpers, typed uv failures, read-only-safe rmtree, nvidia-smi parsing.
No uv or docker process is started here."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from spikes._s6 import facts, proc, venv
from spikes._s6.proc import Outcome


def test_pinned_requirements_match_the_project_lock() -> None:
    lock = (Path(__file__).resolve().parents[3] / "uv.lock").read_text(encoding="utf-8")
    for pin in venv.PINNED_REQUIREMENTS:
        name, _, version = pin.partition("==")
        if name == "numpy":
            continue  # not a project dependency; pinned for the spike only
        assert f'name = "{name}"\nversion = "{version}"' in lock, pin


def test_venv_python_and_site_packages(tmp_path: Path) -> None:
    py = venv.venv_python(tmp_path)
    if sys.platform == "win32":
        assert py == tmp_path / "Scripts" / "python.exe"
        sp = tmp_path / "Lib" / "site-packages"
    else:
        assert py == tmp_path / "bin" / "python"
        sp = tmp_path / "lib" / "python3.12" / "site-packages"
    assert venv.site_packages(tmp_path) is None
    sp.mkdir(parents=True)
    assert venv.site_packages(tmp_path) == sp


def test_dir_bytes_counts_regular_files_only(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 5)
    if sys.platform != "win32":
        os.symlink(tmp_path / "a", tmp_path / "link")
    assert venv.dir_bytes(tmp_path) == 15
    assert venv.dir_bytes(None) is None
    assert venv.dir_bytes(tmp_path / "missing") is None


def test_rmtree_removes_read_only_files(tmp_path: Path) -> None:
    target = tmp_path / "t"
    (target / "d").mkdir(parents=True)
    ro = target / "d" / "ro.txt"
    ro.write_text("x", encoding="utf-8")
    ro.chmod(stat.S_IREAD)
    if sys.platform != "win32":
        (target / "d").chmod(stat.S_IREAD | stat.S_IEXEC)
    venv.rmtree(target)
    assert not target.exists()
    venv.rmtree(target)  # idempotent


def test_write_requirements(tmp_path: Path) -> None:
    path = venv.write_requirements(tmp_path)
    assert path.read_text(encoding="utf-8").splitlines() == list(
        venv.PINNED_REQUIREMENTS
    )


def test_uv_env_isolates_the_measurement(tmp_path: Path) -> None:
    env = venv.uv_env(tmp_path / "cache")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "cache")
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["UV_NO_CONFIG"] == "1"


@pytest.mark.parametrize(
    ("outcome", "failure_type"),
    [
        (
            Outcome(("uv",), None, "", "", 0.0, error="could not start"),
            "uv_cli_missing",
        ),
        (Outcome(("uv",), None, "", "", 9.0, timed_out=True), "timeout"),
        (
            Outcome(
                ("uv",),
                2,
                "",
                "error: No interpreter found for Python 3.12 in managed installations",
                0.1,
            ),
            "python_not_found",
        ),
        (
            Outcome(
                ("uv",),
                2,
                "",
                "error: Failed to fetch: `https://pypi.org/simple/numpy/`\n"
                "  Caused by: error sending request",
                0.1,
            ),
            "network_error",
        ),
        (
            Outcome(
                ("uv",),
                2,
                "",
                "error: Network connectivity is disabled, but a remote index was "
                "requested (--offline)",
                0.1,
            ),
            "network_error",
        ),
        (Outcome(("uv",), 1, "", "error: something else entirely", 0.1), "uv_failed"),
    ],
)
def test_classify_uv_failures(outcome: Outcome, failure_type: str) -> None:
    err = venv._classify(outcome, what="uv pip install")
    assert err.failure_type == failure_type
    assert err.fix


def test_uv_version_without_uv() -> None:
    assert venv.uv_version(None) is None


# ---------------------------------------------------------------- facts
def test_parse_smi_list() -> None:
    text = (
        "GPU 0: NVIDIA GeForce RTX 3050 Laptop GPU "
        "(UUID: GPU-d90235b8-1c24-350c-3f63-96e00c45e94d)\n"
        "GPU 1: NVIDIA RTX A2000 (UUID: GPU-0000)\n"
    )
    assert facts.parse_smi_list(text) == [
        "NVIDIA GeForce RTX 3050 Laptop GPU",
        "NVIDIA RTX A2000",
    ]
    assert facts.parse_smi_list("") == []
    assert facts.parse_smi_list("No devices were found") == []


def test_host_gpu_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    gpu = facts.host_gpu()
    assert gpu["present"] is False
    assert gpu["failure_type"] == "no_gpu_on_host"
    assert gpu["fix"]


def test_host_gpu_smi_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        proc,
        "run",
        lambda argv, *, timeout, env=None, cwd=None: Outcome(
            tuple(argv), 9, "", "NVIDIA-SMI has failed", 0.1
        ),
    )
    gpu = facts.host_gpu()
    assert gpu["present"] is False and "has failed" in gpu["message"]


def test_host_gpu_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(
        proc,
        "run",
        lambda argv, *, timeout, env=None, cwd=None: Outcome(
            tuple(argv), 0, "GPU 0: X (UUID: 1)\n", "", 0.1
        ),
    )
    assert facts.host_gpu() == {"present": True, "devices": ["X"]}


def test_disk_free_bytes(tmp_path: Path) -> None:
    assert (facts.disk_free_bytes(tmp_path) or 0) > 0
    assert facts.disk_free_bytes(tmp_path / "missing") is None
    assert facts.disk_free_bytes(None) is None


def test_host_facts_shape(tmp_path: Path) -> None:
    doc = facts.host_facts(temp_dir=tmp_path, docker_root=None, uv_version="uv 1.0")
    for key in (
        "system",
        "release",
        "wsl",
        "is_wsl",
        "cpu_count",
        "memory_total_bytes",
        "uv_version",
        "gpu",
        "disk_free_bytes",
    ):
        assert key in doc
    assert doc["disk_free_bytes"]["docker_root"] is None
    assert isinstance(doc["gpu"]["present"], bool)
    assert doc["uv_version"] == "uv 1.0"
