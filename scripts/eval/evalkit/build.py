"""Which build is running (D406: every evidence run records version + SHA).

The installed release bundle's ``BUILDINFO.json`` when there is one (the lab
runs the ``rc`` packages), else the source checkout's ``git rev-parse HEAD``
(a developer run - the analysis reports it as such, never as a release).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

__all__ = ["build_info"]


def _git_sha(repo: Path) -> str | None:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - developer checkout only
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else None


def build_info(repo: Path) -> dict[str, Any]:
    """``{package_version, git_sha, source}`` (``bundle`` or ``checkout``)."""
    from meshforge import __version__
    from meshforge.common.buildinfo import current_build_info

    bundle = current_build_info()
    if bundle is not None:
        return {
            "package_version": bundle.version,
            "git_sha": bundle.git_sha,
            "source": "bundle",
        }
    return {
        "package_version": __version__,
        "git_sha": _git_sha(repo),
        "source": "checkout",
    }
