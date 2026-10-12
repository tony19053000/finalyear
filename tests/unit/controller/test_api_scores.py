"""G7-B (0.3.4, D38) item 4: ``scores`` on ``/v1/nodes`` and ``/v1/nodes/{id}``.

Status is measured / pending / none and is never inferred from a number; a
not-measured metric serves ``mean: null``. Pure shaping plus the OpenAPI schema;
the end-to-end REST path is in tests/component/agent/test_hardware_scores.py.
"""

from __future__ import annotations

import datetime as dt

import pytest
from mf_api_records import node_record
from mf_bench_fakes import make_scoreset
from test_api_routes import FakeRegistry, build_app, client_for

from meshforge.agent.benchmarks.wire import to_proto
from meshforge.common.capabilities import HARDWARE_SCORES_CAPABILITY
from meshforge.common.ids import new_node_id
from meshforge.controller.api.shape import node_detail, node_scores, node_summary
from meshforge.controller.http import OPENAPI_URL
from meshforge.controller.scores import scores_document

pytestmark = pytest.mark.unit


NODE = str(new_node_id())


def _document() -> dict[str, object]:
    document, _ = scores_document(to_proto(make_scoreset()), node_id=NODE)
    return document


def test_measured_scores_are_served_with_units_and_nulls() -> None:
    scores = node_scores(node_record(node_id=NODE, hardware_scores=_document()))
    assert scores.status == "measured"
    assert scores.measured_at == dt.datetime(
        2026, 9, 24, 12, 0, 0, 123456, tzinfo=dt.UTC
    )
    as_json = scores.model_dump(mode="json")
    assert as_json["measured_at"] == "2026-09-24T12:00:00.123456+00:00"
    names = [m["name"] for m in as_json["metrics"]]
    assert names == ["cpu_fp32_gflops_1t", "mem_triad_gb_s", "gpu_fp32_tflops"]
    cpu, mem, gpu = as_json["metrics"]
    assert cpu["unit"] == "GFLOP/s" and cpu["mean"] == 97.31354540848592
    assert mem["unit"] == "GB/s"
    assert gpu["status"] == "unavailable" and gpu["mean"] is None
    assert gpu["reason"] == "torch_not_installed" and gpu["gpu_index"] == 0
    assert as_json["conditions"]["priority_lowered"] is True


def test_pending_needs_the_capability_otherwise_none() -> None:
    capable = node_record(
        hardware={"capabilities": ["v0.3", HARDWARE_SCORES_CAPABILITY]}
    )
    assert node_scores(capable).status == "pending"
    assert (
        node_scores(node_record(hardware={"capabilities": ["v0.3"]})).status == "none"
    )
    assert node_scores(node_record(hardware={})).status == "none"
    assert node_scores(node_record(hardware={"capabilities": "x"})).status == "none"
    empty = node_scores(capable).model_dump(mode="json")
    assert empty == {
        "status": "pending",
        "run_id": None,
        "measured_at": None,
        "score_version": None,
        "conditions": None,
        "metrics": [],
    }


def test_summary_and_detail_both_carry_scores() -> None:
    node = node_record(node_id=NODE, hardware_scores=_document())
    summary = node_summary(node, live=None, sample=None, now=1.0)
    detail = node_detail(node, live=None, sample=None, session=None, now=1.0)
    assert summary.scores == detail.scores
    assert summary.scores.status == "measured"


@pytest.mark.asyncio
async def test_the_openapi_schema_documents_scores() -> None:
    async with client_for(build_app(FakeRegistry())) as client:
        response = await client.get(OPENAPI_URL)
    assert response.status_code == 200
    schema = response.json()
    components = schema["components"]["schemas"]
    assert {"NodeScores", "ScoreMetric", "ScoreConditions"} <= set(components)
    status = components["NodeScores"]["properties"]["status"]
    assert set(status["enum"]) == {"measured", "pending", "none"}
    assert "scores" in components["NodeSummary"]["properties"]


def test_a_document_with_unknown_keys_still_serves() -> None:
    """A newer controller may have stored more fields (then been downgraded)."""
    document = _document()
    document["invented_later"] = 1
    document["conditions"]["invented_later"] = 2  # type: ignore[index]
    document["metrics"][0]["invented_later"] = 3  # type: ignore[index]
    document["metrics"][0]["unit"] = "stored unit is ignored"  # type: ignore[index]
    scores = node_scores(node_record(node_id=NODE, hardware_scores=document))
    assert scores.status == "measured"
    assert scores.metrics[0].unit == "GFLOP/s"
