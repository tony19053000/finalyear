"""Durable staging progress of one attempt (V1.0 G9-A, D429).

The staging retry window (``[agent.execution] staging_retry_s``) is "how long
staging may keep failing **without making progress**". Progress is counted
here, and only for work a retry does **not** have to repeat:

* a chunk verified against its manifest digest and written to its resumable
  part (the bitmap sidecar keeps it across tries and crashes),
* a blob installed in the cache (a one-chunk blob committed, or a chunked one
  assembled and hashed whole),
* a tree manifest verified and stored,
* a combine part unpacked and sealed.

Bytes received are deliberately **not** progress: a body cut off mid-chunk is
thrown away (nothing unverified is ever kept), so a link that always breaks
before a chunk completes would otherwise retry forever without getting closer.
A whole-bundle fetch (``fetch_bundle``) keeps nothing of a failed try, so it
never counts as progress either.

One object per attempt (``_Attempt.stage_progress``), shared by every staging
try of that attempt (project tree, named inputs, a lane's item files, a
combine's parts); everything runs on the agent's one event loop, so the
counter needs no lock.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StagingProgress"]


@dataclass(slots=True)
class StagingProgress:
    """See module docstring."""

    units: int = 0
    """Chunks verified + blobs installed + manifests stored + parts sealed."""

    def advance(self, n: int = 1) -> None:
        self.units += n
