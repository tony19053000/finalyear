"""V0.7 G6 deliverables 3-4: the dispatch step that resolves a job's secrets.

* mTLS session: the owner's values come back (they go into ``StartAttempt``);
* insecure-dev session (no client certificate): refused - the attempt ends
  ``config_error`` ``secrets_need_mtls`` and nothing is sent - unless
  ``[security] allow_insecure_secrets``;
* missing / unreadable / key problems end the attempt typed, never retried;
* a job without secrets never touches the store.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.errors import FailureType
from meshforge.common.secret_redaction import SecretMatcher, redact_bytes
from meshforge.controller.jobs import dispatch
from meshforge.controller.jobs.dispatch import DispatchSteps
from meshforge.controller.secret_box import SecretIntegrityError, SecretKeyError
from meshforge.controller.secret_store import SecretsMissingError, SecretsTooLargeError

pytestmark = pytest.mark.unit

JOB: Any = SimpleNamespace(
    id="job_x",
    owner_user_id="usr_alice",
    manifest={"secrets": [{"name": "API_KEY", "as_env": False}]},
)
ATTEMPT: Any = SimpleNamespace(id="att_x", fencing_token=1, node_id="node_x")
CAP = 4096


class Store:
    def __init__(self) -> None:
        self.ended: list[dict[str, Any]] = []

    async def end_attempt(self, attempt_id: str, token: int, **kw: Any) -> Any:
        self.ended.append(kw)
        return SimpleNamespace(attempt=None)


class Secrets:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[Any, Any]] = []
        self.caps: list[int | None] = []

    async def resolve(
        self, owner: Any, names: Any, *, max_total_bytes: int | None = None
    ) -> dict[str, bytes]:
        self.calls.append((owner, list(names)))
        self.caps.append(max_total_bytes)
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)


def _steps(secrets: Any, *, cert: str | None, insecure_ok: bool = False) -> Any:
    steps: Any = DispatchSteps.__new__(DispatchSteps)
    steps._secrets = secrets
    steps._insecure_secrets = insecure_ok
    steps._store = Store()
    steps._settings = SimpleNamespace(max_job_secret_bytes=CAP)
    steps._manager = SimpleNamespace(get=lambda _n: SimpleNamespace(cert_serial=cert))
    steps._after = lambda _outcome, _report=None: None
    return steps


def _run(steps: Any, job: Any = JOB) -> Any:
    return asyncio.run(DispatchSteps._job_secrets(steps, job, ATTEMPT))


def test_mtls_gets_the_owners_values() -> None:
    secrets = Secrets({"API_KEY": b"value-1"})
    steps = _steps(secrets, cert="ab12")
    assert _run(steps) == {"API_KEY": b"value-1"}
    assert secrets.calls == [("usr_alice", ["API_KEY"])]
    assert secrets.caps == [CAP]  # D162: the per-job cap applies at dispatch
    assert steps._store.ended == []


@pytest.mark.security
def test_insecure_dev_is_refused_without_the_flag() -> None:
    secrets = Secrets({"API_KEY": b"value-1"})
    steps = _steps(secrets, cert=None)
    assert _run(steps) is None
    (ended,) = steps._store.ended
    assert ended["failure_type"] is FailureType.CONFIG_ERROR
    assert ended["detail"].startswith("secrets_need_mtls")
    assert ended["retryable"] is False
    assert secrets.calls == []  # not even read
    allowed = _steps(Secrets({"API_KEY": b"value-1"}), cert=None, insecure_ok=True)
    assert _run(allowed) == {"API_KEY": b"value-1"}


@pytest.mark.parametrize(
    ("error", "failure", "prefix"),
    [
        (
            SecretsMissingError(["API_KEY"]),
            FailureType.INVALID_INPUT,
            "secret_missing:API_KEY",
        ),
        (
            SecretsTooLargeError(CAP + 1, CAP),
            FailureType.INVALID_INPUT,
            "secret_bytes_over_limit:",
        ),
        (SecretIntegrityError("x"), FailureType.INTEGRITY_ERROR, "secret_unreadable"),
        (SecretKeyError("/path/key"), FailureType.CONFIG_ERROR, "secret_key_unusable"),
    ],
)
def test_failures_end_the_attempt_typed(
    error: Exception, failure: FailureType, prefix: str
) -> None:
    steps = _steps(Secrets(error), cert="ab12")
    assert _run(steps) is None
    (ended,) = steps._store.ended
    assert ended["failure_type"] is failure
    assert ended["detail"].startswith(prefix)
    assert "/path/key" not in ended["detail"]
    assert ended["retryable"] is False


def test_no_secrets_no_store() -> None:
    job: Any = SimpleNamespace(id="j", owner_user_id=None, manifest={})
    steps = _steps(None, cert=None)
    assert _run(steps, job) == {}


def test_the_log_matcher_is_compiled_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D162 (T-8): compiling on the loop froze every session for seconds."""
    built_in: list[threading.Thread] = []

    class Recording(SecretMatcher):
        def __init__(self, secrets: Any) -> None:
            built_in.append(threading.current_thread())
            super().__init__(secrets)

    monkeypatch.setattr(dispatch, "SecretMatcher", Recording)

    async def main() -> tuple[Any, Any, threading.Thread]:
        made = await dispatch._matcher({"API_KEY": b"value-1234"})
        return made, await dispatch._matcher({}), threading.current_thread()

    matcher, none, loop_thread = asyncio.run(main())
    assert none is None
    assert len(built_in) == 1 and built_in[0] is not loop_thread
    assert redact_bytes(b"x value-1234 y", matcher) == b"x [secret:API_KEY] y"
