# ruff: noqa: S103, PT017
# (adversarial literals / world-writable modes / seeded fuzz are the point of these tests)
"""V0.7 G5 tester round 1: the agent is the last line of the owner's policy (D142).

Everything here goes through the REAL :class:`Executor` and a REAL
:class:`LivePolicy` (policy.toml on disk), with a ``StartAttempt`` that a stale
or lying controller could send (the controller's planner never saw the policy):

* every ``policy_*`` code ends as ``FAILED unauthorized`` + ``policy_denied:<code>``,
  not retryable, before anything is fetched and before any runtime is touched;
* a broken / non-private / symlinked / oversized policy.toml denies everything;
* a live tightening bites the very next start; loosening works within the
  documented limits (a runtime the agent never probed cannot appear).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.policy import LivePolicy, write_overlay
from meshforge.agent.settings import AgentSettings, PolicySettings
from meshforge.common import owner_policy as op
from meshforge.common.errors import FailureType, is_retryable
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = pytest.mark.unit

GIB = 1024**3
DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
DOCKER_GPU = RuntimeAvailability("docker", True, sandboxed=True, gpu_capable=True)
SUB = RuntimeAvailability("subprocess", True)
BASE = {"name": "x", "entrypoint": ["python", "main.py"]}


class _Boom:
    """A runtime that must never be asked to do anything."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(f"the {self.name} runtime was touched ({item})")


def _live(tmp_path: Path, base: PolicySettings, **kw: Any) -> LivePolicy:
    return LivePolicy(
        base,
        tmp_path / "policy.toml",
        gpu_count=kw.pop("gpu_count", 1),
        max_concurrent_attempts=4,
    )


def _start(manifest: dict[str, Any], owner: str = "alice", runtime: str = "") -> Any:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=str(new_attempt_id()),
            fencing_token=1,
            spec=agent_pb2.ExecutionSpec(
                bundle_digest="sha256:" + "0" * 64,
                blob_port=47110,
                fetch_token="tok",
                manifest_json=json.dumps(manifest),
                timeout_s=30,
                runtime=runtime,
                owner_name=owner,
            ),
        ),
    )


def _drive(
    tmp_path: Path,
    live: LivePolicy | None,
    msg: Any,
    *,
    docker: RuntimeAvailability | None = DOCKER,
    sub: RuntimeAvailability | None = SUB,
    allow_unsandboxed_offer: bool = True,
) -> tuple[list[agent_pb2.AttemptStatus], list[str]]:
    fetched: list[str] = []
    sent: list[agent_pb2.AttemptStatus] = []
    ended = asyncio.Event()

    async def fake_fetch(*_a: Any, **_k: Any) -> int:
        fetched.append("fetch")
        raise RuntimeError("stop here: the policy let the job through")

    async def report(_name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        sent.append(copy)
        if attempt_state_from_proto(copy.state).value in ("done", "failed"):
            ended.set()
        return str(new_command_id())

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": True, "allow_unsandboxed": True},
        },
    )
    runtimes = {}
    if docker is not None:
        runtimes["docker"] = _Boom("docker")
    if sub is not None:
        runtimes["subprocess"] = _Boom("subprocess")
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=allow_unsandboxed_offer,
            docker=docker,
            subprocess=sub,
        ),
        runtimes=runtimes,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        executor = Executor(
            settings,
            setup,
            node_id="node_x",
            report=report,
            controller_host=lambda: "127.0.0.1",
            fetch=fake_fetch,
            policy=live,
            cpu_threads=8,
        )
        try:
            executor.handle(msg)
            await asyncio.wait_for(ended.wait(), 20)
        finally:
            await executor.close()

    asyncio.run(scenario())
    return sent, fetched


def _failed(sent: list[agent_pb2.AttemptStatus]) -> tuple[FailureType, str, bool]:
    last = sent[-1]
    assert attempt_state_from_proto(last.state).value == "failed", sent
    assert last.HasField("failure")
    return (
        failure_type_from_proto(last.failure.failure_type),
        last.failure.message,
        bool(last.failure.retryable),
    )


