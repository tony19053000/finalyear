"""A stored model set by version, as the planner's input (V0.15 G5a; D364,
D368, D371).

:class:`RegistryLoader` answers "the :class:`~meshforge.controller.planner.
predictor.MlInput` of set ``V``" for **any** stored set - active, pinned,
superseded or rejected: a replay must re-plan with the set the decision
used, and the D371 regret check compares a candidate with what serves now,
whatever its status. It loads through the **serving path** - the registry's
:meth:`~.registry.ModelRegistry.load` (artefacts from the blob store, digest
checked, :func:`~.serve.load_model`), :class:`~.source.SetPredictor`,
:func:`~.predictor.ml_input` (warm-up) - **without** the live predict budget
(``budget_ms=None``: offline, time decides nothing; a wall-clock budget would
make a replay or a report depend on the machine's load).

* unknown version, a set of another feature version (D366), an artefact
  that is gone or cannot be decoded, a failed warm-up -> None (the caller's
  ``ml_model_unavailable`` / "judge against calibration"); never the wrong
  set - the answer's version is checked against the asked one;
* a database failure raises :class:`~.registry.MlRegistryError` (retryable;
  a hiccup is not "this set does not exist");
* the last :data:`CACHE_SETS` (4) sets loaded are kept (LRU): an evaluator
  run or a retrain asks for the same one or two sets many times. A miss is
  never cached (a restored blob loads on the next ask).

:meth:`RegistryLoader.blocking` is the synchronous ``load`` callable
:func:`~.replay.replay_decision` takes, for code that replays in a worker
thread: it runs :meth:`RegistryLoader.load` on the controller's event loop
and waits.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import Final

from meshforge.common.logging import get_logger
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.predictor import ml_input
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.source import SetPredictor
from meshforge.controller.planner.predictor import DEFAULT_MAX_INTERVAL_LOG, MlInput

__all__ = ["CACHE_SETS", "RegistryLoader"]

log = get_logger(__name__)

CACHE_SETS: Final = 4
"""Sets kept loaded (LRU)."""


class RegistryLoader:
    """See module docstring."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        max_interval_log: float = DEFAULT_MAX_INTERVAL_LOG,
        size: int = CACHE_SETS,
    ) -> None:
        if size < 1:
            raise ValueError("the loader keeps at least one set")
        self._registry = registry
        self.max_interval_log = max_interval_log
        self._size = size
        self._cache: OrderedDict[str, MlInput] = OrderedDict()
        self.loads = 0
        """Sets actually read from the registry (tests: the LRU works)."""

    @property
    def cached(self) -> tuple[str, ...]:
        """The cached set versions, least recently used first."""
        return tuple(self._cache)

    async def load(self, version: str) -> MlInput | None:
        """Set ``version`` as the planner's input, or None (module
        docstring). :class:`~.registry.MlRegistryError` when the database
        fails."""
        found = self._cache.get(version)
        if found is not None:
            self._cache.move_to_end(version)
            return found
        row = await self._registry.get(version)
        if row is None:
            return None
        if row.feature_version != FEATURE_VERSION:
            log.info(
                "controller.planner.ml_set_stale_features",
                ml_set_version=version,
                set_feature_version=row.feature_version,
                feature_version=FEATURE_VERSION,
            )
            return None
        try:
            loaded = await self._registry.load(row)
        except ArtefactError as exc:
            log.warning(
                "controller.planner.ml_set_unloadable",
                ml_set_version=version,
                reason=str(exc.details.get("reason", "unreadable")),
            )
            return None
        self.loads += 1
        built = await asyncio.to_thread(
            ml_input,
            SetPredictor(loaded),
            max_interval_log=self.max_interval_log,
            budget_ms=None,
        )
        if built is None or built.version != version:
            return None
        self._cache[version] = built
        self._cache.move_to_end(version)
        while len(self._cache) > self._size:
            self._cache.popitem(last=False)
        return built

    def blocking(
        self, loop: asyncio.AbstractEventLoop, *, timeout_s: float | None = None
    ) -> Callable[[str], MlInput | None]:
        """A synchronous ``load`` for a worker thread (module docstring).
        Calling it on ``loop``'s own thread would deadlock: it raises
        ``RuntimeError`` instead. With ``timeout_s`` a load still running
        after that long is cancelled and raises ``TimeoutError`` (the worker
        thread never waits on the loop without a bound; None = no bound)."""

        def load(version: str) -> MlInput | None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                raise RuntimeError(
                    "RegistryLoader.blocking() must be called from a worker thread"
                )
            future = asyncio.run_coroutine_threadsafe(self.load(version), loop)
            try:
                return future.result(timeout_s)
            except TimeoutError:
                future.cancel()
                raise

        return load
