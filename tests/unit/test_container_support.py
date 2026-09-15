"""Unit tests for ``tests/_support/mf_test_containers.py`` helpers that need no Docker.

Covers the SIGINT shield (DEFECT-0.1.8-01 fix round 2: a Ctrl-C during container
removal must be deferred, also when the signal is raised from another thread) and
the owner-identity helpers the stale sweep relies on.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import pytest
from mf_test_containers import (
    owner_is_gone,
    pid_alive,
    pid_namespace_id,
    process_start_time,
    sigint_deferred,
)


def _sigint_now() -> None:
    signal.raise_signal(signal.SIGINT)
    time.sleep(0.05)  # handlers run between bytecodes: give it the chance


def _sigint_from_thread() -> None:
    worker = threading.Thread(target=signal.raise_signal, args=(signal.SIGINT,))
    worker.start()
    worker.join()
    time.sleep(0.05)


def _shielded(body: Callable[[], None], done: list[str]) -> None:
    with sigint_deferred():
        body()
        done.append("body finished")


def _nested(done: list[str]) -> None:
    with sigint_deferred():
        _shielded(_sigint_now, done)  # the inner shield must not raise
        done.append("after inner")


def test_sigint_deferred_defers_and_reraises_after_block() -> None:
    before = signal.getsignal(signal.SIGINT)
    done: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        _shielded(_sigint_now, done)
    assert done == ["body finished"]
    assert signal.getsignal(signal.SIGINT) is before


def test_sigint_deferred_covers_signal_raised_from_another_thread() -> None:
    done: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        _shielded(_sigint_from_thread, done)
    assert done == ["body finished"]


def test_sigint_deferred_without_signal_does_not_raise() -> None:
    before = signal.getsignal(signal.SIGINT)
    with sigint_deferred():
        pass
    assert signal.getsignal(signal.SIGINT) is before


def test_sigint_deferred_nested_defers_to_outer_shield() -> None:
    done: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        _nested(done)
    assert done == ["body finished", "after inner"]


def test_sigint_deferred_off_main_thread_is_a_no_op() -> None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with sigint_deferred():
                pass
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert errors == []


def test_process_start_time_is_stable_and_distinguishes_processes() -> None:
    own = process_start_time(os.getpid())
    if own is None:
        pytest.skip("no start-time source here (non-Linux without psutil)")
    assert process_start_time(os.getpid()) == own
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.05)  # ensure a later start tick than the pytest process
        other_start = process_start_time(other.pid)
        assert other_start is not None
        assert other_start != own
    finally:
        other.kill()
        other.wait()


@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "win32")),
    reason="namespace identity is defined for Linux and Windows only",
)
def test_pid_namespace_id_is_stable_and_platform_prefixed() -> None:
    namespace = pid_namespace_id()
    assert namespace is not None
    assert namespace.startswith("linux:" if sys.platform != "win32" else "win32:")


def test_owner_is_gone_detects_dead_pid_and_pid_reuse() -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    if pid_alive(dead.pid) is None:
        pytest.skip("liveness unknown here (Windows without psutil)")
    assert owner_is_gone(dead.pid, None)

    own = process_start_time(os.getpid())
    if own is None:
        pytest.skip("no start-time source here (non-Linux without psutil)")
    assert not owner_is_gone(os.getpid(), own)
    assert not owner_is_gone(os.getpid(), None)  # legacy label: kept
    assert owner_is_gone(os.getpid(), f"1{own}")  # alive but different start
