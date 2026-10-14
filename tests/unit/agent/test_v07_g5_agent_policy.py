"""V0.7 G5 (roadmap 0.7.8, D142): the owner policy on the agent.

* ``[agent.policy]`` and the one-release migration of the old
  ``[agent.execution]`` keys;
* :class:`LivePolicy`: the live file ``policy set`` writes - applied, validated
  as a whole, failing closed when broken or not private;
* the CLI parsing and the file format;
* defence in depth: :func:`plan_attempt` re-checks every StartAttempt against
  the current policy (every ``policy_*`` code) and caps the run with it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.execution.plan import PlanError, plan_attempt
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.policy import (
    LivePolicy,
    format_overlay,
    parse_assignments,
    read_overlay,
    write_overlay,
)
from meshforge.agent.settings import AgentSection, ExecutionSettings, PolicySettings
from meshforge.common import owner_policy as op
from meshforge.common.errors import ConfigError, FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

GIB = 1024**3
MIB = 1024**2
DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
DOCKER_GPU = RuntimeAvailability("docker", True, sandboxed=True, gpu_capable=True)
SUB = RuntimeAvailability("subprocess", True)
OPEN = op.OPEN_POLICY


def _section(**kw: Any) -> AgentSection:
    return AgentSection.model_validate({"controller_address": "127.0.0.1:47100", **kw})


# ------------------------------------------------------------------ settings


def test_policy_defaults_are_closed() -> None:
    policy = PolicySettings()
    assert policy.accept_jobs is False and policy.allow_unsandboxed is False
    assert policy.allow_network is False and policy.allowed_users == []
    assert policy.max_ram_bytes is None and policy.max_cpu_percent is None
    assert policy.to_policy() == op.DENY_ALL


@pytest.mark.parametrize(
    ("old", "new_key", "value"),
    [
        ({"enabled": False}, "accept_jobs", False),
        ({"enabled": True}, "accept_jobs", True),
        ({"allow_unsandboxed": True}, "allow_unsandboxed", True),
        ({"max_cpu_jobs": 2}, "max_cpu_jobs", 2),
        ({"max_gpu_jobs": 0}, "max_gpu_jobs", 0),
    ],
)
def test_the_old_execution_keys_are_still_read(
    old: dict[str, Any], new_key: str, value: object
) -> None:
    section = _section(execution=old)
    assert getattr(section.policy, new_key) == value
    assert section.deprecated_keys == tuple(f"execution.{k}" for k in old)


def test_a_policy_key_wins_over_its_deprecated_spelling() -> None:
    section = _section(execution={"enabled": True}, policy={"accept_jobs": False})
    assert section.policy.accept_jobs is False
    assert section.deprecated_keys == ("execution.enabled",)
    assert _section().deprecated_keys == ()


def test_allowed_users_must_be_user_names() -> None:
    assert PolicySettings(allowed_users=[" alice ", "bob"]).allowed_users == [
        "alice",
        "bob",
    ]
    for bad in (["Alice"], ["a b"], ["alice", "alice"], [f"u{i}" for i in range(65)]):
        with pytest.raises(ValidationError):
            PolicySettings(allowed_users=bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"max_ram_bytes": 1},
        {"max_cpu_percent": 0},
        {"max_cpu_percent": 101},
        {"max_vram_bytes": 0},
        {"unknown": 1},
    ],
)
def test_policy_bounds(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PolicySettings.model_validate(bad)


# ------------------------------------------------------------------ live file


def _live(tmp_path: Path, base: PolicySettings | None = None) -> LivePolicy:
    return LivePolicy(
        base or PolicySettings(accept_jobs=True),
        tmp_path / "policy.toml",
        gpu_count=1,
        max_concurrent_attempts=4,
    )


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, "utf-8")
    os.chmod(path, mode)
    # A new mtime even within the file system's timestamp granularity.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_no_file_is_the_configuration(tmp_path: Path) -> None:
    live = _live(tmp_path)
    assert live.policy.accept_jobs is True
    assert live.slots == (1, 1)
    assert live.state.invalid_reason is None and live.state.overlay is None
    assert live.reload() is False


def test_a_change_is_applied_and_counted(tmp_path: Path) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"max_ram_bytes": 2 * GIB, "allowed_users": ["alice"]})
    assert live.reload() is True
    assert live.policy.max_ram_bytes == 2 * GIB
    assert live.policy.allowed_users == ("alice",)
    assert live.policy.revision == 1 and live.revision == 1
    assert live.reload() is False  # unchanged file: no change
    write_overlay(live.path, {"max_ram_bytes": 2 * GIB, "allowed_users": ["alice"]})
    assert live.reload() is False  # rewritten but equal: no change


def test_none_removes_a_configured_limit(tmp_path: Path) -> None:
    live = _live(tmp_path, PolicySettings(accept_jobs=True, max_ram_bytes=4 * GIB))
    assert live.policy.max_ram_bytes == 4 * GIB
    write_overlay(live.path, {"max_ram_bytes": None})
    assert b'max_ram_bytes = "none"' in live.path.read_bytes()
    assert live.reload() is True
    assert live.policy.max_ram_bytes is None


@pytest.mark.security
@pytest.mark.parametrize(
    "text",
    [
        "accept_jobs = maybe\n",
        "not_a_key = 1\n",
        "max_cpu_percent = 500\n",
        "allowed_users = ['Not A Name']\n",
        "max_cpu_jobs = 9\n",  # 9 + 1 GPU slot > max_concurrent_attempts (4)
        "\x00\x01",
    ],
)
def test_a_broken_live_file_fails_closed(tmp_path: Path, text: str) -> None:
    live = _live(tmp_path)
    _write(live.path, text)
    assert live.reload() is True
    assert live.policy == op.DENY_ALL
    assert live.slots == (0, 0)
    assert live.state.invalid_reason
    # Fixing the file restores the policy.
    write_overlay(live.path, {"accept_jobs": True})
    assert live.reload() is True
    assert live.policy.accept_jobs is True and live.state.invalid_reason is None


@pytest.mark.security
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_live_file_others_can_write_is_refused(tmp_path: Path) -> None:
    live = _live(tmp_path)
    _write(live.path, "accept_jobs = true\nallow_network = true\n", mode=0o666)
    assert live.reload() is True
    assert live.policy == op.DENY_ALL
    assert "mode" in (live.state.invalid_reason or "")


@pytest.mark.security
@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_symlinked_live_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.toml"
    _write(target, "allow_network = true\n")
    live = _live(tmp_path)
    live.path.symlink_to(target)
    assert live.reload() is True
    assert live.policy == op.DENY_ALL


def test_assignments_and_file_format() -> None:
    values = parse_assignments(
        [
            "accept_jobs=yes",
            "allow_network=off",
            "max_ram_bytes=8GiB",
            "max_vram_bytes=512MiB",
            "max_cpu_percent=50",
            "max_gpu_jobs=none",
            "allowed_users=alice, bob",
        ]
    )
    assert values == {
        "accept_jobs": True,
        "allow_network": False,
        "max_ram_bytes": 8 * GIB,
        "max_vram_bytes": 512 * MIB,
        "max_cpu_percent": 50,
        "max_gpu_jobs": None,
        "allowed_users": ["alice", "bob"],
    }
    text = format_overlay(values).decode()
    assert 'allowed_users = ["alice", "bob"]' in text
    assert 'max_gpu_jobs = "none"' in text


@pytest.mark.parametrize(
    "item",
    [
        "accept_jobs",
        "accept_jobs=perhaps",
        "reload_interval_s=1",
        "max_cpu_percent=50GiB",
        "max_ram_bytes=1.5",
        "max_ram_bytes=-1",
        "max_ram_bytes=lots",
        "nonsense=1",
    ],
)
def test_bad_assignments_are_refused(item: str) -> None:
    with pytest.raises(ConfigError):
        parse_assignments([item])


def test_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    values = {"accept_jobs": True, "max_ram_bytes": None, "allowed_users": ["a"]}
    write_overlay(path, values)
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert read_overlay(path) == values


# ------------------------------------------------------------------ plan_attempt


def _msg(manifest: dict[str, Any], *, owner: str = "alice", runtime: str = "") -> Any:
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
            runtime=runtime,
            owner_name=owner,
            # what the controller sends (jobs/spec.py): the manifest's asks
            cpus=float(manifest.get("resources", {}).get("cpus", 0)),
        ),
    )


BASE = {"name": "x", "entrypoint": ["python", "main.py"]}


def _plan(
    tmp_path: Path,
    manifest: dict[str, Any],
    policy: op.OwnerPolicy,
    *,
    owner: str = "alice",
    runtime: str = "",
    docker: bool = True,
    gpu: bool = False,
    threads: int | None = 8,
) -> Any:
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=(DOCKER_GPU if gpu else DOCKER) if docker else None,
        subprocess=SUB,
    )
    return plan_attempt(
        _msg(manifest, owner=owner, runtime=runtime),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
        policy=policy,
        cpu_threads=threads,
    )


def _denied(exc: pytest.ExceptionInfo[PlanError]) -> str:
    assert exc.value.failure_type is FailureType.UNAUTHORIZED
    message = exc.value.message
    assert message.startswith("policy_denied:")
    return message.split(":", 2)[1]


@pytest.mark.security
@pytest.mark.parametrize(
    ("manifest", "policy", "kw", "code"),
    [
        (BASE, op.DENY_ALL, {}, "policy_not_accepting"),
        (
            BASE,
            dataclasses.replace(OPEN, allowed_users=("bob",)),
            {},
            "policy_user_not_allowed",
        ),
        (
            BASE,
            dataclasses.replace(OPEN, allowed_users=("bob",)),
            {"owner": ""},
            "policy_user_not_allowed",
        ),
        (
            BASE,
            dataclasses.replace(OPEN, allowed_users=("bob",)),
            {"owner": "BOB"},  # not a user name: an unknown owner, never bob
            "policy_user_not_allowed",
        ),
        (
            BASE,
            dataclasses.replace(OPEN, allow_unsandboxed=False),
            {"docker": False},
            "policy_unsandboxed",
        ),
        (
            BASE,
            dataclasses.replace(OPEN, allow_unsandboxed=False),
            {"runtime": "subprocess"},
            "policy_unsandboxed",
        ),
        (
            {**BASE, "limits": {"network": "on"}},
            dataclasses.replace(OPEN, allow_network=False),
            {},
            "policy_network",
        ),
        (
            {**BASE, "resources": {"ram_gb": 2}},
            dataclasses.replace(OPEN, max_ram_bytes=GIB),
            {},
            "policy_ram",
        ),
        (
            {**BASE, "resources": {"cpus": 5}},
            dataclasses.replace(OPEN, max_cpu_percent=50),
            {},
            "policy_cpu",
        ),
        (
            {**BASE, "resources": {"gpu": "required", "vram_gb": 2}},
            dataclasses.replace(OPEN, max_vram_bytes=GIB),
            {"gpu": True},
            "policy_vram",
        ),
    ],
)
def test_the_agent_refuses_every_policy_violation(
    tmp_path: Path,
    manifest: dict[str, Any],
    policy: op.OwnerPolicy,
    kw: dict[str, Any],
    code: str,
) -> None:
    with pytest.raises(PlanError) as exc:
        _plan(tmp_path, manifest, policy, **kw)
    assert _denied(exc) == code


def test_a_sandboxed_runtime_is_chosen_when_unsandboxed_is_off(tmp_path: Path) -> None:
    plan = _plan(tmp_path, BASE, dataclasses.replace(OPEN, allow_unsandboxed=False))
    assert plan.runtime == "docker"


@pytest.mark.security
def test_the_owner_limits_cap_the_run(tmp_path: Path) -> None:
    policy = dataclasses.replace(OPEN, max_ram_bytes=GIB, max_cpu_percent=25)
    plan = _plan(tmp_path, BASE, policy)
    # default 4096 MiB memory capped at 1 GiB; no CPU ask -> 25% of 8 threads
    assert plan.spec.limits.memory_bytes == GIB
    assert plan.spec.limits.cpus == 2.0
    small = _plan(tmp_path, {**BASE, "resources": {"ram_gb": 0.5, "cpus": 1}}, policy)
    assert small.spec.limits.cpus == 1.0


@pytest.mark.security
def test_network_only_when_asked_and_allowed(tmp_path: Path) -> None:
    on = {**BASE, "limits": {"network": "on"}}
    assert _plan(tmp_path, on, OPEN).spec.network is True
    assert _plan(tmp_path, BASE, OPEN).spec.network is False  # D143: default none
    assert (
        _plan(tmp_path, {**BASE, "limits": {"network": "none"}}, OPEN).spec.network
        is False
    )


def test_an_allowed_user_runs(tmp_path: Path) -> None:
    policy = dataclasses.replace(OPEN, allowed_users=("alice", "bob"))
    assert _plan(tmp_path, BASE, policy, owner="bob").runtime == "docker"


@pytest.mark.security
def test_gpu_preferred_falls_back_to_the_sandbox_rather_than_unsandboxed(
    tmp_path: Path,
) -> None:
    """Docker without GPU pass-through + a GPU-capable subprocess runtime: with
    unsandboxed runtimes off, a ``gpu: preferred`` job runs in Docker on the
    CPU instead of being refused (the selection skips unsandboxed runtimes,
    not only the check after it)."""
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=DOCKER,
        subprocess=RuntimeAvailability("subprocess", True, gpu_capable=True),
    )
    plan = plan_attempt(
        _msg({**BASE, "resources": {"gpu": "preferred"}}),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
        policy=dataclasses.replace(OPEN, allow_unsandboxed=False),
        cpu_threads=8,
    )
    assert plan.runtime == "docker" and plan.spec.gpu_indices == ()
