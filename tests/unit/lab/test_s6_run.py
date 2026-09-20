"""Unit tests for ``spikes._s6.run`` (0.2.10): configuration defaults, the
budget, and the case gates (skips and typed setup failures) with the docker
and uv CLIs stubbed out - no daemon, no network, no uv process."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import validate
from spikes._s6 import run as r
from spikes._s6 import venv


def test_run_config_defaults_and_quick() -> None:
    cfg = r.RunConfig()
    assert (cfg.effective_reps(), cfg.effective_warmup(), cfg.effective_budget()) == (
        5,
        1,
        3600.0,
    )
    assert cfg.effective_reps() >= MIN_VALID_REPS
    quick = r.RunConfig(quick=True)
    assert quick.effective_reps() == 2 and quick.effective_budget() == 900.0
    assert r.RunConfig(quick=True, reps=7, warmup=0, budget_s=30).effective_reps() == 7
    assert r.RunConfig(warmup=0).effective_warmup() == 0


def test_budget() -> None:
    now = [100.0]
    budget = r.Budget(10.0, clock=lambda: now[0])
    assert budget.remaining() == 10.0 and not budget.expired()
    assert budget.slice_for(3.0) == 3.0 and budget.slice_for(30.0) == 10.0
    now[0] = 111.0
    assert budget.expired() and budget.slice_for(3.0) == 0.0


def test_case_ids_fixed() -> None:
    assert r.CASE_IDS == (
        "docker-start",
        "docker-pull-cold",
        "docker-gpu-smi",
        "docker-gpu-torch",
        "venv-cold",
        "venv-warm",
        "venv-start",
    )
    assert len(r.CASES) == len(r.CASE_IDS)


def _ctx(
    tmp_path: Path,
    cfg: r.RunConfig,
    *,
    docker: str | None,
    docker_ok: bool,
    uv: str | None,
    gpu: bool,
) -> r._Context:
    dfacts: dict[str, Any] = (
        {"ok": True}
        if docker_ok
        else {
            "ok": False,
            "failure_type": "docker_daemon_unreachable",
            "message": "down",
            "fix": "start it",
        }
    )
    host: dict[str, Any] = {
        "gpu": {"present": True, "devices": ["G"]}
        if gpu
        else {"present": False, "message": "no smi", "fix": "driver"},
        "disk_free_bytes": {"temp_dir": 1_000_000, "docker_root": None},
        "uv_version": "uv x",
    }
    return r._Context(
        cfg=cfg,
        budget=r.Budget(cfg.effective_budget()),
        temp_dir=tmp_path,
        log=lambda _m: None,
        docker=docker,
        docker_facts=dfacts,
        uv=uv,
        host=host,
    )


def _setup_failure(case: Any) -> dict[str, Any]:
    assert case.aborted and not case.samples and len(case.errors) == 1
    err = case.errors[0]
    assert err["phase"] == "setup"
    return err


def test_docker_cases_skipped_by_flag(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(skip_docker=True),
        docker=None,
        docker_ok=False,
        uv=None,
        gpu=True,
    )
    for fn in (
        r.case_docker_start,
        r.case_docker_pull_cold,
        r.case_docker_gpu_smi,
        r.case_docker_gpu_torch,
    ):
        err = _setup_failure(fn(ctx))
        assert err["failure_type"] == r.SKIPPED and err["message"] == "--skip-docker"
    assert set(ctx.skipped) == {
        "docker-start",
        "docker-pull-cold",
        "docker-gpu-smi",
        "docker-gpu-torch",
    }


def test_docker_cases_without_cli_are_typed_failures(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, r.RunConfig(), docker=None, docker_ok=False, uv=None, gpu=True)
    err = _setup_failure(r.case_docker_start(ctx))
    assert err["failure_type"] == "docker_cli_missing"
    assert "[fix:" in err["message"]
    assert ctx.skipped == {}


def test_docker_cases_with_daemon_down(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(),
        docker="/fake/docker",
        docker_ok=False,
        uv=None,
        gpu=True,
    )
    for fn in (
        r.case_docker_start,
        r.case_docker_pull_cold,
        r.case_docker_gpu_smi,
        r.case_docker_gpu_torch,
    ):
        err = _setup_failure(fn(ctx))
        assert err["failure_type"] == "docker_daemon_unreachable"
        assert err["message"].endswith("[fix: start it]")


def test_gpu_cases_gates(tmp_path: Path) -> None:
    no_gpu = _ctx(
        tmp_path,
        r.RunConfig(),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=False,
    )
    err = _setup_failure(r.case_docker_gpu_smi(no_gpu))
    assert err["failure_type"] == "no_gpu_on_host" and "[fix: driver]" in err["message"]
    skip = _ctx(
        tmp_path,
        r.RunConfig(skip_gpu=True),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    assert _setup_failure(r.case_docker_gpu_smi(skip))["message"] == "--skip-gpu"
    assert _setup_failure(r.case_docker_gpu_torch(skip))["message"] == "--skip-gpu"
    torch_skip = _ctx(
        tmp_path,
        r.RunConfig(skip_gpu_torch=True),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    assert (
        _setup_failure(r.case_docker_gpu_torch(torch_skip))["message"]
        == "--skip-gpu-torch"
    )
    quick = _ctx(
        tmp_path,
        r.RunConfig(quick=True),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    assert _setup_failure(r.case_docker_gpu_torch(quick))["message"] == "--quick"


def test_pull_cold_skipped_without_network(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(no_network=True),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    assert _setup_failure(r.case_docker_pull_cold(ctx))["message"] == "--no-network"


def test_gpu_torch_absent_image_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(no_network=True),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    monkeypatch.setattr(r.docker, "image_present", lambda *_a, **_k: False)
    err = _setup_failure(r.case_docker_gpu_torch(ctx))
    assert (
        err["failure_type"] == "image_pull_failed" and "--no-network" in err["message"]
    )


def test_gpu_torch_refuses_pull_without_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(),
        docker="/fake/docker",
        docker_ok=True,
        uv=None,
        gpu=True,
    )
    monkeypatch.setattr(r.docker, "image_present", lambda *_a, **_k: False)
    monkeypatch.setattr(r.docker, "compressed_size", lambda *_a, **_k: 3_700_000_000)
    pulled: list[str] = []
    monkeypatch.setattr(r.docker, "pull_image", lambda *_a, **_k: pulled.append("x"))
    err = _setup_failure(r.case_docker_gpu_torch(ctx))
    assert err["failure_type"] == "insufficient_disk"
    assert "11.1 GB needed" in err["message"] and "--skip-gpu-torch" in err["message"]
    assert pulled == []


def test_venv_cases_without_uv(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path, r.RunConfig(), docker=None, docker_ok=False, uv=None, gpu=False
    )
    for fn in (r.case_venv_cold, r.case_venv_warm, r.case_venv_start):
        assert _setup_failure(fn(ctx))["failure_type"] == "uv_cli_missing"


def test_venv_cases_without_network_are_skipped(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        r.RunConfig(no_network=True),
        docker=None,
        docker_ok=False,
        uv="/fake/uv",
        gpu=False,
    )
    for fn in (r.case_venv_cold, r.case_venv_warm, r.case_venv_start):
        assert _setup_failure(fn(ctx))["failure_type"] == r.SKIPPED
    assert set(ctx.skipped) == {"venv-cold", "venv-warm", "venv-start"}


def test_venv_start_without_warm_venv(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path, r.RunConfig(), docker=None, docker_ok=False, uv="/fake/uv", gpu=False
    )
    assert _setup_failure(r.case_venv_start(ctx))["failure_type"] == "venv_unavailable"


def test_venv_warm_setup_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(
        tmp_path, r.RunConfig(), docker=None, docker_ok=False, uv="/fake/uv", gpu=False
    )

    def boom(*_a: Any, **_k: Any) -> float:
        raise venv.VenvError("network_error", "no route", fix="check the network")

    monkeypatch.setattr(r.venv, "create_venv", boom)
    err = _setup_failure(r.case_venv_warm(ctx))
    assert (
        err["failure_type"] == "network_error"
        and "[fix: check the network]" in err["message"]
    )
    assert ctx.warm_venv is None


def test_run_environments_with_everything_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No docker, no uv, no GPU: every case is a typed failure, the envelope is
    valid, the temp dir is gone, and the run is incomplete (exit 3)."""
    monkeypatch.setattr(r.docker, "find_docker", lambda: None)
    monkeypatch.setattr(r.venv, "find_uv", lambda: None)
    monkeypatch.setattr(r.facts.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        r.tempfile,
        "mkdtemp",
        lambda prefix: (
            str(tmp_path / "s6-env-x")
            if (tmp_path / "s6-env-x").mkdir() is None
            else ""
        ),
    )
    out = r.run_environments(r.RunConfig(warmup=0, reps=1))
    doc = validate(out.envelope)
    assert not out.complete
    assert not (tmp_path / "s6-env-x").exists()
    assert set(doc["cases"]) == set(r.CASE_IDS)
    types = {c: doc["cases"][c]["errors"][0]["failure_type"] for c in r.CASE_IDS}
    assert types["docker-start"] == "docker_cli_missing"
    assert types["docker-gpu-smi"] == "docker_cli_missing"
    assert types["venv-cold"] == "uv_cli_missing"
    assert doc["extra"]["docker"]["failure_type"] == "docker_cli_missing"
    assert doc["extra"]["facts"]["gpu"]["present"] is False
    assert any(e["failure_type"] == "docker_cli_missing" for e in doc["errors"])
    assert doc["link"] is None and doc["params"]["reps"] == 1
    assert doc["extra"]["skipped"] == {}
    assert doc["extra"]["pinned_requirements"] == list(venv.PINNED_REQUIREMENTS)


def test_run_environments_all_skipped_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator skips do not count as failures: exit 0 with an honest JSON."""
    monkeypatch.setattr(r.venv, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(r.venv, "uv_version", lambda _uv: "uv fake")
    monkeypatch.setattr(r.facts.shutil, "which", lambda _n: None)
    out = r.run_environments(r.RunConfig(skip_docker=True, no_network=True))
    doc = validate(out.envelope)
    assert out.complete
    assert set(doc["extra"]["skipped"]) == set(r.CASE_IDS)
    assert doc["errors"] == []
    assert doc["extra"]["docker"]["failure_type"] == r.SKIPPED