# (policy settings, manifest, start kwargs, offer kwargs, expected code)
OPEN_KW: dict[str, Any] = {"accept_jobs": True, "allow_unsandboxed": True}
CASES = [
    ({"accept_jobs": False}, BASE, {}, {}, "policy_not_accepting"),
    (
        {**OPEN_KW, "allowed_users": ["bob"]},
        BASE,
        {"owner": "alice"},
        {},
        "policy_user_not_allowed",
    ),
    (
        {**OPEN_KW, "allowed_users": ["bob"]},
        BASE,
        {"owner": ""},  # the legacy fabric token: no owner
        {},
        "policy_user_not_allowed",
    ),
    (
        {**OPEN_KW, "allowed_users": ["bob"]},
        BASE,
        {"owner": "bob\n"},  # trailing newline must not match
        {},
        "policy_user_not_allowed",
    ),
    (
        {**OPEN_KW, "allowed_users": ["bob"]},
        BASE,
        {"owner": "bob\u200b"},  # zero-width space after bob
        {},
        "policy_user_not_allowed",
    ),
    (
        {"accept_jobs": True, "allow_unsandboxed": False},
        BASE,
        {},
        {"docker": None},
        "policy_unsandboxed",
    ),
    (
        {"accept_jobs": True, "allow_unsandboxed": False},
        BASE,
        {"runtime": "subprocess"},
        {},
        "policy_unsandboxed",
    ),
    (
        {**OPEN_KW, "allow_network": False},
        {**BASE, "limits": {"network": "on"}},
        {},
        {},
        "policy_network",
    ),
    (
        {**OPEN_KW, "max_ram_bytes": GIB},
        {**BASE, "resources": {"ram_gb": 1.01}},
        {},
        {},
        "policy_ram",
    ),
    (
        {**OPEN_KW, "max_cpu_percent": 25},
        {**BASE, "resources": {"cpus": 2.5}},  # 25% of 8 threads = 2
        {},
        {},
        "policy_cpu",
    ),
    (
        {**OPEN_KW, "max_vram_bytes": GIB},
        {**BASE, "resources": {"gpu": "required", "vram_gb": 1.5}},
        {},
        {"docker": DOCKER_GPU},
        "policy_vram",
    ),
]


@pytest.mark.parametrize(
    ("settings", "manifest", "start", "offer", "code"),
    CASES,
    ids=[c[4] + f"-{i}" for i, c in enumerate(CASES)],
)
def test_a_start_the_planner_never_vetted_is_refused_at_the_agent(
    tmp_path: Path,
    settings: dict[str, Any],
    manifest: dict[str, Any],
    start: dict[str, Any],
    offer: dict[str, Any],
    code: str,
) -> None:
    live = _live(tmp_path, PolicySettings(**settings))
    sent, fetched = _drive(tmp_path, live, _start(manifest, **start), **offer)
    kind, message, retryable = _failed(sent)
    assert kind is FailureType.UNAUTHORIZED
    assert message.startswith(f"policy_denied:{code}:"), message
    assert not is_retryable(kind) and not retryable
    assert fetched == []  # nothing fetched, no runtime touched (_Boom)


def test_exactly_at_the_limit_is_allowed_by_the_rule(tmp_path: Path) -> None:
    """Boundary: a job asking exactly the cap passes the policy check (it then
    stops at the fetch, which this harness forbids -> not a policy_denied)."""
    live = _live(tmp_path, PolicySettings(**OPEN_KW, max_ram_bytes=GIB))
    sent, fetched = _drive(
        tmp_path, live, _start({**BASE, "resources": {"ram_gb": 1.0}})
    )
    assert fetched == ["fetch"], sent[-1].failure.message


