"""Spike SP2: fetch, verify, extract and trim the pinned PostgreSQL build.

V0.17 G2 moved the implementation to ``scripts/pg_bundle.py`` (pin in
``packaging/pins.toml``, Q19 mirror). This module keeps the spike's API
(``ensure_trimmed(PgBuild)``) for ``lab/spikes/sp2_managed_pg.py`` and the SP2
component tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from meshforge.controller.db_managed.binaries import PgBuild

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
_SCRIPTS = str(REPO_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import pg_bundle  # noqa: E402
from pg_bundle import (  # noqa: E402
    BIN_KEEP,
    BundleError,
    PgPin,
    TrimmedBundle,
    cache_root,
    dir_size,
    sha256_file,
    trim,
)

__all__ = [
    "BIN_KEEP",
    "BundleError",
    "TrimmedBundle",
    "cache_root",
    "dir_size",
    "ensure_trimmed",
    "fetch",
    "sha256_file",
    "trim",
]


def _pin(build: PgBuild) -> PgPin:
    return PgPin(build.version, (build.url,), build.sha256, build.archive_root)


def fetch(build: PgBuild, cache: Path, *, timeout_s: float = 300.0) -> Path:
    return pg_bundle.fetch(_pin(build), cache, timeout_s=timeout_s)


def ensure_trimmed(build: PgBuild, cache: Path | None = None) -> TrimmedBundle:
    """Fetch + extract + trim once (pinned by ``build``); later calls reuse the cache."""
    return pg_bundle.ensure_trimmed(_pin(build), cache)
