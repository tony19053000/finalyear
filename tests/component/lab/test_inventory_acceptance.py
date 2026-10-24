"""Independent acceptance / failure-injection tests for lab/inventory.py (0.2.2).

Derived from the roadmap success criteria, not from reading the implementation:

1. one command, after a plain ``uv sync``, produces valid JSON with every LAB.md
   §1 machine field (or an explicit error);
2. never crashes on a missing tool/driver/permission/timeout; exit 0 on probe
   failure, non-zero only for usage errors / unwritable output;
3. no secrets or personal data (SSIDs, usernames, public IPs, env vars);
4. covered by tests;
5. never needs admin; no shell; torch/NVML probing cannot hang the run.

Runs the real script as a subprocess (same interpreter as the test run), with
real fake executables placed on ``PATH`` -- no monkeypatching of the script's
internals. Slow/adversarial timing and fresh-clone tests are marked ``slow``.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
SCRIPT: Final = REPO_ROOT / "lab" / "inventory.py"

pytestmark = pytest.mark.timeout(120)


def _run(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 90,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _base_env(extra_path_dirs: list[str] | None = None) -> dict[str, str]:
    """A minimal but real env: current interpreter + a real PATH (optionally with
    extra directories prepended so fake tools shadow the real ones)."""
    env = dict(os.environ)
    if extra_path_dirs:
        env["PATH"] = os.pathsep.join([*extra_path_dirs, env.get("PATH", "")])
    return env


def _write_fake_tool(directory: Path, name: str, body: str) -> Path:
    """A tiny executable shell script named ``name`` on a fake PATH directory."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --------------------------------------------------------------------- usage / IO errors


def test_out_path_that_is_an_existing_directory_exits_1(tmp_path: Path) -> None:
    """``--out`` pointing at an existing directory (not a file) must be a clean
    write failure (exit 1), never an unhandled traceback."""
    target_dir = tmp_path / "already-a-directory"
    target_dir.mkdir()
    proc = _run("--out", str(target_dir), "--no-summary")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    assert "cannot write" in proc.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permission bits, non-root (root ignores permission bits)",
)
def test_readonly_parent_directory_exits_1(tmp_path: Path) -> None:
    """A parent directory that cannot be written to (no write permission, e.g. a
    misconfigured lab share) must fail cleanly with exit 1, not crash."""
    readonly_parent = tmp_path / "readonly"
    readonly_parent.mkdir()
    readonly_parent.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        proc = _run("--out", str(readonly_parent / "sub" / "inv.json"), "--no-summary")
        assert proc.returncode == 1, (proc.stdout, proc.stderr)
        assert "Traceback" not in proc.stderr
    finally:
        readonly_parent.chmod(stat.S_IRWXU)


def test_multiple_bad_flags_all_exit_2(tmp_path: Path) -> None:
    for args in (
        ["--out"],  # missing value
        ["--pretty=yes"],  # store_true flag does not take a value
        ["positional-garbage"],
        ["--out", str(tmp_path / "a"), "--out", str(tmp_path / "b"), "--bogus"],
    ):
        proc = _run(*args)
        assert proc.returncode == 2, (args, proc.stdout, proc.stderr)
        assert "Traceback" not in proc.stderr


def test_empty_argv_writes_to_stdout_and_exits_0() -> None:
    proc = _run()
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["schema_version"] == 1


# --------------------------------------------------------------------- hostile PATH tools


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    d = tmp_path / "fakebin"
    d.mkdir()
    return d


