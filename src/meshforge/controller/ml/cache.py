"""The dataset route's cache: build once per data change, one build at a time
(V0.15 G2, D355). I/O (through :mod:`.export` / :mod:`.loader`).

G1's ``GET /v1/planner/ml/dataset`` rebuilt the whole dataset per request - an
easy admin-only denial of service and slow. :class:`DatasetCache` keeps the
last :class:`~.export.BuiltDataset` keyed by a cheap
:class:`~.loader.Watermark` (matched-run count and newest ``(ended_at,
decision id)``, every ended attempt's count and newest end - so a lane, a
combine or a failure that ends later also invalidates -, the feature version
and the read cap). Every request takes one :class:`asyncio.Lock` (single
flight): it reads the watermark and, only when it differs from the cached
one, builds. A request that arrives during a build waits for it, then reads
the watermark again and reuses the fresh build.

The watermark is read **before** the build: data that lands during a build
leaves the cache keyed on the older watermark, so the next request rebuilds
(never a stale hit). One build is held in memory (the Parquet bytes, at most
``ml_dataset_max_runs`` runs' rows).

The heavy modules (pyarrow via :mod:`.export`) are imported on first use, so
creating a cache costs nothing on a controller that never serves the route.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.logging import get_logger

if TYPE_CHECKING:
    from meshforge.controller.ml.export import BuiltDataset
    from meshforge.controller.ml.loader import Watermark

__all__ = ["DatasetCache"]

log = get_logger(__name__)


class DatasetCache:
    """See module docstring."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._key: Watermark | None = None
        self._built: BuiltDataset | None = None
        self.builds = 0
        """Builds done (tests and logs)."""

    async def get(self, engine: AsyncEngine, *, max_runs: int) -> BuiltDataset:
        """The dataset of the current data (cached when unchanged);
        :class:`~.loader.MlLoadError` when the database fails (nothing
        cached then)."""
        from meshforge.controller.ml import export, loader

        async with self._lock:
            key = await loader.read_watermark(engine, max_runs=max_runs)
            if self._built is not None and key == self._key:
                log.info(
                    "controller.ml.dataset_cache_hit",
                    digest=self._built.dataset.digest,
                )
                return self._built
            built = await export.build_dataset(engine, max_runs=max_runs)
            self._key, self._built = key, built
            self.builds += 1
            return built
