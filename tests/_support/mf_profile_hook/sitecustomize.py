"""V1.0 G4 (D412) profiling hook - TEST/LAB ONLY, never shipped.

Put this folder first on ``PYTHONPATH`` of a fabric's processes and set
``MF_PROFILE_DIR``; Python imports ``sitecustomize`` at start-up, so every
controller / agent process then:

* answers ``SIGUSR2`` by writing ``<dir>/<role>-<pid>-<n>.json``: RSS, threads
  by name, live objects per type (``gc``), asyncio tasks per coroutine (the
  loop running in the main thread), and - when ``PYTHONTRACEMALLOC`` is set -
  the top allocation sites plus a ``.tracemalloc`` snapshot file for
  ``Snapshot.compare_to``;
* (controller) times every dispatch pass (``DispatchSteps._dispatch``), every
  ``ExecutionStore.dispatch_planned`` and every planner ``decide`` call -
  including the ``Wait`` answers that write no decision row - and puts the
  numbers into the same file.

Nothing here changes behaviour: the wrappers call the original and only add
``time.perf_counter`` around it. Without ``MF_PROFILE_DIR`` this file does
nothing (the import is a no-op).
"""

from __future__ import annotations

import os
import sys

_DIR = os.environ.get("MF_PROFILE_DIR")


def _role() -> str:
    argv = " ".join(getattr(sys, "orig_argv", sys.argv))
    if "meshforge.controller" in argv:
        return "controller"
    if "meshforge.agent" in argv:
        return "agent"
    return "other"


if _DIR:
    import collections
    import gc
    import importlib.abc
    import importlib.machinery
    import json
    import signal
    import threading
    import time
    import tracemalloc
    from typing import Any

    _ROLE = _role()
    _TIMINGS: dict[str, collections.deque[float]] = {
        "dispatch_pass_ms": collections.deque(maxlen=200_000),
        "dispatch_planned_ms": collections.deque(maxlen=200_000),
        "decide_ms": collections.deque(maxlen=500_000),
    }
    _SEQ = [0]

    def _rss_bytes() -> int:
        try:
            with open("/proc/self/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except OSError:
            return -1

    def _tasks() -> dict[str, int]:
        import asyncio

        loop = asyncio.events._get_running_loop()  # noqa: SLF001 - main thread's loop
        if loop is None:
            return {}
        names: collections.Counter[str] = collections.Counter()
        for task in asyncio.all_tasks(loop):
            coro = task.get_coro()
            names[getattr(coro, "__qualname__", type(coro).__name__)] += 1
        return dict(names.most_common())

    def _dump(signum: int, frame: object) -> None:
        _SEQ[0] += 1
        stem = os.path.join(_DIR, f"{_ROLE}-{os.getpid()}-{_SEQ[0]:04d}")
        types: collections.Counter[str] = collections.Counter(
            type(o).__qualname__ for o in gc.get_objects()
        )
        doc: dict[str, Any] = {
            "role": _ROLE,
            "pid": os.getpid(),
            "seq": _SEQ[0],
            "at": time.time(),
            "rss_bytes": _rss_bytes(),
            "threads": dict(
                collections.Counter(
                    t.name.rsplit("_", 1)[0] for t in threading.enumerate()
                )
            ),
            "gc_objects": sum(types.values()),
            "gc_top": dict(types.most_common(80)),
            "tasks": _tasks(),
            "timings": {k: list(v) for k, v in _TIMINGS.items() if v},
        }
        if tracemalloc.is_tracing():
            snap = tracemalloc.take_snapshot()
            snap.dump(stem + ".tracemalloc")
            doc["tracemalloc_traced_bytes"] = tracemalloc.get_traced_memory()[0]
            doc["tracemalloc_top"] = [
                {"site": str(s.traceback), "bytes": s.size, "count": s.count}
                for s in snap.statistics("lineno")[:40]
            ]
        tmp = stem + ".json.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, stem + ".json")

    def _timed_sync(fn: Any, key: str) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                _TIMINGS[key].append((time.perf_counter() - started) * 1000.0)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    def _timed_async(fn: Any, key: str) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                _TIMINGS[key].append((time.perf_counter() - started) * 1000.0)

        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    def _patch(name: str, module: Any) -> None:
        if name == "meshforge.controller.jobs.dispatch":
            cls = module.DispatchSteps
            cls._dispatch = _timed_async(cls._dispatch, "dispatch_pass_ms")
        elif name == "meshforge.controller.jobs.execution_store":
            cls = module.ExecutionStore
            cls.dispatch_planned = _timed_async(
                cls.dispatch_planned, "dispatch_planned_ms"
            )
        elif name == "meshforge.controller.jobs.planning":
            cls = module.DispatchPlanner
            cls.decide = _timed_sync(cls.decide, "decide_ms")

    _WANTED = {
        "meshforge.controller.jobs.dispatch",
        "meshforge.controller.jobs.execution_store",
        "meshforge.controller.jobs.planning",
    }

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
            if name not in _WANTED:
                return None
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader
            original = loader.exec_module

            def exec_module(module: Any) -> None:
                original(module)
                _patch(name, module)

            loader.exec_module = exec_module  # type: ignore[method-assign]
            return spec

    if _ROLE in ("controller", "agent"):
        os.makedirs(_DIR, exist_ok=True)
        signal.signal(signal.SIGUSR2, _dump)
        if _ROLE == "controller":
            sys.meta_path.insert(0, _Finder())
        if os.environ.get("MF_PROFILE_TRACEMALLOC") == _ROLE:
            tracemalloc.start(int(os.environ.get("MF_PROFILE_TRACEMALLOC_FRAMES", "8")))
