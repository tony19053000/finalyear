"""A lane chunk's inputs (V0.10 G4, D260/D264; split out of :mod:`.lane` in fix
round 2): the agent-owned control folder with the items file (JSON array, or
the ``lines`` records), and - ``glob`` - the chunk's own item files fetched
(only those blobs) and placed read-only at their paths in the workdir."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from meshforge.agent.execution.chunk_folder import ChunkFolder
from meshforge.agent.execution.chunk_inputs import (
    split_view_path,
    write_items_json,
    write_lines_chunk,
)
from meshforge.agent.execution.fetch import (
    FetchError,
    StageReport,
    stage_entries,
    tree_sources,
)
from meshforge.agent.execution.lane_base import STAGE_RETRYABLE, LaneHost
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.split_chunks import ChunkSpan
from meshforge.common.tree import Tree

if TYPE_CHECKING:
    from meshforge.agent.execution.executor import _Attempt
    from meshforge.agent.execution.plan import AttemptPlan

__all__ = ["ChunkStagingSteps"]


class ChunkStagingSteps(LaneHost):
    """See module docstring."""

    async def _chunk_inputs(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        folder: ChunkFolder,
        span: ChunkSpan,
        mine: tuple[str | int, ...],
    ) -> list[Path]:
        """The chunk's folder, its items file and (``glob``) its item files."""
        lane = attempt.lane
        workdir = attempt.workdir
        assert lane is not None  # noqa: S101
        assert lane.items is not None  # noqa: S101
        assert workdir is not None  # noqa: S101
        items = lane.items
        await asyncio.to_thread(folder.create)
        if items.over == "lines":
            source = items.source
            assert source is not None  # noqa: S101 - parse_item_list checked
            records = items.records[span.first : span.stop]

            def write_lines() -> int:
                with self.cas.store.open_read(Digest(source[1])) as fh:
                    return write_lines_chunk(
                        folder.items, fh, header=items.header, records=records
                    )

            await asyncio.to_thread(write_lines)
            return []
        await asyncio.to_thread(write_items_json, folder.items, mine)
        if items.over != "glob":
            return []
        return await self._place_items(attempt, plan, [str(i) for i in mine])

    async def _place_items(
        self, attempt: _Attempt, plan: AttemptPlan, wanted: list[str]
    ) -> list[Path]:
        """D264: the chunk's item files the lane did not stage, fetched (only
        those blobs) and placed read-only at their paths."""
        workdir = attempt.workdir
        assert workdir is not None  # noqa: S101
        mounts = [i.mount for i in self._current_spec(attempt).inputs]
        placed: list[Path] = []
        stages: list[StageReport] = list(attempt.stages)
        for index, report in enumerate(stages):
            mount = None if index == 0 else mounts[index - 1]
            rels = [
                rel
                for m, rel in (split_view_path(p, mounts) for p in wanted)
                if m == mount and rel in report.skipped
            ]
            if not rels:
                continue
            dest = workdir if mount is None else workdir.joinpath(*mount.split("/"))
            got = await self._stage_entries_retrying(
                attempt, report.tree, rels, dest, input_index=index - 1
            )
            placed.extend(got)
        return placed

    async def _stage_entries_retrying(
        self,
        attempt: _Attempt,
        tree: Tree,
        rels: list[str],
        dest: Path,
        *,
        input_index: int,
    ) -> list[Path]:
        """:func:`stage_entries` with the staging retry rule (D95): a refused
        token waits for ``RefreshFetchToken``, an unreachable controller is
        retried with backoff, both for at most ``staging_retry_s``."""
        deadline = self._clock() + self._cfg.staging_retry_s
        delay = 0.25
        while True:
            used = attempt.fetch_token
            attempt.token_changed.clear()
            spec = self._current_spec(attempt)
            sources = tree_sources(
                spec,
                self._controller_host(),
                sources=None if input_index < 0 else spec.inputs[input_index].sources,
                scheme=self._scheme,
            )
            try:
                got = await self._step(
                    attempt,
                    stage_entries(
                        self.cas,
                        tree=tree,
                        paths=rels,
                        sources=sources,
                        dest=dest,
                        timeout_s=self._fetch_cfg.request_timeout_s,
                        tuning=self._tuning(),
                        token_for=self._token_lookup(attempt, str(tree.tree_id)),
                        **self._transport(),
                    ),
                )
            except FetchError as exc:
                if exc.failure_type not in STAGE_RETRYABLE or self._clock() >= deadline:
                    if exc.failure_type is FailureType.UNAUTHORIZED:
                        raise FetchError(
                            exc.message, failure_type=FailureType.TRANSFER_ERROR
                        ) from None
                    raise
                if attempt.fetch_token == used:
                    wait = min(delay, max(0.0, deadline - self._clock()))
                    with contextlib.suppress(TimeoutError):
                        await self._step(
                            attempt,
                            asyncio.wait_for(attempt.token_changed.wait(), wait),
                        )
                delay = min(delay * 2, 5.0)
                continue
            return list(got.placed)
