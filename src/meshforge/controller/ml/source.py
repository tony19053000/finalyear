"""What the planner's ML predictions come from (V0.15 G3, D339, D358) - the
model-set counterpart of the calibration's
:class:`~meshforge.controller.calibration_store.CalibrationSource`.

``[controller.planner] ml``:

* ``off`` - no model; :meth:`ModelSource.refresh` answers None (the planner
  plans exactly as V0.14);
* ``auto`` - the set :meth:`~.registry.ModelRegistry.serving_version` names
  (pinned, else active), re-read by version on every :meth:`refresh` (one
  cheap query) and loaded only when it changed;
* ``sha256:<hex>`` - that set, pinned in configuration. :meth:`check` (at
  startup) refuses a set that does not exist, was ``rejected`` by the gate,
  or cannot be loaded - :class:`~meshforge.common.errors.ConfigError`, never
  a silent fallback (as D306/D301 for the calibration).

A failed read keeps the set it had (logged once per failure streak): a
database hiccup never changes what the planner uses. A set that cannot be
loaded (artefact gone, its library not installed) is remembered, logged
once, and the source answers None for it (the planner falls back to the
calibration) - it is not re-read every tick.

Loading runs in a worker thread (:meth:`~.registry.ModelRegistry.load`);
G4/G5 call :meth:`refresh` from the dispatcher and turn :attr:`current`
into the planner's ``TermPredictor``.
"""

from __future__ import annotations

from typing import Final, Literal

from meshforge.common.errors import ConfigError
from meshforge.common.logging import get_logger
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.registry import MlRegistryError, ModelRegistry
from meshforge.controller.ml.serve import LoadedSet

__all__ = ["ML_OFF", "MlMode", "ModelSource", "parse_ml_mode"]

log = get_logger(__name__)

ML_OFF: Final = "off"
type MlMode = Literal["auto", "off", "pinned"]


def parse_ml_mode(value: str) -> tuple[MlMode, str | None]:
    """``[controller.planner] ml`` -> (mode, pinned set version)."""
    if value in ("auto", "off"):
        return value, None  # type: ignore[return-value]
    return "pinned", value


class ModelSource:
    """See module docstring."""

    def __init__(self, registry: ModelRegistry, setting: str) -> None:
        self._registry = registry
        self.mode, self.pinned = parse_ml_mode(setting)
        self.current: LoadedSet | None = None
        self.kind: str | None = None
        """``pinned`` | ``active`` | ``config`` (what :attr:`current` is)."""
        self.failures = 0
        self.unavailable: str | None = None
        """Why the serving set is not loaded (``artefact_missing``,
        ``library_missing``, ...); None when loaded or nothing serves."""
        self._failing = False
        self._unreadable: str | None = None

    async def check(self) -> None:
        """Startup (module docstring): a configured pin must exist, not be
        ``rejected`` and load."""
        if self.mode != "pinned" or self.pinned is None:
            return
        row = await self._registry.get(self.pinned)
        where = f"[controller.planner] ml = {self.pinned}"
        if row is None:
            raise ConfigError(
                f"{where}: no such model set (see compute planner ml models)"
            )
        if row.status == "rejected":
            raise ConfigError(
                f"{where}: that set was rejected by the activation gate "
                f"({row.reason}) and is never used (pin an active or superseded set)"
            )
        try:
            self.current = await self._registry.load(row)
        except ArtefactError as exc:
            raise ConfigError(
                f"{where}: that set cannot be loaded ({exc.details.get('reason')})"
            ) from None
        self.kind = "config"

    async def refresh(self) -> LoadedSet | None:
        """The set to plan with now (module docstring)."""
        if self.mode == "off":
            return None
        if self.mode == "pinned":
            return self.current
        try:
            kind, version = await self._registry.serving_version()
            if version is None:
                self.current, self.kind, self.unavailable = None, None, None
            elif version == self._unreadable:
                self.current = None  # known bad: logged once, not re-read
            elif self.current is None or self.current.set_version != version:
                await self._load(kind, version)
            else:
                self.kind = kind
        except MlRegistryError as exc:
            self.failures += 1
            if not self._failing:
                log.warning(
                    "controller.planner.ml_read_failed",
                    cause=exc.details.get("cause"),
                    kept_set=None if self.current is None else self.current.set_version,
                )
            self._failing = True
            return self.current
        except Exception as exc:  # a bug here must never stop the dispatch
            self.failures += 1
            if not self._failing:
                log.exception(
                    "controller.planner.ml_read_crashed",
                    error_type=type(exc).__name__,
                )
            self._failing = True
            return self.current
        self._failing = False
        return self.current

    async def _load(self, kind: str | None, version: str) -> None:
        row = await self._registry.get(version)
        if row is None:  # changed between the two reads; next refresh
            return
        try:
            loaded = await self._registry.load(row)
        except ArtefactError as exc:
            reason = str(exc.details.get("reason", "unreadable"))
            log.error(  # noqa: TRY400 - a typed reason, no traceback needed
                "controller.planner.ml_set_unloadable",
                ml_set_version=version,
                reason=reason,
            )
            self._unreadable = version
            self.current, self.kind, self.unavailable = None, kind, reason
            return
        log.info(
            "controller.planner.ml_set_loaded",
            ml_set_version=version,
            kind=kind,
            terms=list(loaded.terms),
        )
        self.current, self.kind, self.unavailable = loaded, kind, None
