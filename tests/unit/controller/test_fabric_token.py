"""Controller side of D44: create/load/rotate ``fabric.token`` and verify requests.

Uses a real directory and an injected monotonic clock (no sleeps).
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.fabric_token import FabricTokenError
from meshforge.controller.fabric_token import (
    FabricTokenVerifier,
    fingerprint,
    load_or_create_token,
    rotate_token,
)

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_first_load_creates_then_reloads_the_same_token(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "fabric.token"
    token, created = load_or_create_token(path)
    assert created
    again, created_again = load_or_create_token(path)
    assert not created_again
    assert again.get_secret_value() == token.get_secret_value()
    assert path.read_text().strip() == token.get_secret_value()
    assert [p.name for p in path.parent.glob(".*.tmp")] == []


@posix_only
def test_created_private_from_the_start(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "fabric.token"
    load_or_create_token(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_a_loosened_file_is_refused_at_load(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    load_or_create_token(path)
    os.chmod(path, 0o640)
    with pytest.raises(FabricTokenError, match="other users"):
        load_or_create_token(path)


def test_rotate_replaces_the_token(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    first, _ = load_or_create_token(path)
    second = rotate_token(path)
    assert second.get_secret_value() != first.get_secret_value()
    assert load_or_create_token(path)[0] == second
    assert fingerprint(first) != fingerprint(second)
    assert len(fingerprint(first)) == 12


def test_concurrent_first_creation_yields_one_token(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "fabric.token"
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(load_or_create_token(path)[0].get_secret_value())
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(results)) == 1


def _verifier(path: Path, clock: Clock) -> FabricTokenVerifier:
    token, _ = load_or_create_token(path)
    return FabricTokenVerifier(path, clock=clock, initial=token)


def test_verify_accepts_only_the_current_token(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    clock = Clock()
    verifier = _verifier(path, clock)
    token = path.read_text().strip()
    assert verifier.verify(token)
    assert not verifier.verify(None)
    assert not verifier.verify("")
    assert not verifier.verify(token[:-1])
    assert not verifier.verify(token + "x")
    assert not verifier.verify("x" * 43)
    assert not verifier.verify("\udcff" * 43)  # undecodable bytes never crash


def test_rotation_is_picked_up_after_the_reload_interval(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    clock = Clock()
    verifier = _verifier(path, clock)
    old = path.read_text().strip()
    new = rotate_token(path).get_secret_value()
    assert verifier.verify(old)  # cached within the interval
    clock.now += 1.0
    assert verifier.verify(new)
    assert not verifier.verify(old)


def test_fails_closed_when_the_file_disappears(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    clock = Clock()
    verifier = _verifier(path, clock)
    token = path.read_text().strip()
    path.unlink()
    clock.now += 5
    assert not verifier.verify(token)
    path.write_text(token + "\n")
    if os.name == "posix":
        os.chmod(path, 0o600)
    clock.now += 5
    assert verifier.verify(token)


@posix_only
def test_fails_closed_when_the_file_is_loosened(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    clock = Clock()
    verifier = _verifier(path, clock)
    token = path.read_text().strip()
    os.chmod(path, 0o644)
    clock.now += 5
    assert not verifier.verify(token)


def test_verifier_without_initial_reads_the_file(tmp_path: Path) -> None:
    path = tmp_path / "fabric.token"
    token, _ = load_or_create_token(path)
    verifier = FabricTokenVerifier(path, clock=Clock())
    assert verifier.verify(token.get_secret_value())
    assert not FabricTokenVerifier(tmp_path / "none", clock=Clock()).verify("a" * 43)


def test_secret_never_in_repr(tmp_path: Path) -> None:
    token, _ = load_or_create_token(tmp_path / "t")
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() not in repr(token)
