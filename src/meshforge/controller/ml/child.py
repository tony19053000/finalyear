"""The ML training child process, seen from the controller (V0.15 G3 D361;
shared by the retrain task and, V0.15 G5a D368, the evaluator's fold
models).

Training never runs in the controller's process or on its event loop: it
runs ``python -m meshforge.controller.ml.train fit ...`` as a child
(``nice``d where the platform has ``nice``) under a timeout, and reads back
what the child wrote (``set.json`` + one ``<hex>.art`` per learned member).

* :func:`ml_missing` - what the child needs that is not installed
  (``find_spec``, nothing imported);
* :func:`run_child` - run it; ``(exit code, stderr tail)``; ``TimeoutError``
  after killing a child that overran (a cancelled caller kills it too);
* :func:`read_output` - its ``set.json``, artefact bytes and timings, or
  :class:`ChildOutputError` (exit 0 but unreadable output).

Moved out of :mod:`meshforge.controller.ml_retrain_task` in G5a (that module
re-exports the names): two callers now.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ML_TRAIN_LIBRARIES",
    "TRAIN_MODULE",
    "ChildOutputError",
    "ChildRunner",
    "describe",
    "ml_missing",
    "niced",
    "read_output",
    "run_child",
]

ML_TRAIN_LIBRARIES: Final = ("numpy", "pyarrow", "sklearn", "lightgbm", "xgboost")
"""What the ``fit`` child imports (the ``controller`` + ``ml`` extras)."""
TRAIN_MODULE: Final = "meshforge.controller.ml.train"
_NICE: Final = 10
_STDERR_TAIL: Final = 2000

type ChildRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, str]]]


def ml_missing() -> list[str]:
    """The :data:`ML_TRAIN_LIBRARIES` that are missing (``find_spec``,
    nothing imported)."""
    return [m for m in ML_TRAIN_LIBRARIES if importlib.util.find_spec(m) is None]


def niced(argv: list[str]) -> list[str]:
    """``argv`` run under ``nice -n 10`` where the platform has it."""
    nice = shutil.which("nice") if os.name == "posix" else None
    return [nice, "-n", str(_NICE), *argv] if nice else argv


async def run_child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
    """Run ``argv``; ``(exit code, stderr tail)``. ``TimeoutError`` after
    killing a child that overran."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    tail = (stderr or b"").decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
    return int(proc.returncode or 0), tail


class ChildOutputError(Exception):
    """The ``fit`` child exited 0 but its output cannot be read (reviewer #4):
    a missing / malformed ``set.json``, a member without its fields, an
    artefact file that is not there. Message = a short description."""


def describe(exc: BaseException) -> str:
    """A short, log-safe description of a read failure."""
    if isinstance(exc, KeyError):
        return f"missing field {exc.args[0]!r}" if exc.args else "missing field"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _object(value: Any, name: str = "set") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name}.json is not an object")
    return value


def read_output(out: Path) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    """The child's ``set.json``, its artefacts and timings;
    :class:`ChildOutputError` when they cannot be read."""
    try:
        doc = _object(json.loads((out / "set.json").read_text(encoding="utf-8")))
        artefacts: dict[str, bytes] = {}
        for member in doc.get("members", []):
            name = member.get("artefact")
            if name:
                version = member["version"]
                artefacts[version] = (out / str(name)).read_bytes()
        timings_path = out / "timings.json"
        timings = (
            _object(json.loads(timings_path.read_text(encoding="utf-8")), "timings")
            if timings_path.exists()
            else {}
        )
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ChildOutputError(describe(exc)) from exc
    return doc, artefacts, timings
