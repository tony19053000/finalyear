"""V0.6 G1 (D116): the controller's known-bad (node, env) memory.

* an attempt that ended ``env_error`` (applied, terminal) records its node and
  the job's env key for ``env_bad_ttl_s``; any other ending records nothing;
* entries expire on the injected monotonic clock; ``ttl 0`` turns it off;
  the map is bounded;
* ``AttemptStatus.env_hash`` is validated before use (64 lowercase hex or empty);
* the new settings' bounds (max accepted, max + 1 refused).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from test_dispatcher_d75 import FakeManager, FakeStore

from meshforge.common.errors import FailureType
from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_job_id,
    new_node_id,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import (
    attempt_state_to_proto,
    failure_type_to_proto,
)
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.dispatcher import Dispatcher, _Status
from meshforge.controller.jobs.env_health import KnownBadEnvs, env_key
from meshforge.controller.jobs.execution_store import (
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.settings import JobApiSettings

pytestmark = pytest.mark.unit

TREE = "sha256:" + "1" * 64
HASH = "ab" * 32


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ the map


def test_an_entry_lives_exactly_ttl_seconds() -> None:
    clock = _Clock()
    bad = KnownBadEnvs(ttl_s=3600, clock=clock)
    key = env_key({"env": {"requirements": "r.txt"}}, tree_id=TREE, bundle_digest=None)
    bad.record("node_a", key, env_hash=HASH, job_id="j", attempt_id="a")
    assert bad.is_bad("node_a", key)
    assert not bad.is_bad("node_b", key)  # per node
    assert not bad.is_bad("node_a", "0" * 64)  # per env
    clock.now += 3599.999
    assert bad.is_bad("node_a", key) and len(bad.for_node("node_a")) == 1
    clock.now += 0.001  # at the TTL: gone
    assert not bad.is_bad("node_a", key)
    assert bad.for_node("node_a") == [] and len(bad) == 0


def test_ttl_zero_remembers_nothing() -> None:
    bad = KnownBadEnvs(ttl_s=0, clock=_Clock())
    assert bad.record("n", "k", env_hash=None, job_id="j", attempt_id="a") is None
    assert not bad.is_bad("n", "k") and len(bad) == 0


def test_the_map_is_bounded_oldest_first() -> None:
    bad = KnownBadEnvs(ttl_s=60, clock=_Clock(), max_entries=2)
    for i in range(3):
        bad.record("n", f"k{i}", env_hash=None, job_id="j", attempt_id="a")
    assert len(bad) == 2 and not bad.is_bad("n", "k0")
    assert bad.is_bad("n", "k1") and bad.is_bad("n", "k2")
    with pytest.raises(ValueError, match="max_entries"):
        KnownBadEnvs(ttl_s=60, clock=_Clock(), max_entries=0)


def test_the_env_key_covers_the_request_and_the_project_only() -> None:
    base = {"name": "x", "entrypoint": ["python", "m.py"]}
    plain = env_key(base, tree_id=TREE, bundle_digest=None)
    assert plain == env_key(
        {**base, "name": "renamed", "env": {"base": "python-cpu"}},
        tree_id=TREE,
        bundle_digest=None,
    )
    assert len(plain) == 64
    others = {
        env_key(
            {**base, "env": {"requirements": "r.txt"}}, tree_id=TREE, bundle_digest=None
        ),
        env_key({**base, "env": {"python": "3.11"}}, tree_id=TREE, bundle_digest=None),
        env_key(
            {**base, "env": {"base": "cuda-pytorch"}}, tree_id=TREE, bundle_digest=None
        ),
        env_key(base, tree_id="sha256:" + "2" * 64, bundle_digest=None),
        env_key(base, tree_id=None, bundle_digest="sha256:" + "3" * 64),
    }
    assert plain not in others and len(others) == 5


# --------------------------------------------------------------- dispatcher


class _Tokens:
    def expire_after(self, *_a: Any) -> None:
        return None


def _dispatcher(bad: KnownBadEnvs | None) -> tuple[Dispatcher, FakeStore]:
    store = FakeStore()
    dispatcher = Dispatcher(
        store=store,  # type: ignore[arg-type]
        manager=FakeManager(),  # type: ignore[arg-type]
        fetch_tokens=_Tokens(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
        known_bad_envs=bad,
    )
    return dispatcher, store


def _ended(
    store: FakeStore, failure: FailureType, *, kind: StatusKind = StatusKind.APPLIED
) -> tuple[_Status, str, str]:
    node, attempt_id = str(new_node_id()), str(new_attempt_id())
    job = SimpleNamespace(
        id=str(new_job_id()),
        manifest={"name": "x", "env": {"requirements": "req.txt"}},
        tree_id=TREE,
        bundle_digest=None,
        cancel_requested_at=None,
    )
    attempt = SimpleNamespace(
        id=attempt_id, node_id=node, state=AttemptState.FAILED, fencing_token=1
    )
    store.outcome = StatusOutcome(kind, attempt, job, ended=True)  # type: ignore[arg-type]
    live = SimpleNamespace(node_id=node, session_id="s", attempt_status_fenced=0)
    report = StatusReport(
        attempt_id=attempt_id,
        fencing_token=1,
        state=AttemptState.FAILED,
        failure_type=failure,
        env_hash=HASH,
    )
    item = _Status(live, str(new_command_id()), report)  # type: ignore[arg-type]
    return item, node, env_key(job.manifest, tree_id=TREE, bundle_digest=None)


@pytest.mark.asyncio
async def test_an_applied_env_error_marks_the_node_and_env_known_bad() -> None:
    bad = KnownBadEnvs(ttl_s=3600, clock=_Clock())
    dispatcher, store = _dispatcher(bad)
    item, node, key = _ended(store, FailureType.ENV_ERROR)
    await dispatcher._handle(item)
    assert bad.is_bad(node, key)
    (entry,) = bad.for_node(node)
    assert entry.env_hash == HASH and entry.attempt_id == item.report.attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [FailureType.USER_ERROR, FailureType.DISK_FULL, FailureType.OOM]
)
async def test_other_failures_record_nothing(failure: FailureType) -> None:
    bad = KnownBadEnvs(ttl_s=3600, clock=_Clock())
    dispatcher, store = _dispatcher(bad)
    item, _node, _key = _ended(store, failure)
    await dispatcher._handle(item)
    assert len(bad) == 0


@pytest.mark.asyncio
async def test_a_duplicate_env_error_report_records_nothing_new() -> None:
    bad = KnownBadEnvs(ttl_s=3600, clock=_Clock())
    dispatcher, store = _dispatcher(bad)
    item, _node, _key = _ended(store, FailureType.ENV_ERROR, kind=StatusKind.DUPLICATE)
    await dispatcher._handle(item)
    assert bad.recorded == 0


# ------------------------------------------------------------------ reports


def _status(env_hash: str) -> agent_pb2.AttemptStatus:
    return agent_pb2.AttemptStatus(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        state=attempt_state_to_proto(AttemptState.FAILED),
        failure=common_pb2.Failure(
            failure_type=failure_type_to_proto(FailureType.ENV_ERROR), message="x"
        ),
        env_hash=env_hash,
    )


def test_the_reported_env_hash_is_validated() -> None:
    ok = parse_status(_status(HASH))
    assert isinstance(ok, StatusReport) and ok.env_hash == HASH
    empty = parse_status(_status(""))
    assert isinstance(empty, StatusReport) and empty.env_hash is None
    for bad in ("ab" * 31 + "a", "ab" * 32 + "a", "AB" * 32, "sha256:" + "a" * 64):
        refused = parse_status(_status(bad))
        assert isinstance(refused, str) and "env_hash" in refused


# ----------------------------------------------------------------- settings


def test_known_bad_settings_bounds() -> None:
    assert JobApiSettings().env_bad_ttl_s == 3600.0
    assert JobApiSettings(env_bad_ttl_s=7 * 86_400.0).env_bad_ttl_s == 7 * 86_400.0
    assert JobApiSettings(env_bad_ttl_s=0).env_bad_ttl_s == 0
    for wrong in (7 * 86_400.0 + 1, -1):
        with pytest.raises(ValidationError):
            JobApiSettings(env_bad_ttl_s=wrong)
    assert JobApiSettings(known_bad_envs_max=1_000_000).known_bad_envs_max == 1_000_000
    for wrong_n in (0, 1_000_001):
        with pytest.raises(ValidationError):
            JobApiSettings(known_bad_envs_max=wrong_n)
