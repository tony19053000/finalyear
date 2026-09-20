"""Tester pass (0.2.10 follow-up): failure-injection attacks on
``spikes._s6.run`` beyond the implementer's own ``test_s6_run.py``.

Focus:
  * a ``KeyboardInterrupt`` raised *inside* a case (not just inside
    ``proc.run``) must still hit ``run_environments``'s ``finally: _cleanup``
    - the run's temp directory must be gone even though the exception
      propagates all the way past ``write_envelope`` (which never runs, so no
      result is written - matching the documented Ctrl-C-writes-no-result
      contract from ``s6_environments.py``'s own docstring / ``S6.md``);
  * ``docker-pull-cold``'s ``finally`` re-pull is attempted even when the
    measured pull raised a fatal error mid-repetition, so the image is never
    left absent because a repetition happened to fail.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from spikes._s6 import docker, proc, venv
from spikes._s6 import run as r


def test_keyboard_interrupt_inside_a_case_still_cleans_up_the_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``RepError`` is caught by ``run_reps``; anything else (including
    ``KeyboardInterrupt``) must propagate through ``run_environments`` - but
    its ``finally: leftovers = _cleanup(ctx)`` must still run first."""
    monkeypatch.setattr(docker, "find_docker", lambda: None)
    monkeypatch.setattr(venv, "find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(venv, "uv_version", lambda _uv: "uv fake")
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    created: dict[str, Path] = {}

    def fake_mkdtemp(prefix: str) -> str:
        target = tmp_path / "s6-env-interrupt"
        target.mkdir()
        created["dir"] = target
        return str(target)

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise KeyboardInterrupt

    # venv-cold is the first case reached once docker is fully skipped.
    monkeypatch.setattr(venv, "create_venv", boom)

    with pytest.raises(KeyboardInterrupt):
        r.run_environments(r.RunConfig(skip_docker=True, warmup=0, reps=1))

    assert "dir" in created, "mkdtemp was never called"
    assert not created["dir"].exists(), (
        "run_environments must remove its temp dir in `finally` even when a "
        "case raises KeyboardInterrupt, not only on a clean return"
    )


def test_docker_pull_cold_finally_repulls_even_after_a_fatal_measured_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``pull_image`` fails fatally during the timed repetition, the image
    must still be left on the machine afterwards (the ``finally`` re-pull is
    unconditional on the measured outcome, not just on success)."""
    cfg = r.RunConfig(warmup=0, reps=1)
    dfacts: dict[str, Any] = {"ok": True}
    host: dict[str, Any] = {
        "gpu": {"present": False, "message": "no smi", "fix": "driver"},
        "disk_free_bytes": {"temp_dir": 1_000_000, "docker_root": None},
        "uv_version": "uv x",
    }
    ctx = r._Context(
        cfg=cfg,
        budget=r.Budget(cfg.effective_budget()),
        temp_dir=tmp_path,
        log=lambda _m: None,
        docker="/fake/docker",
        docker_facts=dfacts,
        uv=None,
        host=host,
    )

    # The image is never reported as present: measure() never enters the
    # "remove then pull" branch, only the plain pull; this isolates the
    # `finally` re-pull from the "was it present before rmi" check.
    monkeypatch.setattr(docker, "image_present", lambda *_a, **_k: False)

    pull_calls: list[str] = []

    def fake_pull(_docker: str, image: str, *, timeout: float) -> Any:
        pull_calls.append(image)
        # the measured repetition's pull fails fatally (manifest unknown).
        return proc.Outcome((), 1, "", "manifest unknown", 0)

    monkeypatch.setattr(docker, "pull_image", fake_pull)

    result = r.case_docker_pull_cold(ctx)

    assert result.errors, "the fatal pull failure must be recorded"
    # One call from the measured (failing) repetition, and a second,
    # independent call from the unconditional `finally` re-pull.
    assert len(pull_calls) == 2, (
        f"expected the measured pull plus one `finally` re-pull, got {pull_calls!r} "
        "- the cleanup re-pull must run even after a fatal measured failure"
    )
