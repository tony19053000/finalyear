"""G7-B (0.3.4, D37) item 2: how a score run reaches the wire on the agent side.

* the first ``Hello`` carries the cache only when it is valid for this machine;
* a finished run replaces what later Hellos carry and is sent as a report;
* ``hardware_scores`` is a report (buffered, replayed) and shares the report
  sequence with ``resource_update`` without ever writing into a ResourceUpdate.

No controller, no child process; the cross-process proofs are in
tests/component/controller/test_hardware_scores.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_facts, make_gpu, make_scoreset
from structlog.testing import capture_logs

from meshforge.agent import runtime
from meshforge.agent.benchmarks.cache import fingerprint, fingerprint_inputs, save_cache
from meshforge.agent.benchmarks.service import session_reporter, valid_cached_scores
from meshforge.agent.benchmarks.wire import from_proto, to_proto
from meshforge.agent.session.hello import build_hello
from meshforge.agent.session.replay import REPORT_PAYLOADS, ReplayRing
from meshforge.agent.settings import AgentSection, AgentSettings, ScoreSettings
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

FACTS = make_facts(gpus=(make_gpu(0),))
BACKENDS: dict[str, str | None] = {"numpy": "2.5.3", "torch": None}
INPUTS = fingerprint_inputs(FACTS, BACKENDS)
FP = fingerprint(INPUTS)
NODE = "node_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"


def _backends() -> dict[str, str | None]:
    return dict(BACKENDS)


# --------------------------------------------------------------- the cache


def test_a_valid_cache_is_returned_for_the_hello(tmp_path: Path) -> None:
    scores = make_scoreset(fingerprint=FP)
    save_cache(tmp_path, scores, INPUTS)
    assert valid_cached_scores(tmp_path, FACTS, _backends) == scores


def test_no_facts_missing_stale_or_corrupt_cache_means_no_scores(
    tmp_path: Path,
) -> None:
    assert valid_cached_scores(tmp_path, None, _backends) is None  # no discovery
    assert valid_cached_scores(tmp_path, FACTS, _backends) is None  # missing
    save_cache(tmp_path, make_scoreset(fingerprint="b" * 64), INPUTS)
    assert valid_cached_scores(tmp_path, FACTS, _backends) is None  # stale
    other = make_facts(ram_bytes=64 * 2**30, gpus=(make_gpu(0),))
    save_cache(tmp_path, make_scoreset(fingerprint=FP), INPUTS)
    assert valid_cached_scores(tmp_path, other, _backends) is None  # RAM upgrade
    (tmp_path / "scores.json").write_text("{not json", encoding="utf-8")
    assert valid_cached_scores(tmp_path, FACTS, _backends) is None  # corrupt


def _settings(tmp_path: Path, **scores: Any) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path,
        agent=AgentSection(
            controller_address="127.0.0.1:1", scores=ScoreSettings(**scores)
        ),
    )


@pytest.mark.asyncio
async def test_hello_scores_is_the_wire_form_or_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert await runtime.hello_scores(settings, NODE, FACTS, backends=_backends) is None
    scores = make_scoreset(fingerprint=FP)
    save_cache(tmp_path, scores, INPUTS)
    wire = await runtime.hello_scores(settings, NODE, FACTS, backends=_backends)
    assert wire is not None and from_proto(wire) == scores
    # Scoring disabled does not hide a valid cache (the CLI's run is still true).
    disabled = _settings(tmp_path, enabled=False)
    assert await runtime.hello_scores(disabled, NODE, FACTS, backends=_backends) == wire


@pytest.mark.asyncio
async def test_a_broken_cache_reader_never_stops_enrolment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(runtime, "valid_cached_scores", boom)
    with capture_logs() as logs:
        assert await runtime.hello_scores(_settings(tmp_path), NODE, FACTS) is None
    assert [e["event"] for e in logs] == ["agent.scores.hello_cache_failed"]


# --------------------------------------------------------------- the Hello


def test_build_hello_carries_scores_only_when_given() -> None:
    wire = to_proto(make_scoreset())
    common: dict[str, Any] = {
        "node_id": NODE,
        "session_epoch": 1,
        "agent_version": "0",
        "facts": ("h", "os", "x86_64"),
        "hardware": agent_pb2.HardwareSummary(cpu_model="c"),
    }
    assert not build_hello(**common).HasField("hardware_scores")
    hello = build_hello(**common, hardware_scores=wire)
    assert hello.HasField("hardware_scores")
    assert hello.hardware_scores == wire


# ---------------------------------------------------------------- the report


class FakeChannel:
    def __init__(self) -> None:
        self.node_id = NODE
        self.session_id = ""
        self.hardware_scores: agent_pb2.HardwareScores | None = None
        self.sent: list[tuple[str, Any, agent_pb2.HardwareScores | None]] = []

    async def send_report(self, payload_name: str, payload: Any) -> str:
        # Record what a Hello built *now* would carry, next to the report.
        self.sent.append((payload_name, payload, self.hardware_scores))
        return "cmd_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"


@pytest.mark.asyncio
async def test_the_reporter_updates_the_hello_first_then_sends() -> None:
    channel = FakeChannel()
    scores = make_scoreset()
    with capture_logs() as logs:
        await session_reporter(channel)(scores)
    assert len(channel.sent) == 1
    name, payload, hello_view = channel.sent[0]
    assert name == "hardware_scores"
    assert from_proto(payload) == scores
    assert hello_view == payload  # a reconnect meanwhile already announces it
    assert channel.hardware_scores == payload
    reported = [e for e in logs if e["event"] == "agent.scores.reported"]
    assert reported and reported[0]["run_id"] == scores.run_id
    assert reported[0]["node_id"] == NODE
    assert "mean" not in str(reported[0])  # statuses, never numbers


def test_hardware_scores_is_a_buffered_report() -> None:
    assert "hardware_scores" in REPORT_PAYLOADS
    ring = ReplayRing(4)
    msg = agent_pb2.AgentMessage(
        message_id="cmd_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
        hardware_scores=to_proto(make_scoreset()),
    )
    assert ring.append(5, msg) is False
    assert ring.first_sequence() == 5
