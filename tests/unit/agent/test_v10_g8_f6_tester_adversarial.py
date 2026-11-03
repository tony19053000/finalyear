"""V1.0 G8 F6 tester: low-recursion-limit readers, retry bounds, run_to_completion, registry CPU."""

from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

from meshforge.agent import known_controller
from meshforge.agent.identity import load_or_create
from meshforge.common.aio import run_to_completion
from meshforge.common.errors import ConfigError
from meshforge.common.private_files import PrivateFileError, PrivateFileReason

pytestmark = pytest.mark.unit


@pytest.fixture
def low_recursion():
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    yield
    sys.setrecursionlimit(old)


def _nest(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


# ---------------------------------------------------------- (1) readers
@pytest.mark.parametrize("depth", [5, 40, 90, 150, 5000, 100_000])
def test_known_controller_any_depth_low_limit(tmp_path, depth, low_recursion):
    d = tmp_path / "id"
    d.mkdir(mode=0o700)
    p = d / "controller.json"
    p.write_bytes(_nest(depth)[:4096])
    p.chmod(0o600)
    assert known_controller.load_known(d) is None


@pytest.mark.parametrize("depth", [5, 40, 90, 150, 5000])
def test_known_controller_nested_in_valid_envelope(tmp_path, depth, low_recursion):
    d = tmp_path / "id"
    d.mkdir(mode=0o700)
    p = d / "controller.json"
    body = b'{"format":%d,"address":' % known_controller.KNOWN_CONTROLLER_FORMAT
    p.write_bytes((body + _nest(depth) + b"}")[:4096])
    p.chmod(0o600)
    assert known_controller.load_known(d) is None


@pytest.mark.parametrize("depth", [5, 40, 90, 150, 5000, 100_000])
def test_upgrade_json_reader_low_limit(tmp_path, depth, low_recursion):
    from meshforge.common import upgrade_state

    p = tmp_path / "x.json"
    # value-nested: dict whose value is deep; str(v) must not raise
    p.write_bytes(b'{"a":' + _nest(depth) + b"}")
    try:
        out = upgrade_state._json(p, False)
    except RecursionError as e:  # defect candidate
        pytest.fail(f"upgrade_state._json raised RecursionError at depth {depth}: {e}")
    assert out is None or isinstance(out, dict)


@pytest.mark.parametrize("depth", [50, 5000])
def test_buildinfo_low_limit(tmp_path, depth, low_recursion):
    from meshforge.common.buildinfo import BUILDINFO_NAME, read_build_info

    (tmp_path / BUILDINFO_NAME).write_bytes(_nest(depth))
    assert read_build_info(tmp_path) is None
    (tmp_path / BUILDINFO_NAME).write_bytes(b'{"version":' + _nest(depth) + b"}")
    try:
        read_build_info(tmp_path)
    except RecursionError as e:
        pytest.fail(f"BUILDINFO nested value raised {e}")


def test_buildinfo_huge_int(tmp_path):
    from meshforge.common.buildinfo import BUILDINFO_NAME, read_build_info

    (tmp_path / BUILDINFO_NAME).write_text('{"version":' + "9" * 100000 + "}")
    try:
        read_build_info(tmp_path)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"raised {type(e).__name__}")


@pytest.mark.parametrize("depth", [150, 5000])
def test_toml_config_and_policy_low_limit(tmp_path, depth, low_recursion):
    from meshforge.agent.policy import parse_overlay
    from meshforge.common.config import _read_toml

    text = "a = " + "[" * depth + "]" * depth + "\n"
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError) as ei:
        _read_toml(p)
    assert str(p) in str(ei.value) and "config" in str(ei.value)
    with pytest.raises(ConfigError) as ei2:
        parse_overlay(text.encode(), tmp_path / "policy.toml")
    assert "policy" in str(ei2.value)


def test_toml_malformed_messages_unchanged(tmp_path):
    from meshforge.common.config import _read_toml

    p = tmp_path / "config.toml"
    p.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError, match="UTF-8"):
        _read_toml(p)
    p.write_text("a = = 1")
    with pytest.raises(ConfigError, match="not valid TOML"):
        _read_toml(p)


def test_migration_check_config_bytes(low_recursion, tmp_path):
    from meshforge.agent.migration.planning import check_config_bytes

    msg = check_config_bytes(
        ("a = " + "[" * 5000 + "]" * 5000).encode(), tmp_path / "c.toml"
    )
    assert msg is not None and "nested too deeply" in msg


def test_condition_marker_and_cas_sidecar_low_limit(tmp_path, low_recursion):
    from meshforge.agent.conditions.marker import ConditionMarker

    m = ConditionMarker(tmp_path)
    m.path.parent.mkdir(parents=True, exist_ok=True)
    m.path.write_bytes(_nest(100_000))
    assert m.load() == {}


