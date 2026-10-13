# ruff: noqa: F811
"""V0.7 G6 fix round 3, T-18 (D164), with REAL uv: the ``--hash`` on a wheel
URL is what closes "https -> redirect -> plain http" and anyone on the path.

The tester's servers: an https server (a one-day test CA) whose ``/redirect/``
path answers 302 to a plain-http server that serves the wheel file as it is on
disk at that moment.

* without ``--hash`` the line is refused before uv starts (nothing is fetched);
* with the right hash the redirect is followed and the pinned bytes installed;
* with a wrong hash it fails;
* with the right hash but OTHER bytes on the plain-http leg (the wheel is
  replaced "on the way") it fails, and nothing of the swapped wheel is
  installed.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from test_v07_g6_tester_r3_https_uv import (  # noqa: F401
    WHEEL_NAME,
    Hits,
    _install,
    servers,
)

from meshforge.agent.envs.builder import default_uv_argv

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

if not default_uv_argv():
    pytest.skip("the uv wheel is not installed (agent extra)", allow_module_level=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_an_unhashed_wheel_url_is_refused_before_anything_is_fetched(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    secure, _plain, _wheel, hits = servers
    for tag, path in (("direct", WHEEL_NAME), ("redirect", f"redirect/{WHEEL_NAME}")):
        got = await _install(tmp_path, f"mfdemo @ {secure}/{path}\n", tag)
        assert got.startswith("requirements_rejected:"), got
        assert "requirements line 1" in got and "--hash=sha256:" in got
    assert hits.https == [] and hits.http == []


async def test_a_redirect_to_http_installs_only_the_pinned_bytes(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    secure, _plain, wheel, hits = servers
    line = f"mfdemo @ {secure}/redirect/{WHEEL_NAME}"
    right = _digest(wheel)

    good = await _install(tmp_path, f"{line} --hash=sha256:{right}\n", "good")
    assert good.startswith("installed:") and "mfdemo" in good, good
    assert hits.http, "the redirect to plain http was not followed (test premise)"

    bad = await _install(tmp_path, f"{line} --hash=sha256:{'0' * 64}\n", "bad")
    assert bad.startswith("env_build_failed:") or "hash" in bad.lower(), bad
    assert not bad.startswith("installed:"), bad


async def test_bytes_swapped_on_the_http_leg_are_not_installed(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    secure, _plain, wheel, hits = servers
    pinned = _digest(wheel)
    # "on the way": the plain-http server now serves another wheel
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("mfdemo/swapped_in_transit.py", "EVIL = True\n")
    assert _digest(wheel) != pinned
    got = await _install(
        tmp_path,
        f"mfdemo @ {secure}/redirect/{WHEEL_NAME} --hash=sha256:{pinned}\n",
        "swap",
    )
    assert hits.http, "the swapped bytes were never fetched (test premise)"
    assert not got.startswith("installed:"), got
    assert "hash" in got.lower(), got
    for path in (tmp_path / "envs-swap").rglob("swapped_in_transit.py"):
        raise AssertionError(f"the swapped wheel was installed: {path}")
