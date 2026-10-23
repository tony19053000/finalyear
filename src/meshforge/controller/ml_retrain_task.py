"""The ML model retrain task (V0.15 G3, D339, D361) - modelled on
:mod:`~meshforge.controller.planner_calibration_task`.

:class:`MlRetrainer` retrains the model set every ``[controller.planner]
ml_retrain_s`` (one day) - the first time one interval after start - and on
demand (:meth:`MlRetrainer.start`, ``POST /v1/planner/ml/train`` / ``compute
planner ml train``). One retrain:

1. **skip when nothing changed** - the D355 dataset watermark (matched runs,
   every ended attempt, run records, feature version, read cap) plus the
   training settings equal those of the newest stored set, or of the last
   retrain this process did (``no_new_runs``);
2. build the G1 dataset (the same builder as ``GET /v1/planner/ml/dataset``)
   and write it to a private temporary directory, with the **reference** -
   what serves now (pinned, else active set) and its artefacts from the
   blob store;
3. run ``python -m meshforge.controller.ml.train fit`` in a **child
   process** (``nice``d where the platform has ``nice``) with a timeout
   (``ml_retrain_timeout_s``; killed when it overruns): training, the
   activation gate, artefacts - never on the event loop, never in the
   controller's memory;
4. store the result (:meth:`~meshforge.controller.ml.registry.ModelRegistry.
   store`: artefacts into the CAS, the set ``active`` or ``rejected`` with
   the gate's reason) and refresh the :class:`~.ml.source.ModelSource`.

**Single flight:** one retrain at a time in this process (an
:class:`asyncio.Lock`; a request while one runs is told ``already_running``)
and across controllers (a PostgreSQL **session advisory lock** held for the
whole retrain - another controller's attempt is ``busy``).

**Failure never matters to dispatch.** A retrain that fails (database down,
child crash, timeout) is logged and counted; the serving set stays what it
was. Without the ``ml`` extra the task logs one ``ml_unavailable`` line at
start and never runs; a manual request is answered ``ml_unavailable``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.logging import get_logger
from meshforge.controller.ml import export, loader
from meshforge.controller.ml.child import (
    ML_TRAIN_LIBRARIES,
    TRAIN_MODULE,
    ChildOutputError,
    ChildRunner,
    describe,
    ml_missing,
    niced,
    read_output,
    run_child,
)
from meshforge.controller.ml.loader import MlLoadError
from meshforge.controller.ml.registry import (
    MlRegistryError,
    ModelRegistry,
    SetRow,
    StoreResult,
)
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.settings import PlannerSettings

__all__ = [
    "ML_TRAIN_LIBRARIES",
    "ChildOutputError",
    "ChildRunner",
    "MlRetrainer",
    "RetrainResult",
    "ml_missing",
    "run_child",
    "train_argv",
]

log = get_logger(__name__)

_MIN_INTERVAL_S: Final = 1.0
_SESSION_LOCK: Final = (
    "SELECT pg_try_advisory_lock(hashtextextended('meshforge.ml_retrain', 0))"
)
_SESSION_UNLOCK: Final = (
    "SELECT pg_advisory_unlock(hashtextextended('meshforge.ml_retrain', 0))"
)


@dataclass(frozen=True, slots=True)
class RetrainResult:
    """What one retrain did: ``row`` None = nothing written (``skipped``)."""

    trigger: str
    started_at: dt.datetime
    finished_at: dt.datetime
    row: SetRow | None = None
    skipped: str | None = None
    """``ml_unavailable`` | ``no_new_runs`` | ``insufficient_data`` |
    ``busy`` | ``same_set`` | ``reference_changed`` (no set written)."""
    error: str | None = None
    """Set when the retrain failed (``timeout``, ``child_failed``,
    ``child_output_invalid``, ``database``, ``crashed``); the serving set is
    unchanged."""
    detail: str | None = None
    superseded: str | None = None
    rows: int | None = None
    seconds: dict[str, Any] = field(default_factory=dict)


def train_argv(
    python: str,
    dataset: Path,
    out: Path,
    reference: Path | None,
    settings: PlannerSettings,
) -> list[str]:
    """The ``fit`` child's command line (``nice``d when available)."""
    argv = [
        python,
        "-m",
        TRAIN_MODULE,
        "fit",
        "--dataset",
        str(dataset),
        "--out",
        str(out),
        "--min-rows",
        str(settings.ml_min_rows),
        "--min-families",
        str(settings.ml_min_families),
        "--min-effect",
        repr(settings.ml_min_effect_log),
        "--max-interval-log",
        repr(settings.ml_max_interval_log),
        "--family-tolerance-log",
        repr(settings.ml_family_tolerance_log),
    ]
    if reference is not None:
        argv += ["--reference", str(reference)]
    return niced(argv)


