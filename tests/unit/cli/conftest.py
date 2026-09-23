"""Fixtures for the ``compute`` unit tests (V0.3 G5, roadmap 0.3.12).

No network and no terminal: every test invokes the Typer app **in-process** with
``typer.testing.CliRunner`` and injects an ``httpx.MockTransport``, so nothing
here opens a socket - a bare ``CliSettings.load()`` would otherwise dial
``127.0.0.1:47110`` on the developer's machine.

The autouse fixture also clears ``MESHFORGE_*`` and repoints the config/data dirs
into ``tmp_path`` so a developer's own ``config.toml`` can never change a result.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mf_cli_support import Handler, TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli.client import ControllerClient
from meshforge.cli.settings import CliSettings


@pytest.fixture(autouse=True)
def _no_meshforge_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    # Rich would otherwise adapt to the developer's terminal width.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> Iterator[TransportFactory]:
    """``controller(handler)`` makes every command in this test use ``handler``.

    Only the *transport* is faked: the real :class:`ControllerClient`, the real
    settings and the real renderers run.
    """
    installed: list[ControllerClient] = []

    def install(handler: Handler) -> None:
        def factory(settings: CliSettings) -> ControllerClient:
            client = ControllerClient(
                settings.cli.controller_url,
                timeout_s=settings.cli.timeout_s,
                transport=httpx.MockTransport(handler),
            )
            installed.append(client)
            return client

        monkeypatch.setattr(cli_app, "_client", factory)

    yield install
    for client in installed:
        client.close()
