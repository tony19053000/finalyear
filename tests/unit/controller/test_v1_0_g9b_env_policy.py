"""V1.0 G9-B item 3 (D433): known-bad environments follow the owner's env policy.

* the agent's fingerprint covers exactly the build-deciding ``[agent.envs]``
  fields (only_binary, indexes, wheelhouse, size limits) - 12 lowercase hex;
* the tag round-trips; a malformed or doubled tag is "unknown";
* an entry recorded under fingerprint A is dropped when the node now says B,
  kept when it says A or nothing (an older agent: the TTL rule as before);
* the dispatcher records the fingerprint of the node's live session;
* the planner asks with the node's current capabilities.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from test_dispatcher_d75 import FakeManager, FakeStore

from meshforge.agent.envs.policy import env_policy_capability, env_policy_fingerprint
from meshforge.agent.hardware.limits import clip_capabilities
from meshforge.agent.settings import EnvsSettings
from meshforge.common.capabilities import (
    ENV_POLICY_PREFIX,
    env_policy_of,
    env_policy_tag,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.env_health import KnownBadEnvs
from meshforge.controller.jobs.execution_store import (
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.settings import JobApiSettings

pytestmark = pytest.mark.unit

A, B = "a" * 12, "b" * 12


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------ the fingerprint


def test_the_fingerprint_moves_with_every_build_deciding_field(
    tmp_path: Path,
) -> None:
    base = env_policy_fingerprint(EnvsSettings())
    assert len(base) == 12 and int(base, 16) >= 0 and base == base.lower()
    assert env_policy_fingerprint(EnvsSettings()) == base  # deterministic
    changed = [
        EnvsSettings(only_binary=False),
        EnvsSettings(index_url="https://mirror.example/simple"),
        EnvsSettings(extra_index_urls=["https://download.pytorch.org/whl/cu126"]),
        EnvsSettings(find_links=tmp_path),
        EnvsSettings(find_links=tmp_path, no_index=True),
        EnvsSettings(allow_python_downloads=True),
        EnvsSettings(max_env_bytes=32 * 1024**3),
        EnvsSettings(max_requirements_bytes=1024),
        EnvsSettings(max_requirements_lines=10),
    ]
    prints = [env_policy_fingerprint(cfg) for cfg in changed]
    assert base not in prints
    assert len(set(prints)) == len(prints)


def test_fields_that_do_not_decide_a_build_keep_it(tmp_path: Path) -> None:
    base = env_policy_fingerprint(EnvsSettings())
    for same in (
        EnvsSettings(dir=tmp_path),
        EnvsSettings(evicted_keep_s=5.0),
        EnvsSettings(free_disk_floor_mb=1),
    ):
        assert env_policy_fingerprint(same) == base


def test_the_agent_advertises_it_and_the_hello_clip_keeps_it() -> None:
    tag = env_policy_capability(EnvsSettings(only_binary=False))
    assert env_policy_of(clip_capabilities(["exec", "exec-venv", tag])) == (
        env_policy_fingerprint(EnvsSettings(only_binary=False))
    )


def test_the_tag_round_trips_and_bad_tags_are_unknown() -> None:
    tag = env_policy_tag(A)
    assert tag == ENV_POLICY_PREFIX + A and len(tag) <= 64
    assert env_policy_of(["exec", tag]) == A
    assert env_policy_of(["exec"]) is None
    assert env_policy_of([tag, env_policy_tag(B)]) is None  # two: not trusted
    assert env_policy_of([tag, tag]) == A
    for bad in ("env-policy:", "env-policy:ABCDEFABCDEF", "env-policy:" + "a" * 13):
        assert env_policy_of([bad]) is None
    with pytest.raises(ValueError):
        env_policy_tag("xyz")


# --------------------------------------------------------------- the map


def _bad(clock: _Clock, *, policy: str | None) -> KnownBadEnvs:
    envs = KnownBadEnvs(ttl_s=60.0, clock=clock)
    envs.record(
        "node_a", "k", env_hash=None, job_id="j", attempt_id="a", env_policy=policy
    )
    return envs


def test_a_changed_policy_drops_the_entry_and_logs_it() -> None:
    clock = _Clock()
    envs = _bad(clock, policy=A)
    assert envs.is_bad("node_a", "k", env_policy=A)  # same policy: still bad
    with structlog.testing.capture_logs() as logs:
        assert not envs.is_bad("node_a", "k", env_policy=B)
    assert len(envs) == 0 and envs.cleared == 1
    (event,) = [
        e for e in logs if e["event"] == "controller.jobs.env_known_bad_cleared"
    ]
    assert event["env_policy_then"] == A and event["env_policy_now"] == B
    assert event["reason"] == "env_policy_changed"
    # gone for good: switching back does not bring it back
    assert not envs.is_bad("node_a", "k", env_policy=A)


@pytest.mark.parametrize(("then", "now"), [(A, None), (None, B), (None, None)])
def test_unknown_on_either_side_is_the_old_ttl_rule(
    then: str | None, now: str | None
) -> None:
    clock = _Clock()
    envs = _bad(clock, policy=then)
    assert envs.is_bad("node_a", "k", env_policy=now)
    clock.now += 59.9
    assert envs.is_bad("node_a", "k", env_policy=now)
    clock.now += 0.1
    assert not envs.is_bad("node_a", "k", env_policy=now)
    assert envs.cleared == 0


def test_a_new_failure_under_the_new_policy_is_recorded_with_it() -> None:
    clock = _Clock()
    envs = _bad(clock, policy=A)
    envs.record(
        "node_a", "k", env_hash=None, job_id="j2", attempt_id="a2", env_policy=B
    )
    assert envs.is_bad("node_a", "k", env_policy=B)
    assert not envs.is_bad("node_a", "k", env_policy=A)


# ----------------------------------------------------- dispatcher + planner


def _dispatcher(manager: FakeManager, envs: KnownBadEnvs) -> Dispatcher:
    return Dispatcher(
        store=FakeStore(),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
        known_bad_envs=envs,
    )


@pytest.mark.parametrize("policy", [A, None])
def test_the_dispatcher_records_the_sessions_fingerprint(policy: str | None) -> None:
    node = str(new_node_id())
    manager = FakeManager()
    manager.live[node] = SimpleNamespace(env_policy=policy)
    envs = KnownBadEnvs(ttl_s=60.0, clock=_Clock())
    dispatcher = _dispatcher(manager, envs)
    attempt = SimpleNamespace(id=str(new_attempt_id()), node_id=node)
    job = SimpleNamespace(
        id=str(new_job_id()),
        manifest={"name": "x"},
        tree_id="sha256:" + "1" * 64,
        bundle_digest=None,
    )
    outcome: Any = StatusOutcome(StatusKind.APPLIED, attempt, job, True)
    report = StatusReport(
        attempt.id, 1, AttemptState.FAILED, failure_type=FailureType.ENV_ERROR
    )
    dispatcher._remember_bad_env(outcome, report)
    (entry,) = envs.for_node(node)
    assert entry.env_policy == policy


def test_no_live_session_records_unknown() -> None:
    node = str(new_node_id())
    envs = KnownBadEnvs(ttl_s=60.0, clock=_Clock())
    dispatcher = _dispatcher(FakeManager(), envs)
    attempt = SimpleNamespace(id=str(new_attempt_id()), node_id=node)
    job = SimpleNamespace(
        id=str(new_job_id()),
        manifest={},
        tree_id=None,
        bundle_digest="sha256:" + "2" * 64,
    )
    outcome: Any = StatusOutcome(StatusKind.APPLIED, attempt, job, True)
    dispatcher._remember_bad_env(
        outcome,
        StatusReport(
            attempt.id, 1, AttemptState.FAILED, failure_type=FailureType.ENV_ERROR
        ),
    )
    (entry,) = envs.for_node(node)
    assert entry.env_policy is None
