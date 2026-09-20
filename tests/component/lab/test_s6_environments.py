"""Component tests for lab/spikes/s6_environments.py (0.2.10): real ``uv``
environments in a private cache, real ``docker run`` of the small image when a
daemon is reachable (skipped with the reason otherwise), the script end to
end in ``--quick`` mode, and the cleanup guarantees (no container, no temp
dir left; a timed-out or interrupted child is killed).

Never pulls the PyTorch image. Asserts behaviour, not this machine's state:
no test assumes docker exists, a GPU exists, or a particular hardware name.
"""

from __future__ import annotations

import _thread
import json
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
SCRIPT: Final = LAB_DIR / "spikes" / "s6_environments.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._harness.envelope import validate  # noqa: E402
from spikes._s6 import docker, proc, venv  # noqa: E402
from spikes._s6.run import CASE_IDS  # noqa: E402

pytestmark = pytest.mark.timeout(600)
EXIT_OK: Final = 0
EXIT_INCOMPLETE: Final = 3
TINY_REQUIREMENT: Final = "six==1.17.0"


def _run_script(*args: str, timeout: float = 540) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _leftover_temp_dirs() -> list[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).glob("s6-env-*"))


def _load_result(out_dir: Path) -> dict[str, Any]:
    files = list(out_dir.glob("*.json"))
    assert len(files) == 1, files
    return validate(json.loads(files[0].read_text(encoding="utf-8")))


def _docker_cli() -> str:
    cli = docker.find_docker()
    if cli is None:
        pytest.skip("docker CLI not on PATH")
    facts = docker.docker_facts(cli)
    if not facts.get("ok"):
        pytest.skip(f"docker daemon not reachable: {facts.get('failure_type')}")
    return cli


def _small_image_or_skip(cli: str) -> None:
    if not docker.image_present(cli, docker.SMALL_IMAGE):
        pytest.skip(f"{docker.SMALL_IMAGE} not present locally (tests never pull)")


@pytest.fixture
def uv_cli() -> str:
    uv = venv.find_uv()
    if uv is None:
        pytest.skip("uv not on PATH")
    return uv


@pytest.fixture
def no_leftovers() -> Iterator[None]:
    before = _leftover_temp_dirs()
    yield
    assert _leftover_temp_dirs() == before