def _write_broken_nvml(directory: Path) -> Path:
    """A stub ``pynvml`` whose ``nvmlInit()`` fails like an unloaded driver.

    The NVML probe runs ``sys.executable -c <script>`` in a child process, so it
    cannot be monkeypatched from here; shadowing the module through
    ``PYTHONPATH`` is the equivalent seam for a subprocess test. It makes the
    *simulated* broken-driver machine independent of whether this host has a
    working driver (or ``pynvml``) installed."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pynvml.py").write_text(
        "class NVMLError(Exception):\n    pass\n\n\n"
        "def nvmlInit():\n"
        '    raise NVMLError("NVML_ERROR_DRIVER_NOT_LOADED: Driver Not Loaded")\n',
        encoding="utf-8",
    )
    return directory


def _broken_driver_env(fake_bin: Path, stub_dir: Path) -> dict[str, str]:
    """Env for a machine whose NVIDIA driver is broken (the state Laptop 1 was in
    while Secure Boot blocked the module): ``nvidia-smi`` is installed but fails,
    and NVML cannot initialise. Nothing here depends on the real host."""
    _write_fake_tool(
        fake_bin,
        "nvidia-smi",
        "echo \"NVIDIA-SMI has failed because it couldn't communicate with the "
        "NVIDIA driver. Make sure that the latest NVIDIA driver is installed "
        'and running." >&2\nexit 9',
    )
    _write_broken_nvml(stub_dir)
    env = _base_env([str(fake_bin)])
    env["PYTHONPATH"] = os.pathsep.join(
        [str(stub_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return env


def test_nvidia_smi_producing_garbage_never_crashes(
    fake_bin: Path, tmp_path: Path
) -> None:
    """A broken/mocked ``nvidia-smi`` on PATH that exits 0 with unrelated garbage
    (not CSV) must be recorded as a failure, and the run must still succeed.

    Host-independent: what nvidia-smi says is fully controlled here, and
    ``driver_working`` is only checked for internal consistency with the two
    probes it is derived from (this host's *real* NVML is deliberately left
    alone, so it may legitimately be working)."""
    _write_fake_tool(fake_bin, "nvidia-smi", 'echo "not a csv line at all, sorry"')
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=_base_env([str(fake_bin)]))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["sections"]["nvidia"]["ok"] is True  # the probe itself never fails
    nv = doc["sections"]["nvidia"]["value"]
    assert nv["nvidia_smi"]["ok"] is False
    assert nv["driver_working"] is bool(nv["nvidia_smi"]["ok"] or nv["nvml"]["ok"])


def test_garbage_nvidia_smi_with_dead_nvml_reports_driver_not_working(
    fake_bin: Path, tmp_path: Path
) -> None:
    """The same garbage nvidia-smi on a machine whose NVML is also dead: both
    probes failed, so ``driver_working`` must be false -- and the run still
    succeeds."""
    env = _broken_driver_env(fake_bin, tmp_path / "stub")
    _write_fake_tool(fake_bin, "nvidia-smi", 'echo "not a csv line at all, sorry"')
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    nv = doc["sections"]["nvidia"]["value"]
    assert nv["nvidia_smi"]["ok"] is False
    assert nv["nvml"]["ok"] is False
    assert nv["driver_working"] is False


def test_nvidia_smi_producing_non_utf8_bytes_never_crashes(
    fake_bin: Path, tmp_path: Path
) -> None:
    """Binary/invalid-UTF-8 stdout from ``nvidia-smi`` (e.g. a corrupted driver
    shim) must decode losslessly-enough to never raise, and be recorded as data."""
    _write_fake_tool(
        fake_bin,
        "nvidia-smi",
        # printf writes raw bytes 0xff 0xfe followed by garbage, exit 0
        "printf '\\377\\376garbage,\\200,data\\n'",
    )
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=_base_env([str(fake_bin)]))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["sections"]["nvidia"]["ok"] is True  # section itself never fails
    assert doc["sections"]["nvidia"]["value"]["nvidia_smi"]["ok"] is False


def test_hanging_nvidia_smi_is_bounded_and_does_not_hang_the_run(
    fake_bin: Path, tmp_path: Path
) -> None:
    """A hung ``nvidia-smi`` (e.g. a wedged driver) must be killed by the
    per-command timeout, not left to hang the whole inventory run indefinitely."""
    _write_fake_tool(fake_bin, "nvidia-smi", "sleep 300")
    out = tmp_path / "inv.json"
    start = time.monotonic()
    proc = _run(
        "--out",
        str(out),
        "--no-summary",
        env=_base_env([str(fake_bin)]),
        timeout=90,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert elapsed < 80, f"inventory took {elapsed:.1f}s with one hanging nvidia-smi"
    doc = json.loads(out.read_text(encoding="utf-8"))
    err = doc["sections"]["nvidia"]["value"]["nvidia_smi"].get("error", "")
    assert "timed out" in err.lower()


@pytest.mark.skipif(os.name == "nt", reason="uses /proc to find orphaned children")
def test_timed_out_tool_does_not_leave_an_orphaned_process_running(
    fake_bin: Path, tmp_path: Path
) -> None:
    """A hung tool that is killed by the per-command timeout must not leave a
    live descendant process behind: the timeout only ``kill()``s the immediate
    shell it spawned (``/bin/sh``), not any process that shell in turn started,
    so a wrapper script that hands off to a slower child leaks a real OS process
    that keeps running for its own full lifetime, invisible to the JSON output
    (which correctly reports "timed out" and moves on)."""
    marker = "meshforge_lab_inventory_orphan_probe_marker"
    _write_fake_tool(fake_bin, "nvidia-smi", f"sleep 250 # {marker}")
    out = tmp_path / "inv.json"
    proc = _run(
        "--out", str(out), "--no-summary", env=_base_env([str(fake_bin)]), timeout=90
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    time.sleep(2)  # give the OS a moment to reap anything actually killed
    survivors = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmdline or "sleep 250" in cmdline:
            survivors.append((pid, cmdline))
    for pid, _cmdline in survivors:
        with contextlib.suppress(OSError):
            os.kill(int(pid), 9)  # cleanup regardless of the assertion below
    assert not survivors, (
        f"orphaned process(es) left running after the tool's own timeout: {survivors}"
    )


@pytest.mark.slow
@pytest.mark.timeout(280)
def test_many_hanging_path_tools_finish_in_reasonable_time(
    fake_bin: Path, tmp_path: Path
) -> None:
    """Every external tool the probes call is hung at once (a pathological lab
    machine with wedged wrappers for git/lscpu/lspci/ip/mokutil/docker/nvidia-smi
    and the "tools" section's git/node/npm/gh/iperf3/protoc/psql). The run must
    stay "well under 60s" as intended by the per-command timeout design, not
    accumulate one full per-command timeout for every one of these tools
    (measured: ~266s with 14 hung tools -- see the tester report defect)."""
    hung_tools = [
        "git",
        "lscpu",
        "lspci",
        "ip",
        "mokutil",
        "docker",
        "nvidia-smi",
        "node",
        "npm",
        "gh",
        "iperf3",
        "protoc",
        "psql",
        "iw",
    ]
    for name in hung_tools:
        _write_fake_tool(fake_bin, name, "sleep 300")
    out = tmp_path / "inv.json"
    start = time.monotonic()
    proc = _run(
        "--out",
        str(out),
        "--no-summary",
        env=_base_env([str(fake_bin)]),
        timeout=280,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(doc["sections"], dict)
    sys.stderr.write(
        f"\n[timing] inventory with {len(hung_tools)} hung PATH tools: {elapsed:.1f}s\n"
    )
    assert elapsed < 60, (
        f"inventory took {elapsed:.1f}s with {len(hung_tools)} hung PATH tools; "
        "the per-command timeouts stack sequentially with no overall time budget "
        "(see tester report, defect on task 0.2.2)"
    )


def test_path_stripped_of_common_tools_still_succeeds(tmp_path: Path) -> None:
    """A minimal PATH with none of nvidia-smi/docker/git/lspci/lscpu/mokutil/ip
    available (a bare container or a locked-down lab image) must still produce a
    valid, complete document -- every affected section recorded as a failure."""
    minimal_bin = tmp_path / "minimal-bin"
    minimal_bin.mkdir()
    # Keep only what's needed to exec `python` itself; PATH has just this dir.
    env = dict(os.environ)
    env["PATH"] = str(minimal_bin)
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=env, timeout=60)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "Traceback" not in proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["sections"]["nvidia"]["value"]["nvidia_smi"]["ok"] is False
    assert doc["sections"]["docker"]["value"]["installed"] is False


# --------------------------------------------------------------------- privacy on THIS machine


def _all_ipv4_strings(text: str) -> set[str]:
    return set(re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text))


@pytest.fixture(scope="module")
def real_machine_json(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("inv") / "real.json"
    proc = _run("--out", str(out), "--no-summary")
    assert proc.returncode == 0, proc.stderr
    doc: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return doc


def test_no_public_ipv4_anywhere_in_the_document(
    real_machine_json: dict[str, Any],
) -> None:
    text = json.dumps(real_machine_json)
    for candidate in _all_ipv4_strings(text):
        try:
            addr = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue  # e.g. a masked version string with an octet > 255
        assert addr.is_private or addr.is_loopback or addr.is_link_local, (
            f"public-looking IPv4 {candidate!r} found in inventory output"
        )


def test_no_real_wifi_ssid_leaked(real_machine_json: dict[str, Any]) -> None:
    """The lab machine's real SSID (learned independently via ``nmcli``, never
    from the script under test) must not appear anywhere in the output."""
    if shutil.which("nmcli") is None:
        pytest.skip("nmcli not available to learn the real SSID on this machine")
    result = subprocess.run(
        ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"nmcli failed: {result.stderr.strip()}")
    ssids = {
        line.split(":", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("yes:") and len(line.split(":", 1)) == 2
    }
    ssids = {s for s in ssids if s and len(s) >= 4}
    hostname = str(real_machine_json.get("hostname") or "")
    # A lab hostname may coincidentally contain the SSID substring (e.g. both
    # named after the same project); that is not a leak of the SSID *field*.
    # Check the document with the (legitimately-kept) hostname value blanked out.
    dumped = json.dumps(real_machine_json)
    doc_without_hostname = (
        json.loads(dumped.replace(hostname, "")) if hostname else real_machine_json
    )
    ssids = {s for s in ssids if not hostname or s not in hostname}
    if not ssids:
        pytest.skip("no SSID independent of the (legitimately kept) hostname to check")
    text = json.dumps(doc_without_hostname)
    for ssid in ssids:
        assert ssid not in text, f"real SSID {ssid!r} leaked into inventory output"


def test_no_real_username_anywhere_in_the_document(
    real_machine_json: dict[str, Any],
) -> None:
    import getpass

    user = getpass.getuser()
    text = json.dumps(real_machine_json)
    assert user not in text, f"real username {user!r} leaked into inventory output"
    assert user not in text.lower()


def test_env_var_secret_never_leaks_on_a_real_run(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["MESHFORGE_TEST_ACCEPTANCE_SECRET"] = "s3cr3t-value-should-never-appear"
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=env)
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "s3cr3t-value-should-never-appear" not in text


def test_every_lab_field_resolves_to_an_ok_or_explicit_error_on_this_machine(
    real_machine_json: dict[str, Any],
) -> None:
    """Independent re-check of the LAB.md §1 field map against the *real* JSON
    produced on this machine (not a fixture): each mapped section is present and
    is itself well-formed (ok+value, or ok=false+error)."""
    sys.path.insert(0, str(REPO_ROOT / "lab"))
    from _inventory.model import LAB_FIELDS

    for row, paths in LAB_FIELDS.items():
        for path in paths:
            node: Any = real_machine_json
            for part in path.split("."):
                assert part in node, (row, path, "missing key", part)
                node = node[part]
            if path.startswith("sections."):
                assert isinstance(node, dict) and "ok" in node, (row, path, node)
                if node["ok"]:
                    assert "value" in node, (row, path, node)
                else:
                    assert node.get("error"), (row, path, node)
            else:
                assert node, (row, path, node)  # e.g. "hostname": a non-empty scalar


def _nvidia_problems(doc: dict[str, Any]) -> list[str]:
    return [p for p in doc["problems"] if p.startswith("nvidia:")]


def test_nvidia_section_ok_true_does_not_imply_driver_working(
    fake_bin: Path, tmp_path: Path
) -> None:
    """Design check, on a *simulated* machine whose NVIDIA driver is broken (the
    state Laptop 1 was in under Secure Boot): the ``nvidia`` section is
    ``ok: true`` -- the probe itself succeeded -- even though
    ``value.driver_working`` is ``false`` and the section is not in
    ``failed_sections``. A consumer that only checks top-level ``ok`` /
    ``failed_sections`` to decide "does this machine have a working GPU" would
    silently miss the failure; it must read ``value.driver_working``, or the
    ``problems`` list, which is exactly why ``problems`` exists.

    Uses a fake failing ``nvidia-smi`` plus a stub NVML, so the result does not
    depend on the driver state of the machine running the tests."""
    env = _broken_driver_env(fake_bin, tmp_path / "stub")
    out = tmp_path / "inv.json"
    proc = _run("--out", str(out), "--no-summary", env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    doc = json.loads(out.read_text(encoding="utf-8"))
    nv = doc["sections"]["nvidia"]
    assert nv["ok"] is True
    assert nv["value"]["driver_working"] is False
    assert "nvidia" not in doc["failed_sections"]
    # ...but the broken driver is surfaced, once, in `problems`.
    assert _nvidia_problems(doc), doc["problems"]
    assert (
        "driver" in _nvidia_problems(doc)[0]
        or "not working" in (_nvidia_problems(doc)[0])
    )


def test_nvidia_driver_state_and_problems_agree_on_this_machine(
    real_machine_json: dict[str, Any],
) -> None:
    """The same property checked against whatever this real machine is: the
    ``nvidia`` probe always succeeds, ``driver_working`` is a boolean, and a
    broken driver on a machine where NVIDIA hardware or tooling is visible is
    always reported in ``problems`` (and never reported when it works)."""
    nv = real_machine_json["sections"]["nvidia"]
    assert nv["ok"] is True
    assert "nvidia" not in real_machine_json["failed_sections"]
    working = nv["value"]["driver_working"]
    assert isinstance(working, bool)
    problems = _nvidia_problems(real_machine_json)
    if working:
        assert not problems, problems
        return
    gpus = real_machine_json["sections"]["gpus"]
    nvidia_visible = nv["value"]["nvidia_smi"].get("available") is True or (
        gpus["ok"] and any(g.get("vendor") == "NVIDIA" for g in gpus["value"])
    )
    if nvidia_visible:
        assert problems, (
            "driver_working is false with NVIDIA hardware/tooling present, "
            f"but nothing was reported in problems: {real_machine_json['problems']}"
        )
    else:
        assert not problems, problems  # no NVIDIA on this machine: not a problem


# --------------------------------------------------------------------- fresh clone, plain uv sync


_FRESH_CLONE_COPY_EXCLUDE = {
    ".venv",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".import_linter_cache",
    "dist",
}


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_fresh_clone_with_spaces_plain_uv_sync_runs_inventory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Success criterion 1, literally: a fresh copy of the repo, in a directory
    whose path contains spaces and a non-ASCII character (a realistic Windows
    path, e.g. under "OneDrive - Ünï Name"), a *plain* ``uv sync`` (no extras),
    then ``uv run python lab/inventory.py`` must produce valid JSON."""
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    root = tmp_path_factory.mktemp("inv-fresh")
    project_dir = root / "dir with spaces ü" / "MeshForge Clone"

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _FRESH_CLONE_COPY_EXCLUDE}

    shutil.copytree(REPO_ROOT, project_dir, ignore=ignore)

    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    cache_dir = root / "uv-cache"
    env["UV_CACHE_DIR"] = str(cache_dir)

    sync = subprocess.run(
        ["uv", "sync"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert sync.returncode == 0, (
        f"plain `uv sync` failed in a path with spaces:\n"
        f"stdout={sync.stdout}\nstderr={sync.stderr}"
    )

    out = root / "out with space" / "inv.json"
    run = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "lab/inventory.py",
            "--out",
            str(out),
        ],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert run.returncode == 0, (
        f"inventory.py failed after fresh clone + plain uv sync:\n"
        f"stdout={run.stdout}\nstderr={run.stderr}"
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["sections"]["python"]["ok"] is True