# ------------------------------------------------------------------ broken file


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_bytes(text.encode("utf-8", "surrogateescape"))
    os.chmod(path, mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize(
    "how",
    [
        "garbage",
        "world_writable",
        "group_readable_writable",
        "symlink",
        "huge",
        "unknown_key",
        "directory",
    ],
)
def test_a_broken_or_unsafe_policy_file_denies_every_start(
    tmp_path: Path, how: str
) -> None:
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    assert live.policy.accept_jobs
    path = live.path
    if how == "garbage":
        _write(path, "\x00\xff not toml")
    elif how == "world_writable":
        _write(path, "accept_jobs = true\nallow_network = true\n", 0o666)
    elif how == "group_readable_writable":
        _write(path, "accept_jobs = true\n", 0o660)
    elif how == "symlink":
        target = tmp_path / "t.toml"
        _write(target, "accept_jobs = true\n")
        path.symlink_to(target)
    elif how == "huge":
        _write(path, "allowed_users = [" + ",".join(['"a"'] * 50000) + "]\n")
    elif how == "unknown_key":
        _write(path, "accept_jobs = true\nsudo = true\n")
    elif how == "directory":
        path.mkdir()
    live.reload()
    assert live.policy == op.DENY_ALL and live.slots == (0, 0), how
    sent, fetched = _drive(tmp_path, live, _start(BASE))
    kind, message, _ = _failed(sent)
    assert kind is FailureType.UNAUTHORIZED
    assert message.startswith("policy_denied:policy_not_accepting")
    assert fetched == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_file_first_seen_non_private_at_startup_denies_from_the_start(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "policy.toml", "allow_network = true\n", 0o644)
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    assert live.policy == op.DENY_ALL
    assert live.state.invalid_reason


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_chmod_after_load_is_noticed_on_the_next_reload(tmp_path: Path) -> None:
    """A file loosened to world-writable (content untouched) must stop being
    trusted - another local user could then rewrite it."""
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    write_overlay(live.path, {"allow_network": True})
    assert live.reload() and live.policy.allow_network
    os.chmod(live.path, 0o666)
    live.reload()
    assert live.policy == op.DENY_ALL


# ------------------------------------------------------------------ live changes


def test_tightening_bites_the_next_start_and_loosening_recovers(
    tmp_path: Path,
) -> None:
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    write_overlay(live.path, {"allowed_users": ["bob"]})
    assert live.reload()
    sent, fetched = _drive(tmp_path, live, _start(BASE, owner="alice"))
    assert _failed(sent)[1].startswith("policy_denied:policy_user_not_allowed")
    write_overlay(live.path, {"allowed_users": ["alice"]})
    assert live.reload()
    # now alice passes the policy: the harness's fetch is reached
    _, fetched = _drive(tmp_path, live, _start(BASE, owner="alice"))
    assert fetched == ["fetch"]


def test_turning_accept_jobs_off_live_refuses_the_next_start(tmp_path: Path) -> None:
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    write_overlay(live.path, {"accept_jobs": False})
    assert live.reload()
    sent, _ = _drive(tmp_path, live, _start(BASE))
    assert _failed(sent)[1].startswith("policy_denied:policy_not_accepting")


def test_allow_unsandboxed_off_live_hides_the_subprocess_runtime(
    tmp_path: Path,
) -> None:
    live = _live(tmp_path, PolicySettings(**OPEN_KW))
    write_overlay(live.path, {"allow_unsandboxed": False})
    assert live.reload()
    sent, _ = _drive(tmp_path, live, _start(BASE, runtime="subprocess"))
    assert _failed(sent)[1].startswith("policy_denied:policy_unsandboxed")


def test_allow_unsandboxed_on_live_needs_a_restart_and_is_not_faked(
    tmp_path: Path,
) -> None:
    """Started with the unsandboxed runtimes off (never probed): turning it on
    live must not conjure a runtime - the documented restart requirement."""
    live = _live(tmp_path, PolicySettings(accept_jobs=True, allow_unsandboxed=False))
    write_overlay(live.path, {"allow_unsandboxed": True})
    assert live.reload() and live.policy.allow_unsandboxed
    sent, fetched = _drive(
        tmp_path,
        live,
        _start(BASE, runtime="subprocess"),
        docker=None,
        sub=None,
        allow_unsandboxed_offer=False,
    )
    kind, message, _ = _failed(sent)
    assert kind is FailureType.NO_FEASIBLE_PLAN, message
    assert fetched == []


# ------------------------------------------------------------------ D143 manifests


@pytest.mark.parametrize("network", ["default", "none", "on", None])
@pytest.mark.parametrize("allow", [True, False])
def test_the_network_matrix_for_old_and_new_manifests(
    tmp_path: Path, network: str | None, allow: bool
) -> None:
    from meshforge.agent.execution.plan import PlanError, plan_attempt
    from meshforge.agent.execution.selection import execution_offer
    from meshforge.agent.settings import ExecutionSettings

    manifest: dict[str, Any] = dict(BASE)
    if network is not None:  # a V0.4-V0.6 manifest without the key at all
        manifest["limits"] = {"network": network}
    policy = op.OwnerPolicy(accept_jobs=True, allow_network=allow)
    msg = _start(manifest).start_attempt
    offer = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=DOCKER, subprocess=None
    )
    try:
        plan = plan_attempt(
            msg,
            settings=ExecutionSettings(),
            offer=offer,
            declared_gpus=(),
            workdir=tmp_path,
            policy=policy,
            cpu_threads=8,
        )
    except PlanError as exc:
        assert network == "on" and not allow
        assert exc.message.startswith("policy_denied:policy_network:")
        return
    # old manifests (key absent / "default" / "none") never get a network
    assert plan.spec.network is (network == "on" and allow)


# ------------------------------------------------------------------ restart remedy


@pytest.mark.asyncio
async def test_a_restart_after_policy_set_probes_the_runtimes_the_overlay_enables(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.offer import build_execution
    from meshforge.agent.settings import AgentSettings

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": False, "allow_unsandboxed": False},
        },
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    write_overlay(
        settings.data_dir / "policy.toml",
        {"accept_jobs": True, "allow_unsandboxed": True},
    )  # what `meshforge-agent policy set accept_jobs=true allow_unsandboxed=true` writes
    # the live policy of the restarted agent says: accepting, unsandboxed allowed
    live = LivePolicy(
        settings.agent.policy,
        settings.data_dir / "policy.toml",
        gpu_count=0,
        max_concurrent_attempts=4,
    )
    assert live.policy.accept_jobs and live.policy.allow_unsandboxed
    setup = await build_execution(settings, node_id="node_x", host_has_nvidia_gpu=False)
    assert setup.offer.enabled, (
        "the agent accepts jobs per its policy but has no runtime"
    )
    assert "subprocess" in setup.runtimes
