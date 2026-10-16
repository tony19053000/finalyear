"""V0.8 G5 item 0e (D195): a revoked agent that is still running exits cleanly.

Revoking a node cuts its stream ``PERMISSION_DENIED`` with the controller's
``node_revoked: ...`` detail (V0.7 D138). That is an admin's decision, not a
fault: the agent stops for good with the same exit code as before
(``CONFIG_ERROR``) but logs ``agent.revoked`` - never ``agent.exited_with_error``
with a traceback.
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest
from structlog.testing import capture_logs

from meshforge.agent import runtime
from meshforge.agent.session.errors import AgentContractError, NodeRevokedError
from meshforge.agent.session.policy import (
    NODE_REVOKED_REASON,
    StreamEndAction,
    stream_end_action,
)
from meshforge.common.errors import ExitCode

pytestmark = pytest.mark.unit

DETAIL = (
    "node_revoked: this node was revoked from the fabric; this laptop can come "
    "back only under a new identity: `meshforge-agent identity reset --yes`, then "
    "`meshforge-agent join <code>` with a new code from the fabric admin"
)


@pytest.mark.parametrize(
    ("code", "details", "action"),
    [
        (grpc.StatusCode.PERMISSION_DENIED, DETAIL, StreamEndAction.STOP_REVOKED),
        (
            grpc.StatusCode.PERMISSION_DENIED,
            "  node_revoked",
            StreamEndAction.STOP_REVOKED,
        ),
        # Other refusals stay fatal config errors (logged with their cause).
        (
            grpc.StatusCode.PERMISSION_DENIED,
            "cert_superseded: x",
            StreamEndAction.FATAL_CONFIG,
        ),
        (grpc.StatusCode.PERMISSION_DENIED, "revoked", StreamEndAction.FATAL_CONFIG),
        # V0.8 G7 (D204): the reason word is matched exactly, never as a prefix.
        (
            grpc.StatusCode.PERMISSION_DENIED,
            "node_revoked_pending: x",
            StreamEndAction.FATAL_CONFIG,
        ),
        (
            grpc.StatusCode.PERMISSION_DENIED,
            "node_revokedx",
            StreamEndAction.FATAL_CONFIG,
        ),
        (
            grpc.StatusCode.PERMISSION_DENIED,
            "node_revoked :x",
            StreamEndAction.STOP_REVOKED,
        ),
        (grpc.StatusCode.PERMISSION_DENIED, "", StreamEndAction.FATAL_CONFIG),
        # The word on another status is not the revocation signal.
        (grpc.StatusCode.ABORTED, DETAIL, StreamEndAction.RECONNECT),
        (grpc.StatusCode.UNAVAILABLE, DETAIL, StreamEndAction.RECONNECT),
        (grpc.StatusCode.INVALID_ARGUMENT, DETAIL, StreamEndAction.FATAL_CONFIG),
    ],
)
def test_only_a_permission_denied_node_revoked_end_is_a_revocation(
    code: grpc.StatusCode, details: str, action: StreamEndAction
) -> None:
    assert stream_end_action(code, details) is action


def test_the_controllers_revocation_detail_is_recognised() -> None:
    """The agent cannot import the controller; the test pins both sides."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.session.admission import NODE_REVOKED, REVOKED_DETAIL

    assert NODE_REVOKED == NODE_REVOKED_REASON
    assert (
        stream_end_action(grpc.StatusCode.PERMISSION_DENIED, REVOKED_DETAIL)
        is StreamEndAction.STOP_REVOKED
    )


def test_node_revoked_error_keeps_the_contract_exit_code_and_the_how_to() -> None:
    error = NodeRevokedError("node_x", DETAIL)
    assert isinstance(error, AgentContractError)  # existing handlers still match
    assert error.exit_code is ExitCode.CONFIG_ERROR
    assert "node_revoked" in str(error) and "identity reset" in str(error)
    assert error.details == {"node_id": "node_x", "reason": "node_revoked"}
    assert "node_revoked" in str(NodeRevokedError("node_x", "  "))


def _run_with(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> tuple[int, list[dict[str, Any]]]:
    async def serve(_settings: Any) -> None:
        raise exc

    monkeypatch.setattr(runtime, "serve", serve)
    with capture_logs() as logs:
        code = runtime.run_agent(object())  # type: ignore[arg-type]
    return code, logs


def test_a_revoked_agent_exits_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, logs = _run_with(monkeypatch, NodeRevokedError("node_x", DETAIL))
    assert code == int(ExitCode.CONFIG_ERROR)
    events = [entry["event"] for entry in logs]
    assert events == ["agent.revoked"], logs
    entry = logs[0]
    assert entry["log_level"] == "error"
    assert "exc_info" not in entry and "exception" not in entry, entry
    assert "identity reset" in entry["error"]
    assert entry["node_id"] == "node_x"


def test_other_contract_errors_still_log_with_their_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the revocation is quiet: a real fault keeps its diagnostic."""
    code, logs = _run_with(monkeypatch, AgentContractError("bad hello"))
    assert code == int(ExitCode.CONFIG_ERROR)
    assert [entry["event"] for entry in logs] == ["agent.exited_with_error"]
    assert logs[0].get("exc_info") is True
