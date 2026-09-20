"""Unit tests for ``spikes._s6.docker`` (0.2.10): parsing of ``docker info`` /
``docker version`` output (Linux Engine and Docker Desktop shapes), the typed
classification of real ``docker`` error strings, and the container helpers.

Nothing here talks to a daemon.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from spikes._s6 import docker, proc
from spikes._s6.proc import Outcome

# ---------------------------------------------------------------- parse_info


def test_parse_info_docker_desktop_wsl2(fixture_text: Callable[[str], str]) -> None:
    facts = docker.parse_info(fixture_text("docker/info_desktop_wsl2.json"))
    assert facts["parsed"] is True
    assert facts["server_version"] == "28.3.2"
    assert facts["operating_system"] == "Docker Desktop"
    assert facts["docker_desktop"] is True
    assert facts["wsl2_backend"] is True
    assert facts["os_type"] == "linux"
    # the privacy sanitizer masks the 4-part kernel number as if it were an IP
    assert str(facts["kernel_version"]).endswith("-microsoft-standard-WSL2")
    # Docker Desktop has no nvidia runtime key even when --gpus works
    assert facts["runtimes"] == ["io.containerd.runc.v2", "runc"]
    assert facts["nvidia_runtime"] is False
    assert facts["cgroup_version"] == "2"
    assert facts["driver"] == "overlayfs"
    assert facts["security_options"] == [
        "name=seccomp,profile=builtin",
        "name=cgroupns",
    ]
    assert facts["server_errors"] == []
    assert facts["ncpu"] == 16


def test_parse_info_linux_engine_with_nvidia(
    fixture_text: Callable[[str], str],
) -> None:
    facts = docker.parse_info(fixture_text("docker/info_linux_nvidia.json"))
    assert facts["docker_desktop"] is False
    assert facts["wsl2_backend"] is False
    assert facts["nvidia_runtime"] is True
    assert facts["runtimes"] == ["io.containerd.runc.v2", "nvidia", "runc"]
    assert facts["cgroup_driver"] == "systemd"
    assert facts["docker_root_dir"] == "/var/lib/docker"
    assert facts["mem_total_bytes"] == 16413962240


def test_parse_info_daemon_down_reports_server_errors(
    fixture_text: Callable[[str], str],
) -> None:
    facts = docker.parse_info(fixture_text("docker/info_daemon_down.json"))
    assert facts["parsed"] is True
    assert facts["server_version"] is None
    assert facts["runtimes"] == []
    assert len(facts["server_errors"]) == 1
    assert "dockerDesktopLinuxEngine" in facts["server_errors"][0]


@pytest.mark.parametrize("text", ["not json", "[1, 2]", "42", "{"])
def test_parse_info_garbage_never_raises(text: str) -> None:
    facts = docker.parse_info(text)
    assert facts["parsed"] is False
    assert facts["server_errors"]


def test_parse_info_empty_output_is_no_server() -> None:
    facts = docker.parse_info("")
    assert facts["parsed"] is True
    assert facts["server_version"] is None


def test_parse_info_wrong_types_are_ignored() -> None:
    facts = docker.parse_info(
        '{"ServerVersion": 12, "Runtimes": ["nvidia"], "SecurityOptions": "x", '
        '"KernelVersion": null, "NCPU": "16", "ServerErrors": [1, "boom"]}'
    )
    assert facts["server_version"] is None
    assert facts["runtimes"] == []
    assert facts["nvidia_runtime"] is False
    assert facts["security_options"] == []
    assert facts["ncpu"] is None
    assert facts["server_errors"] == ["boom"]


def test_parse_version(fixture_text: Callable[[str], str]) -> None:
    ok = docker.parse_version(fixture_text("docker/version_ok.json"))
    assert ok == {
        "client_version": "29.7.2",
        "server_version": "29.7.2",
        "client_os_arch": "linux/amd64",
        "server_os_arch": "linux/amd64",
    }
    down = docker.parse_version(fixture_text("docker/version_no_server.json"))
    assert down["client_version"] == "28.3.2"
    assert down["server_version"] is None
    assert down["client_os_arch"] == "windows/amd64"
    assert docker.parse_version("garbage")["client_version"] is None
    assert docker.parse_version("[]")["server_version"] is None


# ------------------------------------------------------------------ classify
def _failed(stderr: str, *, code: int = 125, stdout: str = "") -> Outcome:
    return Outcome(("docker", "run"), code, stdout, stderr, 0.1)


LINUX_NO_TOOLKIT = (
    'docker: Error response from daemon: could not select device driver "" with '
    "capabilities: [[gpu]]\n\nRun 'docker run --help' for more information"
)
WINDOWS_NO_GPU = (
    'docker: Error response from daemon: could not select device driver "" with '
    "capabilities: [[gpu]]."
)
LINUX_DAEMON_DOWN = (
    "docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)
WINDOWS_DAEMON_DOWN = (
    'error during connect: Head "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/_ping": '
    "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
)
LINUX_PERMISSION = (
    "permission denied while trying to connect to the Docker daemon socket at "
    'unix:///var/run/docker.sock: Head "http://%2Fvar%2Frun%2Fdocker.sock/_ping": '
    "dial unix /var/run/docker.sock: connect: permission denied"
)
NVIDIA_CLI_ERROR = (
    "docker: Error response from daemon: failed to create task for container: failed to "
    "create shim task: OCI runtime create failed: runc create failed: unable to start "
    "container process: error during container init: error running prestart hook #0: "
    "exit status 1, stdout: , stderr: Auto-detected mode as 'legacy'\n"
    "nvidia-container-cli: initialization error: nvml error: driver not loaded: unknown"
)
NVML_MISMATCH = "Failed to initialize NVML: Driver/library version mismatch"
PULL_DENIED = (
    "Unable to find image 'pytorch/pytorch:9.9.9-nope' locally\n"
    "docker: Error response from daemon: manifest for pytorch/pytorch:9.9.9-nope not "
    "found: manifest unknown: manifest unknown"
)
PULL_OFFLINE = (
    "Unable to find image 'python:3.12-slim' locally\n"
    'docker: Error response from daemon: Get "https://registry-1.docker.io/v2/": dial '
    "tcp: lookup registry-1.docker.io on 127.0.0.53:53: no such host"
)
PULL_TLS = (
    'docker: Error response from daemon: Get "https://registry-1.docker.io/v2/": '
    "net/http: TLS handshake timeout"
)
UNKNOWN_FLAG = "unknown flag: --pids-limit\n\nUsage:  docker run [OPTIONS] IMAGE [COMMAND] [ARG...]"
INVALID_ARG = (
    'docker: Error response from daemon: invalid argument "--read-only" for the current '
    "storage driver: not supported"
)


@pytest.mark.parametrize(
    ("stderr", "failure_type"),
    [
        (LINUX_NO_TOOLKIT, "no_nvidia_runtime"),
        (WINDOWS_NO_GPU, "no_nvidia_runtime"),
        (
            "docker: Error response from daemon: unknown or invalid runtime name: nvidia",
            "no_nvidia_runtime",
        ),
        (LINUX_DAEMON_DOWN, "docker_daemon_unreachable"),
        (WINDOWS_DAEMON_DOWN, "docker_daemon_unreachable"),
        (LINUX_PERMISSION, "docker_permission_denied"),
        (NVIDIA_CLI_ERROR, "nvidia_driver_error"),
        (NVML_MISMATCH, "nvidia_driver_error"),
        (PULL_DENIED, "image_pull_failed"),
        (PULL_OFFLINE, "image_pull_failed"),
        (PULL_TLS, "image_pull_failed"),
        (UNKNOWN_FLAG, "sandbox_flag_refused"),
        (INVALID_ARG, "sandbox_flag_refused"),
        ("Traceback (most recent call last): ... ValueError", "container_failed"),
        ("", "container_failed"),
    ],
)
def test_classify_real_error_strings(stderr: str, failure_type: str) -> None:
    err = docker.classify(_failed(stderr), image="img")
    assert err.failure_type == failure_type
    assert err.fix  # every typed failure carries a fix hint
    assert len(str(err)) <= 600


def test_classify_names_the_refused_flag() -> None:
    err = docker.classify(_failed(UNKNOWN_FLAG))
    assert err.failure_type == "sandbox_flag_refused"
    assert str(err).startswith("--pids-limit: ")
    assert "--pids-limit" in err.fix
    err2 = docker.classify(_failed(INVALID_ARG))
    assert str(err2).startswith("--read-only: ")


def test_classify_timeout_and_missing_cli() -> None:
    timed_out = Outcome(("docker",), None, "", "", 30.0, timed_out=True)
    assert docker.classify(timed_out).failure_type == "timeout"
    missing = Outcome(
        ("docker",), None, "", "", 0.0, error="could not start: FileNotFoundError"
    )
    assert docker.classify(missing).failure_type == "docker_cli_missing"


def test_classify_pull_failure_names_the_image() -> None:
    err = docker.classify(_failed(PULL_DENIED), image="pytorch/pytorch:9.9.9-nope")
    assert str(err).startswith("pytorch/pytorch:9.9.9-nope: ")


def test_classify_keeps_only_a_short_sanitised_tail() -> None:
    long = "x" * 5000 + "\n" + "/home/someuser/secret/path boom"
    err = docker.classify(_failed(long))
    assert len(str(err)) <= 600
    assert "someuser" not in str(err)


def test_docker_facts_without_cli() -> None:
    facts = docker.docker_facts(None)
    assert facts["ok"] is False
    assert facts["failure_type"] == "docker_cli_missing"
    assert facts["fix"]


def test_docker_facts_daemon_down_from_server_errors(
    monkeypatch: pytest.MonkeyPatch, fixture_text: Callable[[str], str]
) -> None:
    """A reachable CLI with the engine stopped (the common Windows failure)."""
    outputs = {
        "version": fixture_text("docker/version_no_server.json"),
        "info": fixture_text("docker/info_daemon_down.json"),
    }

    def fake_run(
        argv: list[str], *, timeout: float, env: object = None, cwd: object = None
    ) -> Outcome:
        return Outcome(tuple(argv), 0, outputs[argv[1]], "", 0.01)

    monkeypatch.setattr(proc, "run", fake_run)
    facts = docker.docker_facts("/fake/docker")
    assert facts["ok"] is False
    assert facts["failure_type"] == "docker_daemon_unreachable"
    assert facts["client_version"] == "28.3.2"
    assert "dockerDesktopLinuxEngine" in facts["message"]
    assert "Docker Desktop" in facts["fix"] or "daemon" in facts["fix"]


def test_docker_facts_ok(
    monkeypatch: pytest.MonkeyPatch, fixture_text: Callable[[str], str]
) -> None:
    outputs = {
        "version": fixture_text("docker/version_ok.json"),
        "info": fixture_text("docker/info_linux_nvidia.json"),
    }

    def fake_run(
        argv: list[str], *, timeout: float, env: object = None, cwd: object = None
    ) -> Outcome:
        return Outcome(tuple(argv), 0, outputs[argv[1]], "", 0.01)

    monkeypatch.setattr(proc, "run", fake_run)
    facts = docker.docker_facts("/fake/docker")
    assert facts["ok"] is True
    assert facts["nvidia_runtime"] is True
    assert "failure_type" not in facts


def test_docker_facts_info_exit_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        argv: list[str], *, timeout: float, env: object = None, cwd: object = None
    ) -> Outcome:
        return Outcome(tuple(argv), 1, "", LINUX_DAEMON_DOWN, 0.01)

    monkeypatch.setattr(proc, "run", fake_run)
    facts = docker.docker_facts("/fake/docker")
    assert facts["ok"] is False
    assert facts["failure_type"] == "docker_daemon_unreachable"


# ------------------------------------------------------------- small parsers
def test_parse_smi_row() -> None:
    row = docker.parse_smi_row(
        "NVIDIA GeForce RTX 3050 Laptop GPU, 595.91.07, 4096 MiB\n"
    )
    assert row == {
        "gpu_name": "NVIDIA GeForce RTX 3050 Laptop GPU",
        "driver_version": "595.91.07",
        "memory_total": "4096 MiB",
    }
    assert docker.parse_smi_row("")["gpu_name"] is None
    assert docker.parse_smi_row("only, two")["gpu_name"] is None


def test_parse_torch_line_takes_the_last_json_object() -> None:
    out = 'some warning\n{"torch_version": "2.8.0", "cuda_available": true}\n'
    assert docker.parse_torch_line(out) == {
        "torch_version": "2.8.0",
        "cuda_available": True,
    }
    assert docker.parse_torch_line("nothing here") is None
    assert docker.parse_torch_line("{not json") is None
    assert docker.parse_torch_line("[1,2]") is None


def test_parse_manifest_size() -> None:
    text = '{"layers": [{"size": 100}, {"size": 250}], "config": {"size": 5}}'
    assert docker.parse_manifest_size(text) == 350
    assert docker.parse_manifest_size('{"manifests": [{"size": 1}]}') is None
    assert docker.parse_manifest_size('{"layers": [{"size": "x"}]}') is None
    assert docker.parse_manifest_size("garbage") is None


def test_torch_script_is_self_contained() -> None:
    """The in-container script must compile and only use stdlib + torch."""
    compile(docker.TORCH_SCRIPT, "<torch-script>", "exec")
    assert "torch.cuda.synchronize()" in docker.TORCH_SCRIPT
    assert "1024" in docker.TORCH_SCRIPT
    assert "sys.exit(0 if ok else 3)" in docker.TORCH_SCRIPT


def test_sandbox_flags_match_adr_0004() -> None:
    flags = docker.SANDBOX_FLAGS
    for flag in (
        "--network",
        "--cpus",
        "--memory",
        "--pids-limit",
        "--user",
        "--read-only",
        "--tmpfs",
    ):
        assert flag in flags
    assert flags[flags.index("--network") + 1] == "none"
    assert flags[flags.index("--user") + 1] == "65534:65534"


def test_container_names_are_unique_and_prefixed() -> None:
    names = {docker.new_container_name() for _ in range(50)}
    assert len(names) == 50
    assert all(n.startswith(docker.CONTAINER_NAME_PREFIX) for n in names)


def test_run_container_force_removes_even_when_the_client_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(
        argv: list[str], *, timeout: float, env: object = None, cwd: object = None
    ) -> Outcome:
        calls.append(tuple(argv))
        if argv[1] == "run":
            return Outcome(tuple(argv), None, "", "", timeout, timed_out=True)
        return Outcome(tuple(argv), 0, "", "", 0.01)

    monkeypatch.setattr(proc, "run", fake_run)
    run = docker.run_container(
        "/fake/docker", "img", ["true"], timeout=5, sandbox=True, gpus=True
    )
    assert run.outcome.timed_out
    assert calls[0][1] == "run" and "--rm" in calls[0] and "--gpus" in calls[0]
    assert all(flag in calls[0] for flag in docker.SANDBOX_FLAGS)
    assert calls[-1][1:3] == ("rm", "-f") and calls[-1][3] == run.name


def test_run_container_collects_daemon_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        argv: list[str], *, timeout: float, env: object = None, cwd: object = None
    ) -> Outcome:
        if argv[1] == "run":
            return Outcome(
                tuple(argv),
                0,
                "ok\n",
                "WARNING: Your kernel does not support memory limit capabilities or "
                "the cgroup is not mounted.\n",
                0.2,
            )
        return Outcome(tuple(argv), 0, "", "", 0.01)

    monkeypatch.setattr(proc, "run", fake_run)
    run = docker.run_container("/fake/docker", "img", ["true"], timeout=5)
    assert run.outcome.ok
    assert run.warnings and run.warnings[0].startswith("WARNING: Your kernel")
    assert 150 < run.elapsed_ms < 250
