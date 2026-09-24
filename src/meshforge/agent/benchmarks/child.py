"""The benchmark child process: ``python -m meshforge.agent.benchmarks.child``.

All measurement runs here, never in the agent process (brief §5g D32b): a broken
CUDA stack can hang or hard-crash an interpreter, BLAS reads its thread count from
the environment **at import time**, and a half-imported native library corrupts a
long-lived process (the exit-139 lesson). The parent (:mod:`.runner`) spawns this
module with the thread environment it wants and drives it over stdin/stdout with
spike S7's line protocol - one JSON object per line in each direction:

.. code-block:: text

    parent -> {"op": "probe"}                  child -> {"ok": true, "info": {...}}
    parent -> {"op": "setup", "spec": {...}}   child -> {"ok": true, "info": {...}}
    parent -> {"op": "rep"}                    child -> {"ok": true, "sample": {...}}
    parent -> {"op": "bye"}                    child exits 0

A failure is answered with ``{"ok": false, "failure_type": ..., "message": ...}`` and
the child stays alive. ``--probe`` prints the capabilities and exits. ``--nice N``
lowers the child's own CPU priority first (POSIX; on Windows the parent starts it
with ``BELOW_NORMAL_PRIORITY_CLASS``) so a saturating benchmark cannot starve the
agent's heartbeats.

**Test-only hooks** (environment, read once at start; never set by the agent):

* ``MESHFORGE_BENCH_DISABLE=torch,numpy`` - make those imports fail in this child,
  so a component test gets a numpy-only (or no-BLAS) run on a machine that has
  torch installed.
* ``MESHFORGE_BENCH_FAULT=<target>:<op>:<action>[,...]`` - misbehave on purpose:
  ``target`` is a spec's ``metric`` or ``kind`` (or ``*``), ``op`` is ``setup`` or
  ``rep``, ``action`` is ``hang``, ``hangtree`` (spawn a grandchild, write its pid to
  ``MESHFORGE_BENCH_FAULT_PIDFILE``, then hang), ``exit`` (``os._exit``) or ``segv``.
  They exist so the runner's kill and crash paths are tested against a real
  process, not a mock (V0.3 checklist item 1).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Final, TextIO

from meshforge.agent.benchmarks.kernels import KINDS, BenchError, capabilities

__all__ = ["DISABLE_ENV", "FAULT_ENV", "FAULT_PIDFILE_ENV", "main", "serve"]

DISABLE_ENV: Final = "MESHFORGE_BENCH_DISABLE"
FAULT_ENV: Final = "MESHFORGE_BENCH_FAULT"
FAULT_PIDFILE_ENV: Final = "MESHFORGE_BENCH_FAULT_PIDFILE"
_FAULT_ACTIONS: Final = frozenset({"hang", "hangtree", "exit", "segv"})
_HANG_S: Final = 3600.0

_priority: dict[str, Any] = {"lowered": False, "nice": None}


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _reply(payload: dict[str, Any], out: TextIO) -> None:
    out.write(json.dumps(payload, allow_nan=False) + "\n")
    out.flush()


# ---------------------------------------------------------------- test hooks
def _disable_backends() -> None:
    for name in os.environ.get(DISABLE_ENV, "").split(","):
        name = name.strip()
        if name:
            # The standard way to make ``import name`` fail (ImportError).
            sys.modules[name] = None  # type: ignore[assignment]


def _faults() -> list[tuple[str, str, str]]:
    faults: list[tuple[str, str, str]] = []
    for item in os.environ.get(FAULT_ENV, "").split(","):
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 3 and parts[2] in _FAULT_ACTIONS:
            faults.append((parts[0], parts[1], parts[2]))
    return faults


def _maybe_fault(op: str, spec: dict[str, Any]) -> None:
    targets = {"*", str(spec.get("metric", "")), str(spec.get("kind", ""))}
    for target, fault_op, action in _faults():
        if fault_op == op and target in targets:
            _misbehave(action)


def _misbehave(action: str) -> None:  # pragma: no cover - runs in a killed child
    if action == "exit":
        os._exit(3)
    if action == "segv":
        if sys.platform == "win32":
            os.abort()
        os.kill(os.getpid(), signal.SIGSEGV)
    if action == "hangtree":
        grandchild = subprocess.Popen(  # noqa: S603 - fixed argv, test-only hook
            [sys.executable, "-c", f"import time; time.sleep({_HANG_S})"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pidfile = os.environ.get(FAULT_PIDFILE_ENV)
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(grandchild.pid))
    deadline = time.monotonic() + _HANG_S
    while time.monotonic() < deadline:
        time.sleep(1.0)


# ------------------------------------------------------------------ protocol
def _dispatch(request: dict[str, Any], bench: Any) -> tuple[dict[str, Any], Any]:
    """Handle one request; returns ``(reply, benchmark)``. Raises :class:`BenchError`."""
    op = str(request.get("op"))
    if op == "setup":
        spec = dict(request.get("spec") or {})
        kind = str(spec.get("kind"))
        if kind not in KINDS:
            raise BenchError("unknown_kind", f"unknown benchmark kind {kind!r}")
        _maybe_fault("setup", spec)
        bench = KINDS[kind](spec)
        bench.spec = spec
        return {"ok": True, "info": bench.setup()}, bench
    if op == "rep":
        if bench is None:
            raise BenchError("not_set_up", "rep before setup")
        _maybe_fault("rep", bench.spec)
        return {"ok": True, "sample": bench.rep()}, bench
    if op == "probe":
        return {"ok": True, "info": {**capabilities(), "priority": _priority}}, bench
    raise BenchError("unknown_op", f"unknown op {op!r}")


def serve(stdin: TextIO, stdout: TextIO) -> int:
    """Read request lines, answer one line each, until EOF or ``bye``."""
    bench: Any = None
    for line in stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except ValueError as exc:
            _reply(
                {"ok": False, "failure_type": "bad_request", "message": _describe(exc)},
                stdout,
            )
            continue
        if not isinstance(request, dict):
            _reply(
                {
                    "ok": False,
                    "failure_type": "bad_request",
                    "message": "request is not a JSON object",
                },
                stdout,
            )
            continue
        if request.get("op") == "bye":
            return 0
        try:
            reply, bench = _dispatch(request, bench)
        except BenchError as exc:
            reply = {"ok": False, "failure_type": exc.failure_type, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - any failure is data, not a crash
            reply = {
                "ok": False,
                "failure_type": "benchmark_error",
                "message": _describe(exc),
            }
        _reply(reply, stdout)
    return 0


def _lower_priority(nice: int) -> None:
    if nice <= 0 or not hasattr(os, "nice"):
        return
    with contextlib.suppress(OSError):
        _priority["nice"] = os.nice(nice)
        _priority["lowered"] = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m meshforge.agent.benchmarks.child",
        description="MeshForge hardware-score child process (driven by the agent).",
    )
    parser.add_argument("--probe", action="store_true", help="print capabilities, exit")
    parser.add_argument(
        "--nice", type=int, default=0, help="lower this process's CPU priority first"
    )
    args = parser.parse_args(argv)
    if sys.platform == "win32" and args.nice > 0:
        # Set by the parent at spawn (BELOW_NORMAL_PRIORITY_CLASS); record it.
        _priority["lowered"] = True
    _lower_priority(args.nice)
    _disable_backends()
    # The protocol owns the real stdout; anything a library prints goes to stderr.
    proto = sys.stdout
    sys.stdout = sys.stderr
    try:
        if args.probe:
            _reply(
                {"ok": True, "info": {**capabilities(), "priority": _priority}}, proto
            )
            return 0
        return serve(sys.stdin, proto)
    finally:
        sys.stdout = proto


if __name__ == "__main__":
    raise SystemExit(main())
