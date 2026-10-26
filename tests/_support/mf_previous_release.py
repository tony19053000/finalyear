"""The previous release's installers for the upgrade-from-release tests
(V0.17 G5, Q36). ``packaging/previous-release.toml`` pins them (URL + sha256);
an environment variable can name a local file instead.

Nothing pinned and no local file -> :func:`previous_asset` **skips** the test
with a reason that says exactly that (never a silent pass). A download whose
sha256 differs from the pin fails the test (never a silent fallback).

The tests run only when ``MESHFORGE_TEST_UPGRADE_FROM_RELEASE=1`` (the CI
``upgrade-from-release`` job, or a developer on purpose): the ordinary package
job would otherwise run the same long scenario a second time.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
import urllib.request
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
PINS: Final = REPO / "packaging" / "previous-release.toml"
ENABLE_ENV: Final = "MESHFORGE_TEST_UPGRADE_FROM_RELEASE"
LOCAL_ENV: Final = {
    "agent_deb": "MESHFORGE_TEST_PREVIOUS_AGENT_DEB",
    "controller_deb": "MESHFORGE_TEST_PREVIOUS_CONTROLLER_DEB",
    "windows_exe": "MESHFORGE_TEST_PREVIOUS_WINDOWS_EXE",
}
NOT_PUBLISHED: Final = (
    "no previous release is pinned in packaging/previous-release.toml (no "
    "GitHub Release published yet, Q36): publish v0.17.0 with "
    ".github/workflows/release.yml and pin its assets, or set {env} to a local "
    "older build"
)


def require_enabled() -> None:
    if os.environ.get(ENABLE_ENV) != "1":
        pytest.skip(
            f"upgrade-from-release scenario: set {ENABLE_ENV}=1 (the CI "
            "upgrade-from-release job does)"
        )


def load_pins(path: Path = PINS) -> dict[str, object]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _cache() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "meshforge-bundle" / "previous-release"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def previous_asset(kind: str, *, pins: Path = PINS) -> Path:
    """The previous release's ``kind`` (``agent_deb`` | ``controller_deb`` |
    ``windows_exe``) as a local file; skips when there is none."""
    env = LOCAL_ENV[kind]
    local = os.environ.get(env)
    if local:
        path = Path(local)
        if not path.is_file():
            pytest.fail(f"{env}={local} is not a file")
        return path
    data = load_pins(pins)
    table = data.get(kind)
    url = str(table.get("url", "")) if isinstance(table, dict) else ""
    sha = str(table.get("sha256", "")) if isinstance(table, dict) else ""
    if not data.get("version") or not url:
        pytest.skip(NOT_PUBLISHED.format(env=env))
    if len(sha) != 64:
        pytest.fail(f"{pins}: [{kind}] has a url but no sha256 - pin both")
    target = _cache() / sha / url.rsplit("/", 1)[-1]
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=300) as resp, tmp.open("wb") as out:  # noqa: S310 - pinned https URL
            while block := resp.read(1 << 20):
                out.write(block)
        tmp.replace(target)
    got = sha256_of(target)
    if got != sha:
        target.unlink(missing_ok=True)
        pytest.fail(f"{url}: sha256 {got} is not the pinned {sha}")
    return target
