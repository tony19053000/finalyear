"""V1.0 G8 F6 - local-file readers never raise on hostile content (Windows CI).

On Windows the C JSON scanner hits ``RecursionError`` at a far smaller nesting
depth than on Linux (``b"[" * 3000`` raised there), and ``tomllib`` recurses in
Python on every platform. Each reader of an agent-local file must turn that into
its typed "corrupt" outcome. Linux cannot reach the Windows depth inside the
small caps, so bounded readers are driven by a ``json.loads`` that raises
``RecursionError``; unbounded ones get genuinely deep content.

Also the Windows sharing-violation retry of ``controller.json`` (a reader's open
and the writer's ``os.replace`` collide there), driven via ``_RETRY_SHARING``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent import known_controller
from meshforge.agent.identity import load_or_create
from meshforge.common.errors import ConfigError, IdentityError
from meshforge.common.private_files import PrivateFileError, PrivateFileReason

pytestmark = pytest.mark.unit

_DEEP_JSON = b"[" * 200_000
_DEEP_TOML = "a = " + "[" * 5_000 + "]" * 5_000 + "\n"


def _recursing_json() -> SimpleNamespace:
    def loads(*_a: Any, **_k: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded while decoding")

    return SimpleNamespace(loads=loads, dumps=json.dumps)


@pytest.fixture
def ident_dir(tmp_path: Path) -> Path:
    return load_or_create(tmp_path / "identity").path


# ------------------------------------------------------------ controller.json


def test_known_controller_recursion_is_ignored_not_raised(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    monkeypatch.setattr(known_controller, "json", _recursing_json())
    assert known_controller.load_known(ident_dir) is None


def _flaky_read(
    fails: int, real: Any, reason: PrivateFileReason = PrivateFileReason.UNREADABLE
) -> tuple[Any, list[int]]:
    calls = [0]

    def read(*a: Any, **k: Any) -> bytes:
        calls[0] += 1
        if calls[0] <= fails:
            raise PrivateFileError("sharing violation", reason=reason)
        data: bytes = real(*a, **k)
        return data

    return read, calls


def test_windows_sharing_violation_on_read_is_retried(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    read, calls = _flaky_read(3, known_controller.read_private)
    monkeypatch.setattr(known_controller, "read_private", read)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address == "10.0.0.5:47100"
    assert calls[0] == 4


def test_persistent_unreadable_gives_up_bounded(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read, calls = _flaky_read(10**6, known_controller.read_private)
    monkeypatch.setattr(known_controller, "read_private", read)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    assert known_controller.load_known(ident_dir) is None
    assert calls[0] == known_controller._SHARING_ATTEMPTS


@pytest.mark.parametrize(
    "reason", [PrivateFileReason.MISSING, PrivateFileReason.NOT_PRIVATE]
)
def test_only_unreadable_is_retried(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch, reason: PrivateFileReason
) -> None:
    read, calls = _flaky_read(10**6, known_controller.read_private, reason)
    monkeypatch.setattr(known_controller, "read_private", read)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    assert known_controller.load_known(ident_dir) is None
    assert calls[0] == 1


def test_posix_does_not_retry(ident_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    read, calls = _flaky_read(10**6, known_controller.read_private)
    monkeypatch.setattr(known_controller, "read_private", read)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", False)
    assert known_controller.load_known(ident_dir) is None
    assert calls[0] == 1


def test_windows_sharing_violation_on_replace_is_retried(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = known_controller.atomic_write_bytes
    calls = [0]

    def write(*a: Any, **k: Any) -> None:
        calls[0] += 1
        if calls[0] <= 2:
            raise PermissionError(13, "Access is denied")
        real(*a, **k)

    monkeypatch.setattr(known_controller, "atomic_write_bytes", write)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    assert known_controller.remember(ident_dir, "10.0.0.6:47100", source="session")
    loaded = known_controller.load_known(ident_dir)
    assert loaded is not None and loaded.address == "10.0.0.6:47100"
    assert calls[0] == 3


def test_persistent_replace_failure_is_logged_false(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = [0]

    def write(*_a: Any, **_k: Any) -> None:
        calls[0] += 1
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(known_controller, "atomic_write_bytes", write)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    assert not known_controller.remember(ident_dir, "10.0.0.6:47100", source="join")
    assert calls[0] == known_controller._SHARING_ATTEMPTS


# ------------------------------------------------------- other local readers


def test_identity_metadata_too_deep_is_typed(tmp_path: Path) -> None:
    from meshforge.agent.identity import _read_json

    path = tmp_path / "node.json"
    path.write_bytes(_DEEP_JSON)
    with pytest.raises(IdentityError):
        _read_json(path)


def test_local_node_too_deep_is_typed(tmp_path: Path) -> None:
    from meshforge.common.local_node import (
        MAX_NODE_JSON_BYTES,
        LocalNodeError,
        local_node_json,
        read_local_node_id,
    )

    path = local_node_json(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[" * MAX_NODE_JSON_BYTES)
    with pytest.raises(LocalNodeError):
        read_local_node_id(tmp_path)


def test_policy_overlay_too_deep_is_typed(tmp_path: Path) -> None:
    from meshforge.agent.policy import parse_overlay

    with pytest.raises(ConfigError):
        parse_overlay(_DEEP_TOML.encode(), tmp_path / "policy.toml")


def test_config_file_too_deep_is_typed(tmp_path: Path) -> None:
    from meshforge.common.config import _read_toml

    path = tmp_path / "config.toml"
    path.write_text(_DEEP_TOML, encoding="utf-8")
    with pytest.raises(ConfigError, match="nested too deeply"):
        _read_toml(path)


def test_condition_marker_too_deep_is_moved_aside(tmp_path: Path) -> None:
    from meshforge.agent.conditions.marker import MARKER_NAME, ConditionMarker

    marker = ConditionMarker(tmp_path)
    marker.path.parent.mkdir(parents=True, exist_ok=True)
    marker.path.write_bytes(_DEEP_JSON)
    assert marker.load() == {}
    assert list(tmp_path.glob(f"{MARKER_NAME}.corrupt-*"))


def test_benchmark_cache_too_deep_is_corrupt(tmp_path: Path) -> None:
    from meshforge.agent.benchmarks.cache import CacheStatus, cache_path, load_cache

    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[" * 100_000)
    assert load_cache(tmp_path, "fp").status is CacheStatus.CORRUPT


def test_windows_sharing_violation_on_forget_is_retried(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    real_unlink = Path.unlink
    calls = [0]

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == known_controller.CONTROLLER_FILE_NAME:
            calls[0] += 1
            if calls[0] <= 2:
                raise PermissionError(13, "Access is denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    known_controller.forget(ident_dir)
    assert calls[0] == 3
    assert known_controller.load_known(ident_dir) is None


def test_persistent_forget_failure_is_logged_not_raised(
    ident_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    real_unlink = Path.unlink
    calls = [0]

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == known_controller.CONTROLLER_FILE_NAME:
            calls[0] += 1
            raise PermissionError(13, "Access is denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(known_controller, "_RETRY_SHARING", True)
    monkeypatch.setattr(known_controller, "_SHARING_BACKOFF_S", 0.0)
    known_controller.forget(ident_dir)  # logged, never raised
    assert calls[0] == known_controller._SHARING_ATTEMPTS
