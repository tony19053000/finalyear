"""V0.7 G6 fix round 3, T-19 (D164): ``SecretWrite`` - whichever of "the write
returns" and "the cleanup stops waiting" comes first, the folder is wiped; and
the ``[agent.execution] secret_write_wait_s`` setting.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.execution.secret_files import (
    SecretFilesError,
    SecretVault,
    SecretWrite,
)
from meshforge.agent.settings import ExecutionSettings

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

VALUE = b"t19-unit-secret"


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "root", tag="191919191919")


def _left(vault: SecretVault) -> list[str]:
    return sorted(p.name for p in vault.base.iterdir()) if vault.base.exists() else []


def test_finish_after_the_write_wipes_now(vault: SecretVault) -> None:
    write = SecretWrite(vault, "att_1")
    folder = write.run({"K": VALUE}, read_only=False)
    assert (folder / "K").read_bytes() == VALUE
    assert write.finish() is True
    assert _left(vault) == []
    assert write.finish() is True  # idempotent


def test_finish_before_the_write_started_means_nothing_is_ever_written(
    vault: SecretVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = SecretWrite(vault, "att_1")
    called: list[str] = []
    monkeypatch.setattr(
        SecretVault, "materialise", lambda *a, **k: called.append("materialise")
    )
    assert write.finish() is False
    with pytest.raises(SecretFilesError):
        write.run({"K": VALUE}, read_only=False)
    assert called == []
    assert _left(vault) == []
    assert write.finish() is True  # nothing pending any more


@pytest.mark.parametrize("fails", [False, True])
def test_finish_during_the_write_hands_the_wipe_to_the_write_thread(
    vault: SecretVault, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    write = SecretWrite(vault, "att_1")
    real = SecretVault.materialise
    entered, release = threading.Event(), threading.Event()
    outcome: list[Any] = []

    def slow(self: SecretVault, *args: Any, **kwargs: Any) -> Path:
        entered.set()
        assert release.wait(30)
        folder = real(self, *args, **kwargs)
        assert (folder / "K").read_bytes() == VALUE
        if fails:
            raise OSError(5, "late failure after the files exist")
        return folder

    monkeypatch.setattr(SecretVault, "materialise", slow)

    def body() -> None:
        try:
            outcome.append(write.run({"K": VALUE}, read_only=False))
        except OSError as exc:
            outcome.append(exc)

    thread = threading.Thread(target=body)
    thread.start()
    assert entered.wait(30)
    assert write.finish() is False  # still writing: not ours to wipe yet
    assert _left(vault) == []
    release.set()
    thread.join(30)
    assert not thread.is_alive()
    assert len(outcome) == 1
    assert _left(vault) == []  # the write thread wiped what it wrote
    assert write.finish() is True


def test_the_bound_is_a_setting_with_a_sensible_range() -> None:
    assert ExecutionSettings().secret_write_wait_s == 10.0
    assert ExecutionSettings(secret_write_wait_s=0.1).secret_write_wait_s == 0.1
    assert ExecutionSettings(secret_write_wait_s=120).secret_write_wait_s == 120.0
    for bad in (0, 0.05, -1, 120.5, 3600):
        with pytest.raises(ValidationError):
            ExecutionSettings(secret_write_wait_s=bad)
