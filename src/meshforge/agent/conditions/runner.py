"""The agent's benchmark-condition runner (V0.9 G4, roadmap 0.9.5, D217/D241).

One :class:`ConditionRunner` per agent. The executor hands it an attempt's
``StartAttempt.condition``; the runner

1. **re-validates** it (:func:`~meshforge.common.bench_conditions.parse_condition`,
   the canonical form must match) and refuses a kind this node did not
   advertise (:attr:`ConditionRunner.kinds`) - a typed
   :class:`~.base.ConditionFailedError`, nothing applied;
2. **applies and verifies** it (:meth:`ActiveCondition.apply`, before the
   attempt stages; the controllers in :mod:`.load`, :mod:`.netem`,
   :mod:`.cache`), writing the marker (:mod:`.marker`) before every change;
3. lets the executor run the attempt (``after_stage`` re-checks cache
   conditions once the inputs are staged);
4. **reverts** it when the attempt ends - :meth:`ActiveCondition.finish`, called
   by the executor right before it sends the terminal status, on every path
   (done, failed, cancelled, agent stop) - and returns the
   ``ConditionReport`` (verified, measured JSON, reason, reverted).

A failed apply/verify reverts what was made and fails the attempt
``config_error`` ``condition_failed:<kind>: <why>``, never retried (D217:
an unverified condition is never labelled). A condition that stops holding
while the job runs (a load process died) is reported ``verified = false``
(the run record's failure class becomes ``condition_failed``).

**Crash safety.** :meth:`ConditionRunner.sweep` (agent start) undoes what the
marker lists; :meth:`ConditionRunner.close` (agent exit) reverts everything
still active; :meth:`ConditionRunner.stop_all` is the hook G5's owner
``pause benchmarks`` switch calls - it stops every load at once and marks the
conditions broken.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import psutil  # type: ignore[import-untyped]

from meshforge.agent.conditions.base import ConditionController, ConditionFailedError
from meshforge.agent.conditions.cache import CacheController
from meshforge.agent.conditions.load import (
    CpuLoadController,
    GpuLoadController,
    UtilReader,
)
from meshforge.agent.conditions.marker import BURNER_MODULE, ConditionMarker
from meshforge.agent.conditions.netem import (
    NetemController,
    TcError,
    TcRunner,
    delete_netem,
)
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import (
    CacheCondition,
    ConditionError,
    ConditionKind,
    CpuLoad,
    GpuLoad,
    Netem,
    capability_tag,
    parse_condition,
)
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "CONDITIONS_DIR",
    "LOAD_SLACK_S",
    "MAX_MEASURED_CHARS",
    "ActiveCondition",
    "ConditionRunner",
    "probe_kinds",
]

log = get_logger(__name__)

CONDITIONS_DIR: Final = "conditions"
LOAD_SLACK_S: Final = 30.0
"""A load generator outlives the attempt's own time limit by at most this
(+ ``setup_allowance_s``, the staging that comes first)."""
MAX_MEASURED_CHARS: Final = 2048
"""``ConditionReport.measured_json`` bound (the controller refuses more)."""


def _measured_json(measured: Mapping[str, Any]) -> str:
    text = json.dumps(
        measured, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
    )
    return text if len(text) <= MAX_MEASURED_CHARS else json.dumps({"truncated": True})


class ActiveCondition:
    """One attempt's condition, from apply to revert (see module docstring)."""

    def __init__(
        self,
        runner: ConditionRunner,
        attempt_id: str,
        spec: str,
        controller: ConditionController | None,
        *,
        failure: ConditionFailedError | None = None,
    ) -> None:
        self._runner = runner
        self.attempt_id = attempt_id
        self.spec = spec
        self.controller = controller
        self.failure = failure
        """Set when the condition could not be applied / verified."""
        self.broken: str | None = None
        self._applied = False
        self._report: agent_pb2.ConditionReport | None = None
        self._finishing: asyncio.Future[agent_pb2.ConditionReport] | None = None

    @property
    def kind(self) -> str:
        return self.spec.partition(":")[0]

    def remember(self) -> None:
        """Write this condition's marker entry (sync, a small file)."""
        if self.controller is not None:
            self._runner.marker.put(
                self.attempt_id,
                {"spec": self.spec, **self.controller.marker_entry()},
            )

    async def apply(self) -> None:
        """Apply and verify. Raises :class:`ConditionFailedError` (what was
        made is reverted by :meth:`finish`)."""
        if self.failure is not None:
            raise self.failure
        if self.controller is None:  # pragma: no cover - prepare sets one of them
            raise ConditionFailedError(self.kind, "no controller")
        self._applied = True
        await asyncio.to_thread(self.remember)  # before any change
        try:
            await self.controller.apply()
        except ConditionFailedError as exc:
            self.failure = exc
            raise
        except Exception as exc:  # a controller bug: never a hung attempt
            log.exception(
                "agent.conditions.apply_crashed",
                attempt_id=self.attempt_id,
                kind=self.kind,
            )
            self.failure = ConditionFailedError(
                self.kind, f"apply failed ({type(exc).__name__})"
            )
            raise self.failure from exc
        finally:
            await asyncio.to_thread(self.remember)  # now with pids / interface
        log.info(
            "agent.conditions.applied",
            attempt_id=self.attempt_id,
            condition=self.spec,
            measured=self.controller.measured,
        )

    def after_stage(self, stages: Sequence[Any]) -> None:
        """Re-check once the inputs are staged. Raises :class:`ConditionFailedError`."""
        if self.controller is None or self.failure is not None:
            return
        try:
            self.controller.after_stage(stages)
        except ConditionFailedError as exc:
            self.failure = exc
            raise

    def mark_broken(self, reason: str) -> None:
        if self.broken is None:
            self.broken = reason

    async def finish(self) -> agent_pb2.ConditionReport:
        """Revert (idempotent; concurrent callers share one revert) and the
        report for the terminal status."""
        if self._report is not None:
            return self._report
        if self._finishing is None:
            self._finishing = asyncio.ensure_future(self._finish())
        return await asyncio.shield(self._finishing)

    async def _finish(self) -> agent_pb2.ConditionReport:
        controller = self.controller
        reverted = True
        if controller is not None and self._applied:
            if self.failure is None and self.broken is None:
                self.broken = controller.broken()
            try:
                reverted = await controller.revert()
            except Exception:  # logged; the marker keeps it for the sweep
                log.exception(
                    "agent.conditions.revert_crashed", attempt_id=self.attempt_id
                )
                reverted = False
            if reverted:
                await asyncio.to_thread(self._runner.marker.remove, self.attempt_id)
        self._runner.forget(self)
        measured = {} if controller is None else dict(controller.measured)
        reason = ""
        if self.failure is not None:
            reason = self.failure.reason
        elif self.broken is not None:
            reason = self.broken
        report = agent_pb2.ConditionReport(
            spec=self.spec,
            verified=self.failure is None and self.broken is None and self._applied,
            measured_json=_measured_json(measured) if measured else "",
            reason=reason[:512],
            reverted=reverted,
        )
        log.info(
            "agent.conditions.finished",
            attempt_id=self.attempt_id,
            condition=self.spec,
            verified=report.verified,
            reverted=reverted,
            reason=reason[:200] or None,
        )
        self._report = report
        return report