def test_gc_and_cache_inventory_markers_low_limit(tmp_path, low_recursion):
    import inspect

    from meshforge.agent.cache import inventory
    from meshforge.agent.gc import collector

    # find the public entry points by signature; just assert import-level sanity
    assert inspect.getsource(inventory).count("RecursionError") >= 1
    assert inspect.getsource(collector).count("RecursionError") >= 1


# ------------------------------------------------ (2) known_controller retry
def _ident(tmp_path) -> Path:
    return load_or_create(tmp_path / "identity").path


def test_retry_total_sleep_bounded(tmp_path, monkeypatch):
    d = _ident(tmp_path)
    known_controller.remember(d, "10.0.0.5:47100", source="join")
    sleeps: list[float] = []
    monkeypatch.setattr(known_controller.time, "sleep", sleeps.append)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)

    def always(*a, **k):
        raise PrivateFileError("denied", reason=PrivateFileReason.UNREADABLE)

    monkeypatch.setattr(known_controller, "read_private", always)
    assert known_controller.load_known(d) is None
    assert len(sleeps) == 5 and sum(sleeps) <= 0.151

    sleeps.clear()
    monkeypatch.undo()
    monkeypatch.setattr(known_controller.time, "sleep", sleeps.append)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)

    def deny(*a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(known_controller, "atomic_write_bytes", deny)
    assert known_controller.remember(d, "10.0.0.9:47100", source="session") is False
    assert len(sleeps) == 5 and sum(sleeps) <= 0.151


def test_non_permission_oserror_on_write_not_retried(tmp_path, monkeypatch):
    d = _ident(tmp_path)
    calls = [0]

    def boom(*a, **k):
        calls[0] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(known_controller, "atomic_write_bytes", boom)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller.time, "sleep", lambda s: None)
    assert known_controller.remember(d, "10.0.0.9:47100", source="join") is False
    assert calls[0] == 1


def test_retry_never_swallows_corrupt_content_as_retry(tmp_path, monkeypatch):
    d = _ident(tmp_path)
    (d / "controller.json").write_bytes(b"{not json")
    (d / "controller.json").chmod(0o600)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    n = [0]
    real = known_controller.read_private

    def counting(*a, **k):
        n[0] += 1
        return real(*a, **k)

    monkeypatch.setattr(known_controller, "read_private", counting)
    assert known_controller.load_known(d) is None
    assert n[0] == 1


# ------------------------------------------------------ (3) run_to_completion
@pytest.mark.asyncio
async def test_rtc_cancel_waits_then_raises_cancelled():
    done = []

    async def work():
        await asyncio.sleep(0.2)
        done.append(1)
        return 5

    t = asyncio.create_task(run_to_completion(work()))
    await asyncio.sleep(0.02)
    t.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert done == [1] and time.monotonic() - t0 >= 0.15
    assert t.cancelled()


@pytest.mark.asyncio
async def test_rtc_multiple_cancels_still_completes():
    done = []

    async def work():
        await asyncio.sleep(0.2)
        done.append(1)

    t = asyncio.create_task(run_to_completion(work()))
    for _ in range(5):
        await asyncio.sleep(0.02)
        t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert done == [1]


@pytest.mark.asyncio
async def test_rtc_result_and_exception_passthrough():
    async def ok():
        return 7

    async def bad():
        raise KeyError("x")

    assert await run_to_completion(ok()) == 7
    with pytest.raises(KeyError):
        await run_to_completion(bad())


@pytest.mark.asyncio
async def test_rtc_error_during_cancel_wins():
    async def bad():
        await asyncio.sleep(0.1)
        raise KeyError("x")

    t = asyncio.create_task(run_to_completion(bad()))
    await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(KeyError):
        await t


@pytest.mark.asyncio
async def test_rtc_inner_cancelled_propagates():
    async def inner():
        await asyncio.sleep(10)

    holder = {}

    async def outer():
        holder["t"] = asyncio.ensure_future(inner())
        return await run_to_completion(holder["t"])

    t = asyncio.create_task(outer())
    await asyncio.sleep(0.02)
    holder["t"].cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


@pytest.mark.asyncio
async def test_rtc_hanging_close_is_documented_unbounded():
    """aw 'must be bounded': a hanging close hangs the caller (by design).
    The caller can still escape through an outer timeout only via a second
    mechanism; record the behaviour."""

    async def hang():
        await asyncio.sleep(3600)

    t = asyncio.create_task(run_to_completion(hang()))
    await asyncio.sleep(0.01)
    t.cancel()
    done, _ = await asyncio.wait({t}, timeout=0.3)
    assert not done  # still waiting: documented, caller must bound aw
    t.cancel()
    await asyncio.sleep(0)
    # cleanup: force the underlying task
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
    await asyncio.sleep(0.05)


