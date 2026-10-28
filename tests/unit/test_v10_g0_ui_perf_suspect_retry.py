"""V1.0 G0: UI-perf seeding waits out a node that is SUSPECT for a moment.

The 4x100 pinned burst on the FAST-liveness test fabric makes a node SUSPECT for
a few ms (heartbeat a few tens of ms past the 0.25 s edge); a pinned submit that
lands in that window gets ``409 no_feasible_plan``. Only that refusal is retried,
only for a pinned submit, and only until ``SUSPECT_RETRY_S``.
"""

from __future__ import annotations

import ssl
from types import ModuleType
from typing import Any

import httpx
import pytest
from mf_ui_perf import load_ui_perf

pytestmark = pytest.mark.unit

UI = load_ui_perf("ui_perf_v10_g0_suspect")
SCEN: ModuleType = UI.scenarios

SUSPECT = {
    "error": {"code": "no_feasible_plan", "message": "the node is suspect, not online"}
}
OFFLINE = {
    "error": {"code": "no_feasible_plan", "message": "the node is offline, not online"}
}


def _api(answers: list[tuple[int, dict[str, Any]]]) -> tuple[Any, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = answers[min(len(seen), len(answers)) - 1]
        return httpx.Response(status, json=body)

    api = UI.Api("https://h", "tok", ssl.create_default_context())
    api.http.close()
    api.http = httpx.Client(
        base_url="https://h", transport=httpx.MockTransport(handler)
    )
    return api, seen


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SCEN, "SUSPECT_RETRY_PAUSE_S", 0.0)


def test_a_suspect_blip_is_waited_out() -> None:
    api, seen = _api([(409, SUSPECT), (409, SUSPECT), (201, {"job_id": "job_1"})])
    assert api.submit("tree", "n", "node_a") == "job_1"
    assert len(seen) == 3


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, OFFLINE),
        (
            409,
            {
                "error": {
                    "code": "conflict",
                    "message": "the node is suspect, not online",
                }
            },
        ),
        (500, SUSPECT),
        (404, {"error": {"code": "not_found", "message": "no such node"}}),
    ],
)
def test_any_other_refusal_is_raised_at_once(status: int, body: dict[str, Any]) -> None:
    api, seen = _api([(status, body), (201, {"job_id": "job_1"})])
    with pytest.raises(RuntimeError, match=str(status)):
        api.submit("tree", "n", "node_a")
    assert len(seen) == 1


def test_an_unpinned_submit_is_not_retried() -> None:
    api, seen = _api([(409, SUSPECT), (201, {"job_id": "job_1"})])
    with pytest.raises(RuntimeError, match="409"):
        api.submit("tree", "n")
    assert len(seen) == 1


def test_a_node_that_stays_suspect_fails_after_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SCEN, "SUSPECT_RETRY_S", 0.0)
    api, seen = _api([(409, SUSPECT)])
    with pytest.raises(RuntimeError, match="suspect"):
        api.submit("tree", "n", "node_a")
    assert len(seen) == 1
