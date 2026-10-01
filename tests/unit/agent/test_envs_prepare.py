"""V0.5 G2 (D87): ``meshforge-agent prepare`` - pre-pull the pinned bases, report
sizes and timings; a fake Docker client stands in for the daemon (the real pull is
exercised by hand, see the handoff)."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

docker = pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from meshforge.agent.envs import prepare as prep  # noqa: E402
from meshforge.agent.main import app  # noqa: E402
from meshforge.agent.settings import (  # noqa: E402
    CUDA_PYTORCH_IMAGE,
    PYTHON_CPU_IMAGE,
    AgentSettings,
)
from meshforge.common.exit_codes import ExitCode  # noqa: E402

pytestmark = pytest.mark.unit

UV = ("uv-test",)


class FakeImages:
    def __init__(self, local: set[str], sizes: dict[str, int]) -> None:
        self.local = local
        self.sizes = sizes

    def get(self, ref: str) -> Any:
        if ref not in self.local:
            raise docker.errors.ImageNotFound(f"no such image: {ref}")
        return SimpleNamespace(attrs={"Size": self.sizes.get(ref, 1024**2)})


class FakeApi:
    def __init__(self, client: FakeClient, messages: list[dict[str, Any]]) -> None:
        self.client = client
        self.messages = messages
        self.pulls: list[tuple[str, str]] = []
        self.block = threading.Event()
        self.blocking = False

    def pull(self, repo: str, *, tag: str, stream: bool, decode: bool) -> Iterator[Any]:
        self.pulls.append((repo, tag))

        def gen() -> Iterator[Any]:
            if self.blocking:
                self.block.wait(5)
            yield from self.messages
            if not any("error" in m for m in self.messages):
                self.client.images.local.add(f"{repo}@{tag}")
                for ref in (PYTHON_CPU_IMAGE, CUDA_PYTORCH_IMAGE):
                    if ref.endswith(tag):
                        self.client.images.local.add(ref)

        return gen()


class FakeClient:
    def __init__(
        self,
        local: set[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.images = FakeImages(set(local or ()), {PYTHON_CPU_IMAGE: 41 * 1024**2})
        self.api = FakeApi(self, messages or [{"status": "Downloaded"}])
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _factory(client: FakeClient) -> Any:
    def make(_timeout: float) -> Any:
        return client

    return make


@pytest.fixture
def settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings.model_validate(
        {"data_dir": tmp_path / "data", "agent": {"controller_address": "127.0.0.1:1"}}
    )


@pytest.mark.asyncio
async def test_present_image_is_not_pulled(settings: AgentSettings) -> None:
    client = FakeClient(local={PYTHON_CPU_IMAGE})
    report = await prep.prepare(
        settings, bases=("python-cpu",), client_factory=_factory(client), uv_argv=()
    )
    (image,) = report.images
    assert (image.status, image.size_bytes) == ("present", 41 * 1024**2)
    assert client.api.pulls == []
    assert report.ok and client.closed
    assert report.uv.startswith("missing")


@pytest.mark.asyncio
async def test_missing_image_is_pulled_by_digest_and_timed(
    settings: AgentSettings,
) -> None:
    client = FakeClient()
    ticks = iter([10.0, 12.5])
    report = await prep.prepare(
        settings,
        bases=("python-cpu",),
        client_factory=_factory(client),
        uv_argv=(),
        clock=lambda: next(ticks),
    )
    (image,) = report.images
    assert image.status == "pulled" and image.seconds == 2.5
    repo, tag = client.api.pulls[0]
    assert repo == "python" and tag == PYTHON_CPU_IMAGE.split("@")[1]  # by digest
    assert "pulled in 2.5 s" in "\n".join(report.lines())


@pytest.mark.asyncio
async def test_default_is_both_bases_once_each(settings: AgentSettings) -> None:
    client = FakeClient(local={PYTHON_CPU_IMAGE, CUDA_PYTORCH_IMAGE})
    report = await prep.prepare(
        settings,
        bases=("python-cpu", "cuda-pytorch", "python-cpu"),
        client_factory=_factory(client),
        uv_argv=(),
    )
    assert [i.base for i in report.images] == ["python-cpu", "cuda-pytorch"]


@pytest.mark.asyncio
async def test_failed_pull_is_reported_and_not_ok(settings: AgentSettings) -> None:
    client = FakeClient(messages=[{"error": "manifest unknown"}])
    report = await prep.prepare(
        settings, bases=("python-cpu",), client_factory=_factory(client), uv_argv=()
    )
    (image,) = report.images
    assert image.status == "failed" and "manifest unknown" in image.reason
    assert not report.ok


@pytest.mark.asyncio
async def test_pull_past_the_timeout_is_stopped_and_failed(
    settings: AgentSettings,
) -> None:
    client = FakeClient()
    client.api.blocking = True
    report = await prep.prepare(
        settings,
        bases=("python-cpu",),
        client_factory=_factory(client),
        uv_argv=(),
        timeout_s=0.05,
    )
    client.api.block.set()
    (image,) = report.images
    assert image.status == "failed" and "timed out" in image.reason


@pytest.mark.asyncio
async def test_no_docker_is_reported_not_failed(settings: AgentSettings) -> None:
    def unreachable(_t: float) -> Any:
        raise docker.errors.DockerException("daemon not running")

    report = await prep.prepare(settings, client_factory=unreachable, uv_argv=())
    assert report.docker.startswith("unavailable")
    assert report.images == [] and report.ok


@pytest.mark.asyncio
async def test_unknown_base_is_refused(settings: AgentSettings) -> None:
    with pytest.raises(ValueError, match="unknown base"):
        await prep.prepare(settings, bases=("alpine",), uv_argv=())


# ------------------------------------------------------------------ CLI
runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MESHFORGE_AGENT__CONTROLLER_ADDRESS", "127.0.0.1:1")


def _fake_prepare(status: str) -> Any:
    async def fake(settings: AgentSettings, **kw: Any) -> prep.PrepareReport:
        report = prep.PrepareReport(docker="available", uv="uv 0.0")
        for base in kw["bases"]:
            report.images.append(prep.ImageReport(base, "img@sha256:x", status, 5))
        return report

    return fake


@pytest.mark.usefixtures("cli_env")
def test_cli_json_and_exit_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prep, "prepare", _fake_prepare("present"))
    result = runner.invoke(app, ["prepare", "--base", "python-cpu", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    data = json.loads(result.stdout)
    assert data["ok"] and [i["base"] for i in data["images"]] == ["python-cpu"]


@pytest.mark.usefixtures("cli_env")
def test_cli_failed_pull_exits_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prep, "prepare", _fake_prepare("failed"))
    result = runner.invoke(app, ["prepare"])
    assert result.exit_code == ExitCode.FAILURE
    assert "python-cpu: failed" in result.stdout
    assert "cuda-pytorch: failed" in result.stdout


@pytest.mark.usefixtures("cli_env")
def test_cli_unknown_base_is_usage_error() -> None:
    result = runner.invoke(app, ["prepare", "--base", "alpine"])
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert "unknown base" in result.output


@pytest.mark.usefixtures("cli_env")
def test_cli_refuses_an_unpinned_base_in_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MESHFORGE_AGENT__EXECUTION__IMAGES__PYTHON_CPU", "python:3.12-slim"
    )
    result = runner.invoke(app, ["prepare"])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "pinned by digest" in result.output
