"""V1.0 G1 fix round 1 (roadmap 1.0.2, D410/D411): disk_fill leaves 512 MiB and
must be proven, lasting faults are undone (wrapper traps + ``chaos.py cleanup``),
command values are pattern-checked, the leak fallback needs a long lifetime.

The ``lab/mf-chaos`` tests run the real POSIX shell script with fake
``systemctl`` / ``systemd-run`` / ``nmcli`` / ``tc`` / ``df`` on ``PATH``, so
the argument checks and the ``trap`` undo are exercised for real (signals
included) without touching this machine's network or disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import soak_world as w

import chaos as chaos_cli
from _soak import chaos, report, rules
from _soak.plan import Target

pytestmark = pytest.mark.unit
MIB = w.MIB
WRAPPER = Path(__file__).resolve().parents[3] / "lab" / "mf-chaos"


# ------------------------------------------------------------ catalogue
def test_disk_fill_leaves_half_the_agent_floor_free() -> None:
    store = pytest.importorskip("meshforge.agent.cas.store")
    floor = store.DEFAULT_FREE_DISK_FLOOR_BYTES

    kind = chaos.CATALOGUE["disk_fill"]
    assert dict(kind.params) == {"leave_free_mib": "512"}
    assert floor == chaos.DISK_FILL_LEAVE_FREE_MIB * MIB * 2
    action = chaos.Action("A3", w.at(0), 0, "disk_fill", "L2", "ssh", 600.0,
                          dict(kind.params))  # fmt: skip
    target = Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2")
    assert chaos.render_command(action, target) == (
        "sudo -n /usr/local/sbin/mf-chaos disk-fill a3 "
        "/var/lib/meshforge/mf-chaos-fill.bin 512 600"
    )


# ------------------------------------------------------ safe rendering
@pytest.mark.parametrize(
    "bad",
    ["/x; rm -rf /", "$(reboot)", "a b", "`id`", "x'y", 'x"y', "a\nb", "a|b", "a&b"],
)
def test_unsafe_target_values_are_refused(bad: str) -> None:
    action = chaos.Action("A1", w.at(0), 0, "disk_fill", "L2", "ssh", 60.0,
                          {"leave_free_mib": "512"})  # fmt: skip
    target = Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2",
                    vars={"fill_file": bad})  # fmt: skip
    with pytest.raises(chaos.TemplateError, match="unsafe"):
        chaos.render_command(action, target)
    netem = chaos.Action("A2", w.at(0), 0, "netem", "L2", "ssh", 60.0,
                         {"delay_ms": "200", "loss_pct": "5"})  # fmt: skip
    with pytest.raises(chaos.TemplateError, match="unsafe"):
        chaos.render_command(
            netem, Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2", iface=bad)
        )


def test_custom_template_values_are_checked_and_unknown_placeholder_named() -> None:
    action = chaos.Action("A1", w.at(0), 0, "kill_agent", "L2", "ssh", 0.0, {})
    ok = Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2",
                vars={"svc": "my-agent.service"},
                commands={"kill_agent": "sudo -n systemctl kill {svc}"})  # fmt: skip
    assert chaos.render_command(action, ok) == "sudo -n systemctl kill my-agent.service"
    unknown = Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2",
                     commands={"kill_agent": "kill {nope}"})  # fmt: skip
    with pytest.raises(chaos.TemplateError, match="nope"):
        chaos.render_command(action, unknown)


def test_cleanup_command_with_and_without_iface() -> None:
    plain = Target("L1", "linux", ("controller",), "local")
    assert chaos.cleanup_command(plain) == (
        "sudo -n /usr/local/sbin/mf-chaos cleanup - /var/lib/meshforge/mf-chaos-fill.bin"
    )
    wifi = Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2", iface="wlp3s0",
                  vars={"fill_file": "/data/mf-chaos-fill.bin"})  # fmt: skip
    assert chaos.cleanup_command(wifi).endswith(
        "cleanup wlp3s0 /data/mf-chaos-fill.bin"
    )


# ------------------------------------------------------- chaos.py cleanup
def _recorder(code: int = 0) -> tuple[list[list[str]], Any]:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: float) -> tuple[int, str]:
        calls.append(argv)
        return code, "cleanup done"

    return calls, runner


def test_cleanup_runs_on_linux_laptops_and_prints_windows(capsys: Any) -> None:
    calls, runner = _recorder()
    assert chaos_cli.cleanup(w.plan().targets, runner) == 0
    assert calls[0][:2] == ["sh", "-c"]  # L1: local
    assert "mf-chaos cleanup" in calls[0][2]
    assert calls[1][0] == "ssh" and calls[1][-2] == "lab@l2"  # L2: over SSH
    assert len(calls) == 2  # L3 is Windows: printed, not run
    assert "-Label L3 -Cleanup" in capsys.readouterr().out
    harness = Target("H", "linux", ("agent",), "harness")
    assert chaos_cli.cleanup([harness], runner) == 0
    assert len(calls) == 2


def test_cleanup_failure_is_counted_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    calls, runner = _recorder(code=255)
    assert chaos_cli.cleanup(w.plan().targets, runner) == 2
    assert "FAILED (exit 255)" in capsys.readouterr().out
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"targets": [t.as_dict() for t in w.plan().targets]}))
    monkeypatch.setattr(chaos_cli, "run_argv", runner)
    assert (
        chaos_cli.main(["cleanup", "--targets", str(targets), "--only", "L2"])
        == chaos_cli.EXIT_ACTION_FAILED
    )
    assert calls[-1][0] == "ssh"
    assert chaos_cli.main(["cleanup", "--targets", str(targets), "--only", "L9"]) == (
        chaos_cli.EXIT_USAGE
    )
    ok_calls, ok = _recorder(code=0)
    monkeypatch.setattr(chaos_cli, "run_argv", ok)
    assert chaos_cli.main(["cleanup", "--targets", str(targets)]) == chaos_cli.EXIT_OK
    assert len(ok_calls) == 2


def test_run_cleans_up_at_start_and_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleaned: list[str] = []
    monkeypatch.setattr(chaos_cli, "_load", lambda _d: (w.plan(), []))
    monkeypatch.setattr(
        chaos_cli, "cleanup", lambda targets, runner=None: cleaned.append("c") or 0
    )

    def run(_self: Any) -> int:
        assert cleaned == ["c"]  # before the first action
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)  # the handler raises first
        return 0

    monkeypatch.setattr(chaos_cli.Driver, "run", run)
    saved = {
        s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }
    try:
        code = chaos_cli.main(["run", "--dir", str(tmp_path)])
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    assert code == chaos_cli.EXIT_INTERRUPTED
    assert cleaned == ["c", "c"]


# ------------------------------------------------------- disk_fill proof
def _disk_world(attempt_node: str, attempt_end_s: float, failure: str) -> Any:
    attempts = [
        {"attempt_id": "a1", "node_id": attempt_node, "state": "failed", "role": "run",
         "failure_type": failure, "ended_at": w.at(attempt_end_s)},
        {"attempt_id": "a2", "node_id": "node_L3", "state": "done", "role": "run",
         "ended_at": w.at(attempt_end_s + 60)},
    ]  # fmt: skip
    view = w.view("job0", end_s=attempt_end_s + 60, attempts=attempts)
    return w.world(
        jobs=[(w.spec(0), view)],
        actions=[
            w.action("A1", "disk_fill", "L2", 1000, 1120),
            w.action("A2", "kill_agent", "L3", 2000),
        ],
    )


def test_disk_fill_is_proven_by_a_disk_full_attempt_on_its_laptop() -> None:
    rule = rules.rule_chaos(_disk_world("node_L2", 1100, "disk_full"))
    assert rule["verdict"] == rules.PASS, rule["summary"]
    assert rule["details"]["proof"]["A1"] == ["job0/a1"]
    assert rule["details"]["unproven"] == []
    # the deferred job (retried on L3, succeeded) is not lost
    world = _disk_world("node_L2", 1100, "disk_full")
    assert rules.rule_lost_jobs(world, rules.Attributor(world))["verdict"] == rules.PASS
    text = report.render(
        rules.evaluate(world, generated_at=w.T0, meta={}), json_name="v"
    )
    assert "job0/a1" in text


@pytest.mark.parametrize(
    ("node", "end_s", "failure"),
    [
        ("node_L3", 1100, "disk_full"),  # another laptop
        ("node_L2", 1400, "disk_full"),  # after the window (1120 + 180 grace)
        ("node_L2", 1100, "node_lost"),  # not a disk_full
    ],
)
def test_disk_fill_without_proof_is_not_counted(
    node: str, end_s: float, failure: str
) -> None:
    rule = rules.rule_chaos(_disk_world(node, end_s, failure))
    assert rule["details"]["unproven"] == ["A1"]
    assert rule["details"]["counted"] == 1
    assert rule["verdict"] == rules.FAIL  # 2 needed, only the kill counts
    assert "not proven" in rule["summary"]


def test_unproven_disk_fill_with_enough_other_actions_is_inconclusive() -> None:
    world = _disk_world("node_L3", 1100, "disk_full")
    world.actions.append(w.action("A3", "kill_agent", "L2", 2500))
    rule = rules.rule_chaos(world)
    assert rule["verdict"] == rules.INCONCLUSIVE
    no_final = w.world(collected=False, actions=world.actions)
    assert rules.rule_chaos(no_final)["verdict"] == rules.INCONCLUSIVE


# -------------------------------------------------- leak rule refinements
def test_excluded_share_is_reported_per_series() -> None:
    memory = rules.rule_memory(w.world())
    row = next(r for r in memory["details"] if r["series"] == "L2/agent")
    # 1 min warm-up of a 72 min series sampled every 30 s: 2 of 145 samples
    assert row["excluded_share"] == pytest.approx(2 / 145, abs=1e-3)
    handles = rules.rule_handles(w.world())
    assert all("excluded_share" in r for r in handles["details"])


def test_fallback_on_short_lifetimes_is_inconclusive_not_pass() -> None:
    """Restarted every 8 min: no lifetime spans both quiet windows, and the
    longest (< 10 min = 12 h scaled to the 1 h plan) cannot tell a leak from
    warm-up - INCONCLUSIVE, never a PASS on the slope alone."""
    samples = w.healthy_samples()
    samples["L2"] = w.sampler(
        "L2", {"agent": ("agent", "L2")}, pids={"agent": lambda t: 100 + int(t // 480)}
    )
    world = w.world(samples=samples)
    memory = next(
        r for r in rules.rule_memory(world)["details"] if r["series"] == "L2/agent"
    )
    assert memory["growth_basis"] == "too_short"
    assert memory["verdict"] == rules.INCONCLUSIVE
    fds = next(
        r
        for r in rules.rule_handles(world)["details"]
        if r["series"] == "L2/agent" and r["metric"] == "fds"
    )
    assert fds["growth_basis"] == "too_short"
    assert fds["verdict"] == rules.INCONCLUSIVE


def test_fallback_min_span_scales_with_the_plan() -> None:
    assert rules.fallback_min_span_s(72.0) == 12 * 3600.0
    assert rules.fallback_min_span_s(100.0) == 12 * 3600.0
    assert rules.fallback_min_span_s(1.0) == pytest.approx(600.0)
    assert rules.fallback_min_span_s(0.1) == pytest.approx(60.0)


# ------------------------------------------------------- lab/mf-chaos (real sh)
def _fake_bin(tmp_path: Path, avail_bytes: int = 3 * MIB) -> tuple[Path, Path]:
    """Fake system tools that append their argv to ``calls.log``."""
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()
    for name in ("systemctl", "systemd-run", "nmcli", "tc", "fallocate", "runuser"):
        body = f'echo "{name} $*" >> "{log}"\n'
        if name == "tc":
            body += 'case "$*" in "qdisc show"*) echo "qdisc netem 8001: root";; esac\n'
        if name == "nmcli":
            body += '[ "$#" -eq 1 ] && echo enabled\n'
        if name == "fallocate":  # really create the file (size argument: -l N -- F)
            body += 'shift; n=$1; shift; shift; head -c "$n" /dev/zero > "$1"\n'
        (bindir / name).write_text("#!/bin/sh\n" + body)
        (bindir / name).chmod(0o755)
    (bindir / "df").write_text(f'#!/bin/sh\necho Avail\necho " {avail_bytes}"\n')
    (bindir / "df").chmod(0o755)
    return bindir, log


def _sh(bindir: Path, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["sh", str(WRAPPER), *args], env=env, capture_output=True, text=True,
        timeout=30, check=False, **kw,
    )  # fmt: skip


def _log(log: Path) -> str:
    return log.read_text() if log.exists() else ""


needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


@needs_sh
@pytest.mark.parametrize(
    "args",
    [
        ("disk-fill", "a1", "/etc/passwd", "512", "10"),
        ("disk-fill", "a1", "/var/../etc/mf-chaos-fill.bin", "512", "10"),
        ("disk-fill", "a1", "/var/lib/meshforge/mf-chaos-fill.bin", "5x", "10"),
        ("net-down", "a;b", "10"),
        ("net-down", "a1", "10; reboot"),
        ("netem", "a1", "eth0 x", "200", "5", "10"),
        ("netem", "a1", "eth0", "200", "5"),
        ("cleanup", "eth0", "/etc/shadow"),
        ("stop-postgres", "/usr/bin", "/var/lib/meshforge/db/data"),
        ("kill-job-tree", "/etc"),
        ("kill-agent", "extra"),
        ("reinstall", "/tmp/x.sh"),
        ("format-disk",),
        (),
    ],
)
def test_wrapper_refuses_bad_arguments_before_doing_anything(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    bindir, log = _fake_bin(tmp_path)
    done = _sh(bindir, *args)
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "mf-chaos:" in done.stderr
    assert _log(log) == ""


@needs_sh
def test_wrapper_refuses_a_deb_not_owned_by_root(tmp_path: Path) -> None:
    bindir, log = _fake_bin(tmp_path)
    deb = tmp_path / "meshforge-controller_1.0.0_amd64.deb"
    deb.write_text("x")
    done = _sh(bindir, "reinstall", str(deb))
    if os.geteuid() != 0:
        assert done.returncode == 2
        assert "owned by root" in done.stderr
        assert "apt-get" not in _log(log)


@needs_sh
def test_wrapper_detaches_lasting_faults_as_named_units(tmp_path: Path) -> None:
    bindir, log = _fake_bin(tmp_path)
    assert _sh(bindir, "net-down", "a7", "30").returncode == 0
    assert _sh(bindir, "netem", "a8", "wlp3s0", "200", "5", "600").returncode == 0
    fill = "/var/lib/meshforge/mf-chaos-fill.bin"
    assert _sh(bindir, "disk-fill", "a9", fill, "512", "37.5").returncode == 0
    lines = _log(log).splitlines()
    assert lines == [
        "systemd-run --collect --quiet --unit=mf-chaos-a7 /usr/local/sbin/mf-chaos "
        "_body-net-down 30",
        "systemd-run --collect --quiet --unit=mf-chaos-a8 /usr/local/sbin/mf-chaos "
        "_body-netem wlp3s0 200 5 600",
        "systemd-run --collect --quiet --unit=mf-chaos-a9 /usr/local/sbin/mf-chaos "
        f"_body-disk-fill {fill} 512 37.5",
    ]


def _start_body(bindir: Path, *args: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return subprocess.Popen(
        ["sh", str(WRAPPER), *args], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True,
    )  # fmt: skip


def _reap(proc: subprocess.Popen[str]) -> None:
    """Kill what is left of the group (a ``sleep`` that ignored SIGINT)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _wait_for(check: Any, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@needs_sh
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_disk_fill_body_removes_its_file_on_a_signal(
    tmp_path: Path, sig: signal.Signals
) -> None:
    """``systemctl stop mf-chaos-ID`` signals the unit's whole cgroup; the
    trap must undo the fill at once, not after the sleep."""
    bindir, _log_path = _fake_bin(tmp_path, avail_bytes=3 * MIB)
    fill = tmp_path / "vol" / "mf-chaos-fill.bin"
    fill.parent.mkdir()
    proc = _start_body(bindir, "_body-disk-fill", str(fill), "1", "60")
    try:
        _wait_for(fill.exists)
        _wait_for(lambda: fill.stat().st_size == 2 * MIB)  # avail 3 MiB - leave 1
        started = time.monotonic()
        os.killpg(proc.pid, sig)
        proc.wait(timeout=10)
        assert time.monotonic() - started < 5
        assert not fill.exists()
    finally:
        _reap(proc)


@needs_sh
def test_disk_fill_body_undoes_on_normal_end_and_net_down_traps(tmp_path: Path) -> None:
    bindir, log = _fake_bin(tmp_path, avail_bytes=3 * MIB)
    fill = tmp_path / "mf-chaos-fill.bin"
    done = _sh(bindir, "_body-disk-fill", str(fill), "1", "0.2")
    assert done.returncode == 0, done.stderr
    assert "wrote 2097152 bytes" in done.stdout
    assert not fill.exists()
    proc = _start_body(bindir, "_body-net-down", "60")
    try:
        _wait_for(lambda: "nmcli networking off" in _log(log))
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        _reap(proc)
    calls = _log(log).splitlines()
    assert calls[calls.index("nmcli networking off") + 1] == "nmcli networking on"


@needs_sh
def test_netem_body_deletes_the_qdisc_when_done(tmp_path: Path) -> None:
    bindir, log = _fake_bin(tmp_path)
    done = _sh(bindir, "_body-netem", "wlp3s0", "200", "5", "0.1")
    assert done.returncode == 0, done.stderr
    calls = _log(log).splitlines()
    assert calls[0] == "tc qdisc replace dev wlp3s0 root netem delay 200ms loss 5%"
    assert "tc qdisc del dev wlp3s0 root" in calls


@needs_sh
def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    bindir, log = _fake_bin(tmp_path)
    fill = tmp_path / "mf-chaos-fill.bin"
    fill.write_bytes(b"x" * 1024)
    for _ in range(2):
        done = _sh(bindir, "cleanup", "wlp3s0", str(fill))
        assert done.returncode == 0, done.stderr
        assert "cleanup done" in done.stdout
        assert not fill.exists()
    calls = _log(log)
    assert "systemctl stop mf-chaos-*.service" in calls
    assert "nmcli networking on" in calls
    assert "tc qdisc del dev wlp3s0 root" in calls
    assert _sh(bindir, "cleanup", "-", "-").returncode == 0
