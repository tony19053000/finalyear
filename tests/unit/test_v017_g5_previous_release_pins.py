"""V0.17 G5 (Q36): the previous-release pins the upgrade tests start from.

``packaging/previous-release.toml`` is empty until v0.17.0 is published: the
tests must then SKIP with that reason (never pass silently). A pinned asset is
downloaded once and refused when its sha256 differs; a local file named by the
environment wins.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import _pytest.outcomes
import pytest
from mf_previous_release import (
    LOCAL_ENV,
    NOT_PUBLISHED,
    PINS,
    previous_asset,
    require_enabled,
)

pytestmark = pytest.mark.unit


def _pins(
    tmp_path: Path, url: str = "", sha: str = "", version: str = "0.17.0"
) -> Path:
    path = tmp_path / "previous-release.toml"
    path.write_text(
        f'version = "{version}"\ntag = "v{version}"\n'
        f'[agent_deb]\nurl = "{url}"\nsha256 = "{sha}"\n'
        '[controller_deb]\nurl = ""\nsha256 = ""\n[windows_exe]\nurl = ""\nsha256 = ""\n',
        encoding="utf-8",
    )
    return path


def test_the_committed_pins_parse_and_name_all_three_assets() -> None:
    data = tomllib.loads(PINS.read_text(encoding="utf-8"))
    assert {"version", "tag", "agent_deb", "controller_deb", "windows_exe"} <= set(data)
    for kind in ("agent_deb", "controller_deb", "windows_exe"):
        assert set(data[kind]) == {"url", "sha256"}
        if data[kind]["url"]:
            assert len(data[kind]["sha256"]) == 64, kind
            assert data[kind]["url"].startswith("https://"), kind


def test_nothing_pinned_skips_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LOCAL_ENV["agent_deb"], raising=False)
    with pytest.raises(_pytest.outcomes.Skipped, match="no previous release is pinned"):
        previous_asset("agent_deb", pins=_pins(tmp_path, version=""))
    assert "MESHFORGE_TEST_PREVIOUS_" in NOT_PUBLISHED.format(
        env="MESHFORGE_TEST_PREVIOUS_X"
    )


def test_a_url_without_a_sha256_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LOCAL_ENV["agent_deb"], raising=False)
    with pytest.raises(_pytest.outcomes.Failed, match="no sha256"):
        previous_asset("agent_deb", pins=_pins(tmp_path, url="https://x/y.deb"))


def test_a_pinned_download_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LOCAL_ENV["agent_deb"], raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    asset = tmp_path / "meshforge-agent_0.17.0-1_amd64.deb"
    asset.write_bytes(b"pretend package")
    good = hashlib.sha256(b"pretend package").hexdigest()
    got = previous_asset("agent_deb", pins=_pins(tmp_path, asset.as_uri(), good))
    assert got.read_bytes() == b"pretend package"
    assert got.parent.name == good  # cached under its digest
    with pytest.raises(_pytest.outcomes.Failed, match="is not the pinned"):
        previous_asset("agent_deb", pins=_pins(tmp_path, asset.as_uri(), "0" * 64))


def test_a_local_file_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = tmp_path / "old.deb"
    local.write_bytes(b"x")
    monkeypatch.setenv(LOCAL_ENV["agent_deb"], str(local))
    assert previous_asset("agent_deb", pins=_pins(tmp_path, version="")) == local
    monkeypatch.setenv(LOCAL_ENV["agent_deb"], str(tmp_path / "missing.deb"))
    with pytest.raises(_pytest.outcomes.Failed, match="is not a file"):
        previous_asset("agent_deb")


def test_the_scenario_runs_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESHFORGE_TEST_UPGRADE_FROM_RELEASE", raising=False)
    with pytest.raises(
        _pytest.outcomes.Skipped, match="MESHFORGE_TEST_UPGRADE_FROM_RELEASE=1"
    ):
        require_enabled()
    monkeypatch.setenv("MESHFORGE_TEST_UPGRADE_FROM_RELEASE", "1")
    require_enabled()