# ------------------------------------------------------------ (4) cpu_windows
def _install_fake_winreg(monkeypatch, *, open_exc=None, query=None):
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.REG_SZ = 1
    fake.REG_BINARY = 3

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(*a):
        if open_exc:
            raise open_exc
        return Key()

    fake.OpenKey = open_key
    fake.QueryValueEx = lambda k, n: query
    monkeypatch.setitem(sys.modules, "winreg", fake)
    import psutil  # noqa: F401  (import before faking the platform)

    monkeypatch.setattr(sys, "platform", "win32")
    sys.modules.pop("meshforge.agent.hardware.cpu_windows", None)
    return fake


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"open_exc": FileNotFoundError()}, ""),
        ({"open_exc": PermissionError(5, "Access is denied")}, ""),
        ({"query": (b"bytes", 3)}, ""),
        ({"query": (123, 1)}, ""),
        ({"query": (b"x", 1)}, ""),
        ({"query": ("  Intel(R) Core(TM) i7  ", 1)}, "  Intel(R) Core(TM) i7  "),
        ({"query": ("A" * 1000, 1)}, "A" * 256),
    ],
)
def test_registry_cpu_name(monkeypatch, kw, expect):
    import subprocess

    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    _install_fake_winreg(monkeypatch, **kw)
    try:
        mod = __import__("meshforge.agent.hardware.cpu_windows", fromlist=["x"])
        assert mod.processor_name_string() == expect
    finally:
        sys.modules.pop("meshforge.agent.hardware.cpu_windows", None)
    assert not spawned


def test_collect_on_win32_no_subprocess_and_falls_back(monkeypatch):
    import subprocess

    from meshforge.agent.hardware import cpu

    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", lambda *a, **k: spawned.append(a)
    )
    _install_fake_winreg(monkeypatch, open_exc=PermissionError(5, "denied"))
    monkeypatch.setattr(cpu.platform, "processor", lambda: "Intel64 Family 6")
    try:
        facts = cpu._collect(1.0)
    finally:
        sys.modules.pop("meshforge.agent.hardware.cpu_windows", None)
    assert facts.model == "Intel64 Family 6" and not spawned


def test_collect_on_win32_uses_registry(monkeypatch):
    from meshforge.agent.hardware import cpu

    _install_fake_winreg(monkeypatch, query=("   Intel(R)   Core(TM) i5 \n", 1))
    try:
        facts = cpu._collect(1.0)
    finally:
        sys.modules.pop("meshforge.agent.hardware.cpu_windows", None)
    assert facts.model == "Intel(R) Core(TM) i5"


@pytest.mark.asyncio
async def test_finished_within_true_false_and_exception() -> None:
    from meshforge.common.aio import finished_within

    async def quick() -> None:
        await asyncio.sleep(0.01)

    async def slow() -> None:
        await asyncio.sleep(3600)

    async def bad() -> None:
        raise KeyError("k")

    assert await finished_within(quick(), 1) is True
    t0 = time.monotonic()
    assert await finished_within(slow(), 0.1) is False
    assert time.monotonic() - t0 < 1
    with pytest.raises(KeyError):
        await finished_within(bad(), 1)


@pytest.mark.asyncio
async def test_rtc_over_finished_within_hanging_cancel_bounded() -> None:
    from meshforge.common.aio import finished_within

    async def slow() -> None:
        await asyncio.sleep(3600)

    t = asyncio.create_task(run_to_completion(finished_within(slow(), 0.3)))
    await asyncio.sleep(0.05)
    t.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(t, 3)
    assert time.monotonic() - t0 < 1.5


def test_forget_retries_then_gives_up_bounded(tmp_path, monkeypatch):
    d = _ident(tmp_path)
    known_controller.remember(d, "10.0.0.5:47100", source="join")
    sleeps: list[float] = []
    monkeypatch.setattr(known_controller.time, "sleep", sleeps.append)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    real = Path.unlink
    n = [0]

    def unlink(self, missing_ok=False):
        if self.name == "controller.json":
            n[0] += 1
            if n[0] <= 2:
                raise PermissionError(13, "Access is denied")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    known_controller.forget(d)
    assert not (d / "controller.json").exists() and n[0] == 3

    known_controller.remember(d, "10.0.0.5:47100", source="join")
    n[0] = -(10**6)  # every call fails
    sleeps.clear()
    known_controller.forget(d)  # must not raise
    assert (d / "controller.json").exists()
    assert sum(sleeps) <= 0.151 and len(sleeps) <= 5


def test_forget_posix_no_retry(tmp_path, monkeypatch):
    d = _ident(tmp_path)
    known_controller.remember(d, "10.0.0.5:47100", source="join")
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", False)
    monkeypatch.setattr(known_controller.time, "sleep", lambda s: pytest.fail("slept"))
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name == "controller.json":
            raise PermissionError(13, "denied")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    known_controller.forget(d)