async def probe_kinds(
    settings: ConditionSettings,
    *,
    has_gpu: bool,
    platform: str = sys.platform,
    tc: TcRunner | None = None,
) -> frozenset[ConditionKind]:
    """What this node can do (advertised at start): ``cpu_load`` and ``cache``
    always; ``gpu_load`` with an NVIDIA GPU and torch installed; ``netem`` on
    Linux when enabled and a dry run of ``tc qdisc show`` succeeds."""
    if not settings.enabled:
        return frozenset()
    kinds = {ConditionKind.CPU_LOAD, ConditionKind.CACHE}
    if has_gpu and importlib.util.find_spec("torch") is not None:
        kinds.add(ConditionKind.GPU_LOAD)
    if settings.netem and platform.startswith("linux"):
        runner = tc or TcRunner(settings.tc_command)
        try:
            await runner.show(settings.netem_interface)
        except TcError as exc:
            log.warning(
                "agent.conditions.netem_unavailable",
                interface=settings.netem_interface,
                cause=str(exc)[:200],
            )
        else:
            kinds.add(ConditionKind.NETEM)
    return frozenset(kinds)


class ConditionRunner:
    """See module docstring. Lives on the agent's event loop."""

    def __init__(
        self,
        settings: ConditionSettings,
        *,
        data_dir: Path,
        kinds: Collection[ConditionKind],
        store: CasStore | None = None,
        index: Callable[[], GcIndex | None] = lambda: None,
        gpu_util: UtilReader = lambda _index: None,
        controller: Callable[[], tuple[str, int] | None] = lambda: None,
        tc: TcRunner | None = None,
    ) -> None:
        self._settings = settings
        self.kinds = frozenset(kinds)
        self.marker = ConditionMarker(data_dir / CONDITIONS_DIR)
        self._store = store
        self._index = index
        self._gpu_util = gpu_util
        self._controller = controller
        self._tc = tc or TcRunner(settings.tc_command)
        self._active: dict[str, ActiveCondition] = {}

    @property
    def capability(self) -> str | None:
        """The ``bench-conditions:...`` tag (None = nothing to advertise)."""
        return capability_tag(self.kinds)

    @property
    def active(self) -> list[ActiveCondition]:
        return list(self._active.values())

    def forget(self, active: ActiveCondition) -> None:
        if self._active.get(active.attempt_id) is active:
            del self._active[active.attempt_id]

    def prepare(
        self,
        attempt_id: str,
        spec: str,
        *,
        timeout_s: float,
        gpu_indices: Sequence[int] = (),
        tree_ids: Sequence[str] = (),
    ) -> ActiveCondition:
        """The attempt's condition, not yet applied (never raises: a refusal
        is an :class:`ActiveCondition` whose :meth:`~ActiveCondition.apply`
        raises, so the executor reports it like any other failure)."""
        kind = spec.partition(":")[0][:32] or "condition"
        try:
            condition = parse_condition(spec)
        except ConditionError as exc:
            return self._refused(attempt_id, spec, kind, str(exc))
        if condition.canonical() != spec:
            return self._refused(
                attempt_id, spec, kind, "the condition is not in its canonical form"
            )
        if condition.kind not in self.kinds:
            return self._refused(
                attempt_id,
                spec,
                kind,
                f"this node does not offer {condition.kind.value}",
            )
        deadline_s = float(timeout_s) + LOAD_SLACK_S + self._settings.setup_allowance_s
        window_s = self._settings.verify_window_s
        holder: list[ActiveCondition] = []

        def on_change() -> None:
            # every new burner pid is in the marker at once (crash safety)
            if holder:
                holder[0].remember()

        controller: ConditionController
        if isinstance(condition, CpuLoad):
            controller = CpuLoadController(
                condition.cores,
                deadline_s=deadline_s,
                window_s=window_s,
                on_change=on_change,
            )
        elif isinstance(condition, GpuLoad):
            controller = GpuLoadController(
                condition.percent,
                gpu_index=int(gpu_indices[0]) if gpu_indices else 0,
                deadline_s=deadline_s,
                window_s=window_s,
                util=self._gpu_util,
                on_change=on_change,
            )
        elif isinstance(condition, Netem):
            controller = NetemController(
                condition,
                tc=self._tc,
                interface=self._settings.netem_interface,
                controller=self._controller,
            )
        elif isinstance(condition, CacheCondition) and self._store is not None:
            controller = CacheController(
                condition.mode,
                store=self._store,
                index=self._index(),
                tree_ids=tree_ids,
            )
        else:  # a controller-side kind, or cache without a CAS
            return self._refused(
                attempt_id,
                spec,
                kind,
                f"{condition.kind.value} is not applied by the agent",
            )
        active = ActiveCondition(self, attempt_id, spec, controller)
        holder.append(active)
        self._active[attempt_id] = active
        return active

    def _refused(
        self, attempt_id: str, spec: str, kind: str, reason: str
    ) -> ActiveCondition:
        log.warning(
            "agent.conditions.refused", attempt_id=attempt_id, kind=kind, reason=reason
        )
        return ActiveCondition(
            self,
            attempt_id,
            spec[:64],
            None,
            failure=ConditionFailedError(kind, reason),
        )

    async def sweep(self) -> int:
        """Agent start: undo what a previous process left applied (marker).
        The entries undone."""
        entries = await asyncio.to_thread(self.marker.load)
        undone = 0
        for attempt_id, entry in entries.items():
            pids = entry.get("pids") or []
            killed = await asyncio.to_thread(_kill_burners, pids)
            iface = entry.get("netem")
            netem_ok = True
            if isinstance(iface, str) and iface:
                cause = await delete_netem(self._tc, iface)
                netem_ok = cause is None
                if cause is not None:
                    log.error(
                        "agent.conditions.sweep_netem_failed",
                        interface=iface,
                        cause=cause,
                    )
            log.warning(
                "agent.conditions.swept",
                attempt_id=attempt_id,
                condition=str(entry.get("spec", ""))[:64],
                killed=killed,
                netem=iface if isinstance(iface, str) else None,
            )
            if netem_ok:
                undone += 1
                await asyncio.to_thread(self.marker.remove, attempt_id)
        return undone

    async def stop_all(self, reason: str) -> int:
        """Revert every active condition now (G5 owner ``pause benchmarks``
        hook, and agent exit); each is reported broken with ``reason``."""
        active = list(self._active.values())
        for item in active:
            item.mark_broken(reason)
        await asyncio.gather(
            *(item.finish() for item in active), return_exceptions=True
        )
        return len(active)

    async def close(self) -> None:
        await self.stop_all("agent_shutdown")


def _kill_burners(pids: object) -> int:
    """Kill the recorded burners that still exist **as themselves** (same pid,
    same creation time, our module in the command line)."""

    killed = 0
    if not isinstance(pids, list):
        return 0
    for item in pids:
        if not (isinstance(item, list) and len(item) == 2):
            continue
        pid, created = item
        if not isinstance(pid, int) or not isinstance(created, int | float):
            continue
        with contextlib.suppress(psutil.Error):
            proc = psutil.Process(pid)
            if abs(proc.create_time() - float(created)) > 1.0:
                continue
            if BURNER_MODULE not in " ".join(proc.cmdline()):
                continue
            proc.kill()
            killed += 1
    return killed