def _settings_key(settings: PlannerSettings) -> dict[str, Any]:
    """The training settings a stored set records (a change retrains)."""
    return {
        "min_rows": settings.ml_min_rows,
        "min_families": settings.ml_min_families,
        "min_effect_log": settings.ml_min_effect_log,
        "max_interval_log": settings.ml_max_interval_log,
        "family_tolerance_log": settings.ml_family_tolerance_log,
    }


class MlRetrainer:
    """See module docstring. One per controller process."""

    def __init__(
        self,
        engine: AsyncEngine,
        registry: ModelRegistry,
        source: ModelSource,
        settings: PlannerSettings,
        *,
        python: str = sys.executable,
        child: ChildRunner = run_child,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._source = source
        self._settings = settings
        self._python = python
        self._child = child
        self._clock = clock
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[RetrainResult] | None = None
        self._last_key: dict[str, Any] | None = None
        self.missing = ml_missing()
        self.retrains = 0
        self.failures = 0
        self.last: RetrainResult | None = None
        self.running_since: dt.datetime | None = None

    @property
    def available(self) -> bool:
        return not self.missing

    @property
    def running(self) -> bool:
        """A retrain holds the lock, or a requested one has not ended yet."""
        return self._lock.locked() or (self._task is not None and not self._task.done())

    @property
    def interval_s(self) -> float:
        return self._settings.ml_retrain_s

    # ------------------------------------------------------------------ API

    def start(self, trigger: str = "manual") -> bool:
        """Start a retrain in the background (``POST /v1/planner/ml/train``);
        False when one is already running."""
        if self.running:
            return False
        self._task = asyncio.get_running_loop().create_task(
            self.retrain_now(trigger), name="ml-retrain"
        )
        return True

    async def wait(self) -> RetrainResult | None:
        """The background retrain's result (tests; None = none started)."""
        if self._task is None:
            return None
        return await self._task

    def request_stop(self) -> None:
        """End :meth:`run` at its next wait and cancel a manual retrain in
        progress (its child process is killed; nothing is stored). A
        scheduled retrain in progress ends when the controller cancels
        :meth:`run`'s task - the same way."""
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def run(self) -> None:
        """Every ``ml_retrain_s`` until :meth:`request_stop`."""
        if not self.available:
            log.warning(
                "controller.planner.ml_unavailable",
                missing=self.missing,
                hint="install the controller's ml extra to train models",
            )
            return
        interval = max(_MIN_INTERVAL_S, self._settings.ml_retrain_s)
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)
            if self._stop.is_set():
                return
            await self.retrain_now("scheduled")

    # ------------------------------------------------------------------ work

    async def retrain_now(self, trigger: str = "manual") -> RetrainResult:
        """One retrain (module docstring). Never raises for a failure: the
        result says what happened."""
        started = self._clock()
        if not self.available:
            return self._done(
                RetrainResult(trigger, started, self._clock(), skipped="ml_unavailable")
            )
        if self._lock.locked():
            return RetrainResult(
                trigger, started, self._clock(), skipped="already_running"
            )
        async with self._lock:
            self.running_since = started
            try:
                result = await self._locked(trigger, started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception(
                    "controller.planner.ml_retrain_crashed",
                    error_type=type(exc).__name__,
                )
                result = RetrainResult(
                    trigger,
                    started,
                    self._clock(),
                    error="crashed",
                    detail=type(exc).__name__,
                )
            finally:
                self.running_since = None
        return self._done(result)

    def _done(self, result: RetrainResult) -> RetrainResult:
        self.retrains += 1
        self.last = result
        if result.error is not None:
            self.failures += 1
            log.warning(
                "controller.planner.ml_retrain_failed",
                trigger=result.trigger,
                error=result.error,
                detail=result.detail,
                failures=self.failures,
            )
        elif result.row is None:
            log.info(
                "controller.planner.ml_retrain_skipped",
                trigger=result.trigger,
                reason=result.skipped,
            )
        else:
            log.info(
                "controller.planner.ml_retrained",
                trigger=result.trigger,
                ml_set_version=result.row.set_version,
                status=result.row.status,
                reason=result.row.reason,
                superseded_set=result.superseded,
                rows=result.rows,
                seconds=result.seconds.get("total_s"),
            )
        return result

    async def _locked(self, trigger: str, started: dt.datetime) -> RetrainResult:
        try:
            async with self._engine.connect() as conn:
                got = (await conn.execute(text(_SESSION_LOCK))).scalar()
                await conn.commit()
                if not got:
                    return RetrainResult(
                        trigger, started, self._clock(), skipped="busy"
                    )
                try:
                    return await self._retrain(trigger, started)
                finally:
                    with contextlib.suppress(Exception):
                        await conn.execute(text(_SESSION_UNLOCK))
                        await conn.commit()
        except (MlLoadError, MlRegistryError) as exc:
            return RetrainResult(
                trigger,
                started,
                self._clock(),
                error="database",
                detail=str(exc.details.get("cause")),
            )
        except (SQLAlchemyError, OSError) as exc:  # the session lock's connection
            return RetrainResult(
                trigger,
                started,
                self._clock(),
                error="database",
                detail=type(exc).__name__,
            )

    async def _key(self) -> dict[str, Any]:
        mark = await loader.read_watermark(
            self._engine, max_runs=self._settings.ml_dataset_max_runs
        )
        return {"watermark": mark.as_json(), "train": _settings_key(self._settings)}

    async def _unchanged(self, key: Mapping[str, Any]) -> bool:
        if self._last_key == key:
            return True
        newest = await self._registry.newest()
        if newest is None:
            return False
        stored = newest.inputs
        return bool(
            stored.get("watermark") == key["watermark"]
            and stored.get("train") == key["train"]
        )

    async def _write_reference(self, directory: Path) -> str | None:
        """What serves now, into ``directory``; its set version (None =
        nothing serves: the gate judges against the calibration)."""
        kind, row = await self._registry.serving()
        if row is None:
            return None
        await asyncio.to_thread(directory.mkdir)
        members = []
        for member in row.members:
            entry = {
                "term": member.term,
                "algorithm": member.algorithm,
                "version": member.version,
                "file": None,
            }
            if member.learned:
                name = member.version.split(":", 1)[1] + ".art"
                try:
                    data = await self._registry.artefact(member)
                except Exception:  # noqa: BLE001 - judged against calibration
                    data = None
                if data is not None:
                    await asyncio.to_thread((directory / name).write_bytes, data)
                entry["file"] = name
            members.append(entry)
        doc = {"kind": kind, "set_version": row.set_version, "members": members}
        await asyncio.to_thread(
            (directory / "reference.json").write_text, json.dumps(doc), "utf-8"
        )
        return row.set_version

    async def _retrain(self, trigger: str, started: dt.datetime) -> RetrainResult:
        key = await self._key()
        if await self._unchanged(key):
            return RetrainResult(trigger, started, self._clock(), skipped="no_new_runs")
        built = await export.build_dataset(
            self._engine, max_runs=self._settings.ml_dataset_max_runs
        )
        with tempfile.TemporaryDirectory(prefix="meshforge-ml-retrain-") as scratch:
            work = Path(scratch)
            dataset = work / "dataset.parquet"
            await asyncio.to_thread(dataset.write_bytes, built.dataset.parquet)
            reference_version = await self._write_reference(work / "reference")
            out = work / "out"
            argv = train_argv(
                self._python,
                dataset,
                out,
                None if reference_version is None else work / "reference",
                self._settings,
            )
            try:
                code, stderr = await self._child(
                    argv, self._settings.ml_retrain_timeout_s
                )
            except TimeoutError:
                return RetrainResult(
                    trigger,
                    started,
                    self._clock(),
                    error="timeout",
                    detail=f"killed after {self._settings.ml_retrain_timeout_s:g} s",
                )
            if code != 0:
                return RetrainResult(
                    trigger,
                    started,
                    self._clock(),
                    error="child_failed",
                    detail=f"exit {code}: {stderr.splitlines()[-1] if stderr else ''}"[
                        :500
                    ],
                )
            try:
                doc, artefacts, timings = await asyncio.to_thread(_read_output, out)
            except ChildOutputError as exc:
                return self._invalid_output(trigger, started, str(exc))
        rows = int(doc.get("rows") or 0)
        if doc.get("status") == "insufficient_data":
            self._last_key = key
            return RetrainResult(
                trigger,
                started,
                self._clock(),
                skipped="insufficient_data",
                rows=rows,
                seconds=timings,
            )
        inputs = {
            **key,
            "trigger": trigger,
            "dataset": {
                "digest": built.dataset.digest,
                "rows": len(built.dataset.rows),
                "truncated": built.truncated,
                "counts": dict(built.dataset.counts),
            },
            "seconds": timings,
            "libraries": doc.get("libraries", {}),
        }
        try:
            stored: StoreResult = await self._registry.store(
                doc, artefacts, reference_version=reference_version, inputs=inputs
            )
        except (KeyError, TypeError, ValueError) as exc:
            # reviewer #4: a set.json that does not match its artefacts or
            # lacks a field is the child's fault, not a crash of this task
            return self._invalid_output(trigger, started, _describe(exc))
        except MlRegistryError as exc:
            if exc.retryable:
                raise
            # fix r2 (N1): the database refused a value of the document
            return self._invalid_output(
                trigger, started, f"refused by the database: {exc.details.get('cause')}"
            )
        if stored.skipped != "reference_changed":
            self._last_key = key
        await self._source.refresh()
        return RetrainResult(
            trigger,
            started,
            self._clock(),
            row=stored.row,
            skipped=stored.skipped,
            superseded=stored.superseded,
            rows=rows,
            seconds=timings,
        )

    def _invalid_output(
        self, trigger: str, started: dt.datetime, detail: str
    ) -> RetrainResult:
        return RetrainResult(
            trigger,
            started,
            self._clock(),
            error="child_output_invalid",
            detail=detail[:500],
        )


_read_output = read_output
"""G3 name (tests)."""
_describe = describe
