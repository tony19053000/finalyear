"""When scoring runs (brief §5g D34): on demand, and once in the agent's background.

* :func:`score_and_cache` - one locked run: fingerprint -> ``scores.lock`` ->
  :class:`~.runner.ScoreRunner` -> ``scores.json``. Shared by ``meshforge-agent
  hardware score`` (through ``asyncio.run``) and the agent.
* :class:`ScoreService` - the agent's single background task (behind
  ``[agent.scores] enabled``). After the **first ONLINE of the process** it checks
  the cache; if it is missing, corrupt or stale it waits ``start_delay_s`` and
  scores once (never before ``Hello``, never delaying connect - criterion 3). Then
  it polls the cache's mtime every ``cache_poll_s`` so a run made by the CLI is
  picked up without a restart. It never re-scores periodically.

The "report" step is one injectable callback. In G7-A it only logs
``agent.scores.ready`` (run_id, fingerprint, per-metric statuses - no numbers);
G7-B replaces it with ``send_report``. The task owns all of its state (single
writer); on agent shutdown it is cancelled and the runner kills the child tree.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from meshforge.agent.benchmarks.cache import (
    CacheState,
    ScorerBusyError,
    backend_versions,
    cache_mtime_ns,
    fingerprint,
    fingerprint_inputs,
    load_cache,
    save_cache,
    scorer_lock,
)
from meshforge.agent.benchmarks.model import ScoreSet
from meshforge.agent.benchmarks.plan import GpuRef
from meshforge.agent.benchmarks.runner import HostFacts, RunnerConfig, ScoreRunner
from meshforge.agent.hardware.summary import HardwareFacts
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import ScoreSettings
from meshforge.common.logging import get_logger

__all__ = [
    "CacheWriteError",
    "ScoreService",
    "SessionView",
    "current_fingerprint",
    "host_facts",
    "runner_config",
    "score_and_cache",
]

log = get_logger(__name__)

_ONLINE_WAIT_S = 3600.0

type Report = Callable[[ScoreSet], Awaitable[None]]
type RunnerFactory = Callable[..., ScoreRunner]


class CacheWriteError(Exception):
    """The run finished but ``scores.json`` could not be written; ``scores`` is kept."""

    def __init__(self, scores: ScoreSet, cause: OSError) -> None:
        super().__init__(f"could not write scores.json: {cause.strerror or cause}")
        self.scores = scores


def host_facts(facts: HardwareFacts) -> HostFacts:
    return HostFacts(
        cpu_threads=max(1, facts.cpu.logical_threads),
        gpus=tuple(GpuRef(g.index, g.model) for g in facts.gpus),
    )


def runner_config(settings: ScoreSettings) -> RunnerConfig:
    return RunnerConfig(
        budget_s=settings.budget_s, case_timeout_s=settings.case_timeout_s
    )


def current_fingerprint(
    facts: HardwareFacts,
    backends: Callable[[], dict[str, str | None]] = backend_versions,
) -> tuple[str, dict[str, Any]]:
    inputs = fingerprint_inputs(facts, backends())
    return fingerprint(inputs), inputs


async def score_and_cache(
    data_dir: Path,
    facts: HardwareFacts,
    config: RunnerConfig,
    *,
    runner_factory: RunnerFactory | None = None,
    backends: Callable[[], dict[str, str | None]] = backend_versions,
    log_fields: dict[str, Any] | None = None,
) -> ScoreSet:
    """Measure under ``scores.lock`` and write ``scores.json``.

    Raises :class:`~.cache.ScorerBusyError` (another scorer holds the lock),
    :class:`CacheWriteError` (measured, not saved), ``OSError`` (the data dir or lock
    file cannot be opened) and ``CancelledError``. Measurement problems are never
    raised: they are metric statuses in the returned set.
    """
    fp, inputs = current_fingerprint(facts, backends)
    factory = runner_factory or ScoreRunner
    with scorer_lock(data_dir):
        runner = factory(
            config, host_facts(facts), fingerprint=fp, log_fields=log_fields
        )
        scores = await runner.run()
        try:
            await asyncio.to_thread(save_cache, data_dir, scores, inputs)
        except OSError as exc:
            raise CacheWriteError(scores, exc) from exc
    return scores


class SessionView(Protocol):
    """What the service needs from ``AgentSessionClient`` (and tests fake)."""

    state: AgentState
    node_id: str
    session_id: str

    async def wait_for_state(
        self, *states: AgentState, timeout_s: float
    ) -> AgentState: ...


class ScoreService:
    """The agent's background scoring task; see the module docstring."""

    def __init__(
        self,
        settings: ScoreSettings,
        data_dir: Path,
        session: SessionView,
        facts: HardwareFacts | None,
        *,
        report: Report | None = None,
        runner_factory: RunnerFactory | None = None,
        config: RunnerConfig | None = None,
        backends: Callable[[], dict[str, str | None]] = backend_versions,
    ) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self._session = session
        self._facts = facts
        self._report = report or self._log_ready
        self._runner_factory = runner_factory
        self._config = config or runner_config(settings)
        self._backends = backends
        self._fingerprint = ""
        self._last_mtime: int | None = None
        self._last_reported: str | None = None
        self.runs = 0
        """Scoring runs this service started (tests)."""

    def _fields(self) -> dict[str, Any]:
        return {
            "node_id": self._session.node_id,
            "session_id": self._session.session_id or None,
        }

    async def run(self) -> None:
        """Wait for ONLINE, score once if needed, then poll the cache forever."""
        facts = self._facts
        if facts is None:
            log.warning(
                "agent.scores.skipped",
                **self._fields(),
                reason="hardware discovery failed: no fingerprint, no GPU list",
            )
            return
        await self._wait_first_online()
        self._fingerprint, _ = current_fingerprint(facts, self._backends)
        state = await self._load()
        if state.valid:
            assert state.scores is not None  # noqa: S101 - VALID implies scores
            log.info(
                "agent.scores.cache_valid",
                **self._fields(),
                run_id=state.scores.run_id,
                measured_at=state.scores.measured_at.isoformat(),
            )
            self._last_reported = state.scores.run_id
            self._last_mtime = state.mtime_ns
        else:
            await self._score_once(facts, state)
        await self._poll()

    async def _wait_first_online(self) -> None:
        while self._session.state is not AgentState.ONLINE:
            with contextlib.suppress(TimeoutError):
                await self._session.wait_for_state(
                    AgentState.ONLINE, timeout_s=_ONLINE_WAIT_S
                )

    async def _load(self) -> CacheState:
        return await asyncio.to_thread(
            load_cache, self.data_dir, self._fingerprint, **self._fields()
        )

    async def _score_once(self, facts: HardwareFacts, state: CacheState) -> None:
        log.info(
            "agent.scores.scheduled",
            **self._fields(),
            cache_status=state.status.value,
            reason=state.reason or None,
            start_delay_s=self.settings.start_delay_s,
        )
        await asyncio.sleep(self.settings.start_delay_s)
        state = await self._load()  # the CLI may have scored meanwhile
        if state.valid:
            self._last_mtime = state.mtime_ns
            await self._deliver(state)
            return
        self.runs += 1
        try:
            scores = await score_and_cache(
                self.data_dir,
                facts,
                self._config,
                runner_factory=self._runner_factory,
                backends=self._backends,
                log_fields=self._fields(),
            )
        except ScorerBusyError:
            log.info(
                "agent.scores.skipped",
                **self._fields(),
                reason="another scorer holds scores.lock; its result is picked up "
                "by the cache poller",
            )
            return
        except CacheWriteError as exc:
            log.warning(
                "agent.scores.cache_write_failed", **self._fields(), error=str(exc)
            )
            scores = exc.scores
        except OSError as exc:
            log.warning(
                "agent.scores.failed",
                **self._fields(),
                failure_type="io_error",
                error=exc.strerror or type(exc).__name__,
            )
            return
        self._last_mtime = await asyncio.to_thread(cache_mtime_ns, self.data_dir)
        await self._emit(scores)

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cache_poll_s)
            mtime = await asyncio.to_thread(cache_mtime_ns, self.data_dir)
            if mtime is None or mtime == self._last_mtime:
                continue
            self._last_mtime = mtime
            await self._deliver(await self._load())

    async def _deliver(self, state: CacheState) -> None:
        if state.valid and state.scores is not None:
            await self._emit(state.scores)
        else:
            log.info(
                "agent.scores.cache_ignored",
                **self._fields(),
                cache_status=state.status.value,
                reason=state.reason or None,
            )

    async def _emit(self, scores: ScoreSet) -> None:
        if scores.run_id == self._last_reported:
            return  # the same run seen twice (our own write, then the poller)
        self._last_reported = scores.run_id
        await self._report(scores)

    async def _log_ready(self, scores: ScoreSet) -> None:
        """G7-A's report step: log the run (no numbers); G7-B sends it."""
        log.info(
            "agent.scores.ready",
            **self._fields(),
            run_id=scores.run_id,
            fingerprint=scores.fingerprint[:16],
            statuses=scores.statuses(),
        )
