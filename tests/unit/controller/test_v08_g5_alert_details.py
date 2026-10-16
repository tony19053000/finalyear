"""V0.8 G5 item 0d (D195): alert ``details`` never carry secret-looking keys.

An alert reaches every admin's browser twice: as the audit row's ``details``
(``GET /v1/alerts``) and as the live ``alert`` event's data. Both must hold
ids, states and node names only. Two checks:

1. **Static, every producer:** each ``audit(db, AuditAction.X, ...)`` call in
   the controller whose action is in ``ALERT_KINDS`` passes ``details`` as a
   dict literal, and no key looks like a secret. A new alert kind is covered
   the moment it is added to ``ALERT_KINDS``.
2. **Runtime, the live event:** the dispatcher's WRONG_NODE alert payload
   (the real code path) has no secret-looking key or value.
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import AttemptState
from meshforge.controller.alerts import ALERT_KINDS
from meshforge.controller.jobs.dispatcher import Dispatcher, _Status
from meshforge.controller.jobs.execution_store import (
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.liveness import LeaseTracker, LivenessConfig, NodeState
from meshforge.controller.session.live import LiveSession, SeenIds
from meshforge.controller.session.writer import WriteChain
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.store.models.enrollment import AuditAction

pytestmark = [pytest.mark.unit, pytest.mark.security]

CONTROLLER_SRC: Final = (
    Path(__file__).resolve().parents[3] / "src" / "meshforge" / "controller"
)
SECRET_LOOKING: Final = re.compile(
    r"token|secret|passw|code|key|cookie|csrf|auth|credential|cert|private|"
    r"session_id|signature|hash|salt",
    re.IGNORECASE,
)
"""Key names an alert must never carry (a value under them would be shown to
every admin and kept in the audit table)."""

# Values that look like our own secret shapes (join code mf1-, API token mft_,
# PEM blocks, long base64/hex blobs).
SECRET_VALUE: Final = re.compile(r"mf1-|mft_|-----BEGIN|[A-Za-z0-9+/_-]{40,}")


def test_the_pattern_flags_what_it_must_and_passes_todays_keys() -> None:
    for bad in (
        "token",
        "api_token",
        "password",
        "join_code",
        "ca_key",
        "cookie",
        "mf_csrf",
        "authorization",
        "client_cert",
        "secret_value",
    ):
        assert SECRET_LOOKING.search(bad), bad
    for ok in (
        "id",
        "kind",
        "node_id",
        "attempt_id",
        "job_id",
        "placed_node_id",
        "reported",
    ):
        assert not SECRET_LOOKING.search(ok), ok


def _alert_audit_calls() -> list[tuple[Path, ast.Call, str]]:
    """Every ``audit(db, AuditAction.X, ...)`` call whose X is an alert kind."""
    alert_members = {
        member.name for member in AuditAction if member.value in ALERT_KINDS
    }
    found: list[tuple[Path, ast.Call, str]] = []
    for path in sorted(CONTROLLER_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "audit"
                and len(node.args) >= 2
            ):
                continue
            action = node.args[1]
            if (
                isinstance(action, ast.Attribute)
                and isinstance(action.value, ast.Name)
                and action.value.id == "AuditAction"
                and action.attr in alert_members
            ):
                found.append((path, node, action.attr))
    return found


def test_every_alert_kind_has_an_audit_producer_this_test_can_see() -> None:
    seen = {name for _p, _n, name in _alert_audit_calls()}
    expected = {m.name for m in AuditAction if m.value in ALERT_KINDS}
    assert seen == expected, (
        f"alert kinds without a visible audit(...) producer: {expected - seen}; "
        "call audit(db, AuditAction.X, ..., details={...}) directly"
    )


def test_alert_audit_details_are_literal_with_no_secret_looking_key() -> None:
    calls = _alert_audit_calls()
    assert calls
    for path, call, name in calls:
        where = f"{path.relative_to(CONTROLLER_SRC)}:{call.lineno} ({name})"
        details = next((k.value for k in call.keywords if k.arg == "details"), None)
        if details is None:
            continue  # no details at all is fine
        assert isinstance(details, ast.Dict), (
            f"{where}: alert details must be a dict literal so they can be checked"
        )
        for key in details.keys:
            assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
                f"{where}: alert detail keys must be plain strings (no ** spread)"
            )
            assert not SECRET_LOOKING.search(key.value), (
                f"{where}: alert detail key {key.value!r} looks like a secret; "
                "alerts go to every admin's browser (D195)"
            )


# ---------------------------------------------------------------- live event


class _Manager:
    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(self, *_a: Any, **_kw: Any) -> str:
        return str(new_command_id())

    def ack_message(self, _live: Any, _message_id: str) -> bool:
        return True


class _Store:
    async def apply_status(self, _r: StatusReport, *, node_id: str) -> StatusOutcome:
        return StatusOutcome(
            StatusKind.WRONG_NODE,
            alert={"id": "7", "job_id": "job_x", "placed_node_id": "node_a"},
        )

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return []

    async def get_attempt(self, _attempt_id: str) -> Any:
        return None


def _live() -> LiveSession:
    node_id, session_id = str(new_node_id()), str(new_session_id())
    return LiveSession(
        node_id=node_id,
        session_id=session_id,
        session_epoch=1,
        peer_address="ipv4:127.0.0.1:1",
        tracker=LeaseTracker(LivenessConfig(), lambda: 0.0),
        state=NodeState.ONLINE,
        outbound=asyncio.Queue(),
        seen=SeenIds(4),
        next_ping_at=0.0,
        writer=WriteChain(node_id=node_id, session_id=session_id),
    )


@pytest.mark.asyncio
async def test_the_live_wrong_node_alert_has_no_secret_looking_key_or_value() -> None:
    alerts: list[dict[str, str]] = []
    dispatcher = Dispatcher(
        store=_Store(),  # type: ignore[arg-type]
        manager=_Manager(),  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
        on_alert=alerts.append,
    )
    report = StatusReport(str(new_attempt_id()), 99, AttemptState.FAILED)
    await dispatcher._handle(_Status(_live(), str(new_command_id()), report))  # type: ignore[arg-type]
    assert len(alerts) == 1
    for key, value in alerts[0].items():
        assert not SECRET_LOOKING.search(key), key
        assert not SECRET_VALUE.search(str(value)), (key, value)
    # The fencing token of the refused report is a credential-like value: not sent.
    assert "99" not in alerts[0].values()