# ------------------------------------------------------------------ uv venv
def test_real_uv_venv_cold_then_warm_offline(uv_cli: str, tmp_path: Path) -> None:
    """A tiny pinned package: cold install fills the private cache, a second
    venv installs from it ``--offline``; sizes are measured, dirs removed."""
    requirements = tmp_path / "req.txt"
    requirements.write_text(TINY_REQUIREMENT + "\n", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    cold = tmp_path / "cold"
    try:
        create_s = venv.create_venv(uv_cli, cold, cache_dir=cache, timeout=120)
    except venv.VenvError as exc:
        if exc.failure_type == "python_not_found":
            pytest.skip(f"no Python {venv.PYTHON_REQUEST} for uv: {exc}")
        raise
    assert create_s > 0
    assert venv.venv_python(cold).exists()
    try:
        install_s = venv.install(
            uv_cli, cold, requirements, cache_dir=cache, offline=False, timeout=300
        )
    except venv.VenvError as exc:
        if exc.failure_type == "network_error":
            pytest.skip(f"no network for the cold install: {exc}")
        raise
    assert install_s > 0
    sp = venv.site_packages(cold)
    assert sp is not None and (sp / "six.py").exists()
    assert (venv.dir_bytes(sp) or 0) > 0
    assert (venv.dir_bytes(cache) or 0) > 0
    warm = tmp_path / "warm"
    venv.create_venv(uv_cli, warm, cache_dir=cache, timeout=120)
    warm_s = venv.install(
        uv_cli, warm, requirements, cache_dir=cache, offline=True, timeout=300
    )
    assert warm_s > 0
    out = proc.run(
        [str(venv.venv_python(warm)), "-c", "import six; print('ok')"], timeout=60
    )
    assert out.ok and out.stdout.strip() == "ok"
    venv.rmtree(cold)
    venv.rmtree(warm)
    venv.rmtree(cache)
    assert not cold.exists() and not warm.exists() and not cache.exists()


def test_real_uv_offline_install_with_empty_cache_is_typed(
    uv_cli: str, tmp_path: Path
) -> None:
    requirements = tmy = tmp_path / "req.txt"
    tmy.write_text(TINY_REQUIREMENT + "\n", encoding="utf-8")
    cache = tmp_path / "empty-cache"
    cache.mkdir()
    target = tmp_path / "v"
    try:
        venv.create_venv(uv_cli, target, cache_dir=cache, timeout=120)
    except venv.VenvError as exc:
        pytest.skip(f"cannot create a venv here: {exc}")
    with pytest.raises(venv.VenvError) as exc_info:
        venv.install(
            uv_cli, target, requirements, cache_dir=cache, offline=True, timeout=120
        )
    assert exc_info.value.failure_type in {"network_error", "uv_failed"}
    assert exc_info.value.fix


# --------------------------------------------------------------------- proc
def test_proc_timeout_kills_the_child() -> None:
    marker = "s6-proc-timeout-marker"
    out = proc.run(
        [sys.executable, "-c", f"import time; {marker!r}; time.sleep(30)"], timeout=1.0
    )
    assert out.timed_out and not out.ok
    assert 0.9 <= out.elapsed_s <= 10.0
    assert "timed out" in out.describe()
    assert not _python_children_with(marker)


def test_proc_keyboard_interrupt_kills_the_child_and_propagates() -> None:
    marker = "s6-proc-interrupt-marker"
    timer = threading.Timer(0.5, _thread.interrupt_main)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            proc.run(
                [sys.executable, "-c", f"import time; {marker!r}; time.sleep(30)"],
                timeout=30,
            )
    finally:
        timer.cancel()
    assert not _python_children_with(marker)


def test_proc_missing_executable_is_an_error_not_an_exception(tmp_path: Path) -> None:
    out = proc.run([str(tmp_path / "does-not-exist"), "x"], timeout=5)
    assert out.error and not out.ok and out.returncode is None
    assert proc.run(["x"], timeout=0).error == "no time left"


def test_proc_output_is_capped() -> None:
    out = proc.run([sys.executable, "-c", "print('x' * 200000)"], timeout=30)
    assert out.ok
    assert len(out.stdout) < proc.MAX_OUTPUT_CHARS
    assert "[truncated]" in out.stdout


def _python_children_with(marker: str) -> set[int]:
    """Linux: PIDs of live processes whose command line holds ``marker``
    (zombies excluded). Elsewhere an empty set, so the check is skipped."""
    root = Path("/proc")
    if not root.is_dir():
        return set()
    found: set[int] = set()
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if marker.encode() in cmdline and stat.rpartition(")")[2].split()[0] != "Z":
            found.add(int(entry.name))
    return found


# ------------------------------------------------------------------- docker
@pytest.mark.docker
def test_real_sandboxed_container_start() -> None:
    cli = _docker_cli()
    _small_image_or_skip(cli)
    run = docker.run_container(
        cli,
        docker.SMALL_IMAGE,
        ["python", "-c", "print('ok')"],
        timeout=120,
        sandbox=True,
    )
    if not run.outcome.ok:
        err = docker.classify(run.outcome, image=docker.SMALL_IMAGE)
        assert err.failure_type != "container_failed", (
            err
        )  # a typed refusal is a finding
        pytest.skip(f"host refuses the sandbox: {err.failure_type}: {err}")
    assert run.outcome.stdout.strip() == "ok"
    assert run.elapsed_ms > 0
    assert run.name not in docker.list_own_containers(cli)


@pytest.mark.docker
def test_real_container_timeout_is_killed_and_removed() -> None:
    cli = _docker_cli()
    _small_image_or_skip(cli)
    run = docker.run_container(cli, docker.SMALL_IMAGE, ["sleep", "60"], timeout=3)
    assert run.outcome.timed_out
    assert docker.classify(run.outcome).failure_type == "timeout"
    deadline = time.monotonic() + 30
    while run.name in docker.list_own_containers(cli) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert run.name not in docker.list_own_containers(cli)


@pytest.mark.docker
def test_real_detached_leftover_is_swept_by_name(tmp_path: Path) -> None:
    """A container this tool named but did not remove (e.g. after a hard kill
    of the Python process) is found and removed by the final sweep."""
    cli = _docker_cli()
    _small_image_or_skip(cli)
    name = docker.new_container_name()
    out = proc.run(
        [cli, "run", "-d", "--name", name, docker.SMALL_IMAGE, "sleep", "60"],
        timeout=120,
    )
    assert out.ok, out.describe()
    try:
        assert name in docker.list_own_containers(cli)
    finally:
        docker.force_remove(cli, name)
    assert name not in docker.list_own_containers(cli)


@pytest.mark.docker
def test_real_image_facts_and_unknown_image() -> None:
    cli = _docker_cli()
    _small_image_or_skip(cli)
    info = docker.image_facts(cli, docker.SMALL_IMAGE)
    assert info["image"] == docker.SMALL_IMAGE
    assert isinstance(info["size_bytes"], int) and info["size_bytes"] > 0
    assert info["digest"] is None or "@sha256:" in info["digest"]
    assert docker.image_facts(cli, "s6-no-such-image:none")["size_bytes"] is None
    assert not docker.image_present(cli, "s6-no-such-image:none")


@pytest.mark.docker
def test_real_docker_facts_are_typed_and_sanitised() -> None:
    cli = _docker_cli()
    facts = docker.docker_facts(cli)
    assert facts["ok"] is True
    assert facts["server_version"] and facts["client_version"]
    assert isinstance(facts["runtimes"], list)
    assert isinstance(facts["nvidia_runtime"], bool)
    assert facts["os_type"] in ("linux", "windows")


# ---------------------------------------------------------- script, quick
def test_script_quick_venv_only(tmp_path: Path, no_leftovers: None) -> None:
    """``--skip-docker``: real uv cases only; everything else is skipped by
    flag. No temp dir survives; every case id is present in the JSON."""
    uv = venv.find_uv()
    if uv is None:
        pytest.skip("uv not on PATH")
    out_dir = tmp_path / "out"
    result = _run_script(
        "run", "--quick", "--skip-docker", "--warmup", "0", "--out-dir", str(out_dir)
    )
    doc = _load_result(out_dir)
    assert set(doc["cases"]) == set(CASE_IDS)
    assert doc["extra"]["quick"] is True
    assert set(doc["extra"]["skipped"]) >= {
        "docker-start",
        "docker-pull-cold",
        "docker-gpu-smi",
        "docker-gpu-torch",
    }
    assert (
        "QUICK MODE" in result.stderr and "every row will be INVALID" in result.stderr
    )
    cold = doc["cases"]["venv-cold"]
    if cold["errors"] and cold["errors"][0]["failure_type"] in {
        "network_error",
        "python_not_found",
    }:
        assert result.returncode == EXIT_INCOMPLETE
        pytest.skip(f"uv cases could not run here: {cold['errors'][0]['message']}")
    assert result.returncode == EXIT_OK, result.stderr
    for case_id in ("venv-cold", "venv-warm", "venv-start"):
        case = doc["cases"][case_id]
        assert len(case["samples"]) == 2, (case_id, case["errors"])
        assert case["warmup"] == []
    assert doc["cases"]["venv-warm"]["attrs"]["offline"] is True
    assert (
        doc["cases"]["venv-warm"]["summary"]["install_s"]["mean"]
        < doc["cases"]["venv-cold"]["summary"]["install_s"]["mean"]
    )
    assert doc["cases"]["venv-start"]["summary"]["start_to_exit_ms"]["n"] == 2
    assert "INVALID (n=2)" in result.stderr
    assert "container TBD" in result.stderr and "venv TBD" in result.stderr


@pytest.mark.docker
def test_script_quick_docker_no_network(tmp_path: Path, no_leftovers: None) -> None:
    """Docker cases without any download: docker-start runs for real (image
    present), the pull case is skipped, GPU cases are gated, no container is
    left with the tool's name prefix."""
    cli = _docker_cli()
    _small_image_or_skip(cli)
    out_dir = tmp_path / "out"
    result = _run_script(
        "run",
        "--quick",
        "--no-network",
        "--skip-gpu",
        "--warmup",
        "0",
        "--reps",
        "1",
        "--out-dir",
        str(out_dir),
        "--name",
        "s6test",
    )
    doc = _load_result(out_dir)
    assert doc["host"]["label"] == "s6test"
    start = doc["cases"]["docker-start"]
    if start["errors"]:
        failure = start["errors"][0]["failure_type"]
        assert failure in {
            "sandbox_flag_refused",
            "timeout",
            "docker_daemon_unreachable",
            "docker_permission_denied",
        }
        assert result.returncode == EXIT_INCOMPLETE
        pytest.skip(f"docker-start refused here: {failure}")
    assert result.returncode == EXIT_OK, result.stderr
    assert len(start["samples"]) == 1 and start["samples"][0]["exit_code"] == 0
    assert start["attrs"]["image"]["image"] == docker.SMALL_IMAGE
    assert start["attrs"]["sandbox_flags"] == list(docker.SANDBOX_FLAGS)
    assert doc["extra"]["skipped"]["docker-pull-cold"] == "--no-network"
    assert doc["extra"]["skipped"]["docker-gpu-smi"] == "--skip-gpu"
    assert doc["extra"]["docker"]["ok"] is True
    assert "docker: INVALID (n<5)" in result.stderr
    assert docker.list_own_containers(cli) == []


def test_script_report_from_quick_result(tmp_path: Path) -> None:
    uv = venv.find_uv()
    if uv is None:
        pytest.skip("uv not on PATH")
    results = tmp_path / "2026-09-20-S6-environments"
    _run_script(
        "run",
        "--quick",
        "--skip-docker",
        "--no-network",
        "--warmup",
        "0",
        "--out-dir",
        str(results),
    )
    out = tmp_path / "report.md"
    result = _run_script(
        "report", "--results", str(results), "--out", str(out), "--labels", "L9=nobody"
    )
    assert result.returncode == EXIT_OK, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "### Environment verdicts per laptop" in text
    assert "(quick run)" in text
    assert "| L9 | ? | TBD — no result file |" in text
    assert "skipped" in text
