"""Agent settings (``[agent]`` section, roadmap 0.3.8 / 0.3.9).

Layout follows :mod:`meshforge.common.config`: one TOML file may serve every process
on a machine, so agent keys live in their own ``[agent]`` table
(``MESHFORGE_AGENT__CONTROLLER_ADDRESS=192.168.1.20:47100`` from the environment).
Common keys (``data_dir``, ``[log]``) are inherited from
:class:`~meshforge.common.config.MeshForgeSettings`.

Where things live (ARCHITECTURE §23): the node identity is ``data_dir/identity``
(:attr:`AgentSettings.identity_dir`); the controller is reached at
``agent.controller_address`` or, when that is empty, found on the LAN via mDNS
(``agent.discovery``). Reconnect timing comes from ADR-0002 Updates §3, gRPC
keepalive from the same section; the replay ring size from Updates §4. Hardware
discovery (``[agent.hardware]``, roadmap 0.3.3) and the live resource monitor
(``[agent.resources]``, roadmap 0.3.5) and hardware scores (``[agent.scores]``,
roadmap 0.3.4: ``<data_dir>/scores.json`` + ``scores.lock``) and job execution
(``[agent.execution]``, V0.4 D45/D51: off by default, ``<data_dir>/work``) are
documented on their own sections below.

This module imports no agent-extra package so ``meshforge-agent --version`` works
on a base install.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator, model_validator

from meshforge import __version__
from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError

__all__ = [
    "CUDA_PYTORCH_IMAGE",
    "DEFAULT_SERVICE_TYPE",
    "GRACE_BOUND_S",
    "IDENTITY_DIR_NAME",
    "PYTHON_CPU_IMAGE",
    "AgentSection",
    "AgentSettings",
    "DiscoverySettings",
    "ExecutionImages",
    "ExecutionSettings",
    "HardwareSettings",
    "KeepaliveSettings",
    "ReconnectSettings",
    "ResourceSettings",
    "ScoreSettings",
    "parse_controller_address",
]

DEFAULT_SERVICE_TYPE: Final = "_meshforge-ctl._tcp.local."
"""mDNS service type the controller advertises and the agent browses for."""

IDENTITY_DIR_NAME: Final = "identity"
"""Sub-directory of ``data_dir`` holding ``node.key`` / ``node.json`` (ADR-0013)."""

_PORT_MAX: Final = 65535
_AGENT_VERSION_MAX_LENGTH: Final = 64
"""Matches the controller's ``Hello.agent_version`` cap (``node_session.agent_version``)."""


def parse_controller_address(text: str) -> tuple[str, int]:
    """Parse ``"host:port"`` / ``"[v6]:port"`` into ``(host, port)``.

    The agent *dials out*, so a hostname is acceptable here (the controller's bind
    rules in :mod:`meshforge.common.netaddr` are about listening). Raises
    :class:`~meshforge.common.errors.ConfigError` on a malformed value.
    """
    raw = text.strip()
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ConfigError(
                f"controller_address must look like [ipv6]:port, got {text!r}",
                details={"controller_address": text},
            )
        port_text = rest[1:]
    else:
        host, sep, port_text = raw.rpartition(":")
        if not sep or ":" in host:
            raise ConfigError(
                f"controller_address must look like host:port (bracket IPv6), got {text!r}",
                details={"controller_address": text},
            )
    if not host:
        raise ConfigError(
            f"controller_address has an empty host: {text!r}",
            details={"controller_address": text},
        )
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(
            f"controller_address has a non-numeric port: {text!r}",
            details={"controller_address": text},
        ) from None
    if not 1 <= port <= _PORT_MAX:
        raise ConfigError(
            f"controller_address port must be 1..{_PORT_MAX}, got {port}",
            details={"controller_address": text},
        )
    return host, port


RECONNECT_BASE_S: Final = 0.5
"""Default first backoff ceiling (ADR-0002 Updates §3)."""

RECONNECT_CAP_BOUND_S: Final = 5.0
"""Largest allowed ``[agent.reconnect] cap_s`` - the V0.3 **criterion 2** bound.

Criterion 2: a restored link -> node ONLINE within 10 s, with no agent restart.
Nothing wakes a backoff sleep early when the link returns, so after a long outage
the restore can land at the start of a sleep as long as the cap: the cap is added
to the restore latency as-is. At 5 s that leaves ~5 s for the connect + Hello
round trip (worst ~6 s on a LAN; D27, ADR-0002 Updates amended 2026-09-24). Raising
it past 5 s eats that margin; criterion 3 (reconnect within 15 s of a controller
restart) alone would allow 15 s, so it is criterion 2 that binds.
"""

RECONNECT_ATTEMPT_BUDGET_S: Final = 8.0
"""Largest allowed ``cap_s + hello_timeout_s`` - the rest of the criterion 2 arithmetic.

An attempt that starts just before the link returns can spend a whole
``hello_timeout_s`` on the dead path and *then* sleep up to ``cap_s`` before the
attempt that succeeds (D29, 2026-09-24). Both must fit under criterion 2's 10 s
with room for the successful connect + Hello round trip: 3 s + 5 s = 8 s leaves
~2 s. Enforced by :meth:`AgentSection._criterion_2_budget`.
"""

RECONNECT_CAP_S: Final = RECONNECT_CAP_BOUND_S
"""Default ``cap_s``; the single source of truth for :class:`~meshforge.agent.
session.policy.BackoffPolicy` too."""

RECONNECT_RESET_AFTER_S: Final = 30.0
"""Default stream lifetime that resets the backoff exponent."""


class ReconnectSettings(SectionModel):
    """``[agent.reconnect]`` - full-jitter exponential backoff (ADR-0002 Updates §3).

    ``sleep = uniform(0, min(cap_s, base_s * 2**n))``; ``n`` resets to 0 after a
    stream that lived at least ``reset_after_s``. ``cap_s`` must stay <=
    :data:`RECONNECT_CAP_BOUND_S` (5 s): V0.3 criterion 2 requires a restored link
    to be ONLINE again within 10 s (see that constant for the arithmetic).
    """

    base_s: float = Field(
        default=RECONNECT_BASE_S, gt=0, description="First backoff ceiling."
    )
    cap_s: float = Field(
        default=RECONNECT_CAP_S,
        gt=0,
        # V0.3 criterion 2 (restored link -> ONLINE <= 10 s): see RECONNECT_CAP_BOUND_S.
        le=RECONNECT_CAP_BOUND_S,
        description=(
            "Upper bound of the backoff ceiling (<= 5 s, V0.3 criterion 2: "
            "restored link -> ONLINE within 10 s)."
        ),
    )
    reset_after_s: float = Field(
        default=RECONNECT_RESET_AFTER_S,
        ge=0,
        description="A stream that lived this long resets the backoff exponent.",
    )

    @model_validator(mode="after")
    def _cap_not_below_base(self) -> ReconnectSettings:
        if self.cap_s < self.base_s:
            raise ValueError("reconnect.cap_s must be >= reconnect.base_s")
        return self


class KeepaliveSettings(SectionModel):
    """``[agent.keepalive]`` - gRPC transport keepalive (ADR-0002 Updates §3).

    A dead TCP path is torn down by the transport after ``time_ms + timeout_ms``
    without waiting on the application lease.
    """

    time_ms: int = Field(
        default=10_000, ge=1_000, description="Interval between keepalive pings."
    )
    timeout_ms: int = Field(
        default=5_000, ge=100, description="Ping unanswered this long = dead path."
    )


class HardwareSettings(SectionModel):
    """``[agent.hardware]`` - static hardware discovery (roadmap 0.3.3).

    One blocking pass runs at start-up in a thread and its result is cached for the
    process (``HardwareProvider``). ``timeout_s`` is the budget for **all** probes
    together: whatever has not answered when it runs out is reported as ``timeout``
    and the Hello simply lacks that field (D10 - a probe costs a field, never the
    agent). Raise it on a laptop whose ``nvidia-smi`` or Docker daemon is slow to
    wake; the agent's start-up is delayed by at most this long.

    The budget is enforced *around* the pass as well as inside it: NVML is plain
    synchronous FFI with no cancellation point, so the pass runs on a deadline
    thread (:mod:`meshforge.agent.hardware.deadline`) and start-up gives up after
    ``timeout_s`` plus a small grace, falling back to the basic summary.
    """

    timeout_s: float = Field(
        default=5.0,
        gt=0,
        le=60.0,
        description=(
            "Total budget for one hardware-discovery pass; the agent's start-up "
            "waits at most this long before sending Hello."
        ),
    )


class ResourceSettings(SectionModel):
    """``[agent.resources]`` - the live resource monitor (roadmap 0.3.5).

    Sampling is unconditional while the agent runs; *reporting* is
    change-triggered (§2 D5): a report is sent when a metric moved by more than
    ``change_threshold_percent``, and otherwise at least every
    ``max_report_interval_s``. While the session is not ONLINE the monitor keeps
    sampling but sends nothing (D9) - buffering hundreds of stale samples would
    evict real reports from the replay ring - and the first sample after the
    session is ONLINE again is always sent.
    """

    enabled: bool = Field(
        default=True,
        description="Run the resource monitor. Off = the node reports no usage.",
    )
    sample_interval_s: float = Field(
        default=2.0,
        gt=0,
        le=300.0,
        description="Seconds between samples; matches the controller's live ring.",
    )
    change_threshold_percent: float = Field(
        default=5.0,
        ge=0,
        le=100.0,
        description=(
            "Report at once when a metric moves more than this: percentage points "
            "for CPU/GPU utilisation, relative percent for byte metrics."
        ),
    )
    max_report_interval_s: float = Field(
        default=10.0,
        gt=0,
        le=3600.0,
        description="Report at least this often even when nothing changed.",
    )
    max_gpus: int = Field(
        default=16,
        ge=1,
        le=16,
        description="GPU entries per report (the controller's cap is the same).",
    )
    max_disks: int = Field(
        default=16,
        ge=1,
        le=16,
        description="Disk entries per report (the controller's cap is the same).",
    )
    sample_timeout_s: float = Field(
        default=5.0,
        gt=0,
        le=60.0,
        description=(
            "Give up waiting for one sample after this long (a wedged NVML/driver "
            "call): the tick is counted and the loop continues."
        ),
    )

    @model_validator(mode="after")
    def _intervals_ordered(self) -> ResourceSettings:
        if self.max_report_interval_s < self.sample_interval_s:
            raise ValueError(
                "resources.max_report_interval_s must be >= sample_interval_s"
            )
        return self


class ScoreSettings(SectionModel):
    """``[agent.scores]`` - hardware scores (roadmap 0.3.4, brief §5g D32b/D34).

    The agent measures CPU GFLOP/s, memory bandwidth and GPU TFLOP/s **once per
    hardware/software fingerprint**: in the background, ``start_delay_s`` after the
    first ONLINE of the process, and only when ``<data_dir>/scores.json`` is missing,
    corrupt or stale. It never re-scores periodically. ``meshforge-agent hardware
    score`` measures on demand; a running agent notices the new cache by polling its
    modification time every ``cache_poll_s``. Every measurement runs in a child
    process at lowered priority, killed at ``case_timeout_s`` per case and at
    ``budget_s`` for the whole run.
    """

    enabled: bool = Field(
        default=True,
        description="Score this node in the background when the cache is invalid.",
    )
    start_delay_s: float = Field(
        default=15.0,
        ge=0,
        le=3600.0,
        description=(
            "Wait this long after the first ONLINE before scoring, so start-up and "
            "enrolment never compete with the benchmark."
        ),
    )
    budget_s: float = Field(
        default=90.0,
        ge=1.0,
        le=1800.0,
        description=(
            "Whole-run budget; cases it does not reach are FAILED: budget_exhausted."
        ),
    )
    case_timeout_s: float = Field(
        default=30.0,
        gt=0,
        le=600.0,
        description=(
            "One case (child start + setup + warmup + 5 repetitions); on expiry the "
            "child tree is killed and the case is FAILED: timeout."
        ),
    )
    cache_poll_s: float = Field(
        default=10.0,
        ge=0.05,
        le=3600.0,
        description="How often a running agent checks scores.json for a newer run.",
    )

    @model_validator(mode="after")
    def _case_fits_budget(self) -> ScoreSettings:
        if self.case_timeout_s > self.budget_s:
            raise ValueError("scores.case_timeout_s must be <= scores.budget_s")
        return self


# ------------------------------------------------------------------ execution
PYTHON_CPU_IMAGE: Final = (
    "python:3.12-slim"
    "@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
)
"""Default ``python-cpu`` base (V0.4 D46/D51): the Docker Official Image, multi-arch
index digest resolved 2026-09-28 (image built 2026-09-19, Python 3.12.14). 3.12 is
the project's interpreter, so a job behaves the same as a local run; ``slim``
(Debian) keeps glibc wheels working (an Alpine/musl base would force source builds
of numpy & co.)."""

CUDA_PYTORCH_IMAGE: Final = (
    "pytorch/pytorch:2.14.0-cuda12.6-cudnn9-runtime"
    "@sha256:a77983eb7a3042ccf41a94da547a7f54a47fb70da15c36d67a8e8a329ebaa098"
)
"""Default ``cuda-pytorch`` base: PyTorch's official image (linux/amd64 only),
published 2026-09-02, Python 3.12. **CUDA 12.6** rather than 13.x on purpose: it runs
on NVIDIA drivers >= 560 (and with minor-version compatibility >= 525) and still
supports Maxwell/Pascal/Volta GPUs, which CUDA 13 dropped - the team's laptops have
unknown drivers and GPUs until ``lab/inventory.py`` has run on them. ``runtime`` (not
``devel``): ~4 GB instead of ~14 GB compressed."""

_IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,200}@sha256:[0-9a-f]{64}")
_RUN_AS = re.compile(r"auto|(\d{1,10}):(\d{1,10})")
GRACE_BOUND_S: Final = 3.0
"""Largest ``[agent.execution] grace_s``: V0.4 criterion 3 wants a cancelled workload
stopped within 5 s, and a runtime returns within ``grace_s + 2 s`` (brief D51)."""


class ExecutionImages(SectionModel):
    """``[agent.execution.images]`` - the image each manifest ``env.base`` maps to.

    Every value must be **digest-pinned** (``name[:tag]@sha256:<64 hex>``): a tag
    alone can move under the fabric and silently change what a job runs, which
    would break "same inputs, same outputs" (V0.4 criterion 1) and V0.5's env hash.
    """

    python_cpu: str = Field(
        default=PYTHON_CPU_IMAGE,
        description="Image for env.base = python-cpu (digest-pinned).",
    )
    cuda_pytorch: str = Field(
        default=CUDA_PYTORCH_IMAGE,
        description="Image for env.base = cuda-pytorch (digest-pinned).",
    )

    @field_validator("python_cpu", "cuda_pytorch")
    @classmethod
    def _digest_pinned(cls, value: str) -> str:
        if not _IMAGE_REF.fullmatch(value):
            raise ValueError(
                "image must be a lowercase reference pinned by digest: "
                "name[:tag]@sha256:<64 hex digits>"
            )
        return value


class ExecutionSettings(SectionModel):
    """``[agent.execution]`` - whether and how this laptop runs other people's jobs.

    Off by default (brief D45, ADR-0014): an agent executes nothing unless its owner
    sets ``enabled = true``. The Docker runtime is the sandbox (workdir-only mount,
    non-root, no capabilities, limits; brief D51). The **subprocess** runtime has no
    sandbox at all - the workload runs as the agent's user with the agent's file
    access - and is offered only with the second opt-in ``allow_unsandboxed``.

    Limits here are *defaults* for a job whose manifest does not set them; a
    manifest value never exceeds the ``max_*`` bounds. ``workdir_root`` empty =
    ``<data_dir>/work``. ``run_as``: ``auto`` runs containers as the agent's own
    uid:gid (so the workload can write its workdir and the agent can collect the
    outputs), or ``65534:65534`` (nobody) when the agent itself is root or on
    Windows; an explicit ``"uid:gid"`` must not be root (neither id 0).
    """

    enabled: bool = Field(
        default=False,
        description="Run jobs sent by the controller (off = refuse every StartAttempt).",
    )
    allow_unsandboxed: bool = Field(
        default=False,
        description=(
            "Also offer the subprocess runtime, which has NO sandbox (runs as the "
            "agent's user). Needed on a laptop without Docker."
        ),
    )
    images: ExecutionImages = Field(default_factory=ExecutionImages)
    grace_s: float = Field(
        default=GRACE_BOUND_S,
        gt=0,
        le=GRACE_BOUND_S,
        description=(
            "Cancel/timeout: SIGTERM, then SIGKILL after this long (<= 3 s, V0.4 "
            "criterion 3: stopped within 5 s)."
        ),
    )
    workdir_root: Path | None = Field(
        default=None,
        description="Where attempt workdirs are created; empty = <data_dir>/work.",
    )
    default_timeout_s: float = Field(
        default=3600.0, gt=0, le=604_800.0, description="Job time limit if none given."
    )
    max_timeout_s: float = Field(
        default=86_400.0,
        gt=0,
        le=604_800.0,
        description="Largest time limit a job may ask for (7 days at most).",
    )
    default_memory_mb: int = Field(
        default=4096, ge=64, le=1_048_576, description="Memory limit if none given."
    )
    default_cpus: float | None = Field(
        default=None,
        gt=0,
        le=1024,
        description="CPU limit (cores) if none given; empty = no CPU limit.",
    )
    default_pids: int = Field(
        default=1024, ge=16, le=65_536, description="Process/thread limit per job."
    )
    tmp_size_mb: int = Field(
        default=512,
        ge=16,
        le=65_536,
        description="Size of the container's /tmp (tmpfs).",
    )
    shm_size_mb: int = Field(
        default=512,
        ge=16,
        le=65_536,
        description="Size of /dev/shm (PyTorch DataLoader workers need more than 64 MB).",
    )
    env_timeout_s: float = Field(
        default=1800.0,
        gt=0,
        le=7200.0,
        description=(
            "Budget for the env phase (image pull + requirements install); on expiry "
            "the attempt fails with `timeout` (retryable), not `deadline_exceeded`."
        ),
    )
    docker_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Timeout of one Docker API call (create/inspect/remove).",
    )
    run_as: str = Field(
        default="auto",
        description='Container user: "auto" or "uid:gid" (numeric, neither 0).',
    )
    keep_workdirs: bool = Field(
        default=False,
        description=(
            "Keep each attempt's workdir (and its .meshforge/*.log files) after its "
            "final status was acknowledged; off = deleted (V0.4 D67)."
        ),
    )
    max_concurrent_attempts: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "Attempts this agent holds at once; a StartAttempt beyond it fails "
            "resource_busy (the controller sends one at a time in V0.4)."
        ),
    )
    terminal_resend_s: float = Field(
        default=5.0,
        ge=0.2,
        le=300.0,
        description=(
            "An ended attempt's final status is re-sent this often (and after every "
            "reconnect) until the controller acknowledges it (V0.4 D75)."
        ),
    )

    @field_validator("run_as")
    @classmethod
    def _run_as_shape(cls, value: str) -> str:
        match = _RUN_AS.fullmatch(value.strip())
        if match is None:
            raise ValueError('run_as must be "auto" or "uid:gid" (numbers)')
        if match.group(1) is not None and 0 in (int(match[1]), int(match[2])):
            raise ValueError("run_as must not be uid 0 or gid 0 (root)")
        return value.strip()

    @model_validator(mode="after")
    def _timeouts_ordered(self) -> ExecutionSettings:
        if self.default_timeout_s > self.max_timeout_s:
            raise ValueError("execution.default_timeout_s must be <= max_timeout_s")
        return self


class DiscoverySettings(SectionModel):
    """``[agent.discovery]`` - mDNS lookup of the controller (roadmap 0.3.9).

    Used only when ``agent.controller_address`` is empty. One browse lasts
    ``timeout_s``; a cycle that finds nothing ends in BACKOFF and the next cycle
    browses again (never a busy loop).
    """

    enabled: bool = Field(
        default=True,
        description="Browse mDNS when controller_address is empty. Off = fail fast.",
    )
    timeout_s: float = Field(
        default=5.0, gt=0, description="How long one browse waits for a record."
    )
    service_type: str = Field(
        default=DEFAULT_SERVICE_TYPE,
        description="DNS-SD service type; must match the controller's.",
    )

    @field_validator("service_type")
    @classmethod
    def _service_type_shape(cls, value: str) -> str:
        value = value.strip()
        if not value.endswith("._tcp.local.") or not value.startswith("_"):
            raise ValueError("service_type must look like _name._tcp.local.")
        return value


class AgentSection(SectionModel):
    """``[agent]`` section."""

    controller_address: str = Field(
        default="",
        description=(
            "host:port of the controller's gRPC listener (bracket IPv6). Empty = "
            "find it via mDNS (agent.discovery)."
        ),
    )
    reconnect: ReconnectSettings = Field(default_factory=ReconnectSettings)
    keepalive: KeepaliveSettings = Field(default_factory=KeepaliveSettings)
    replay_ring_size: int = Field(
        default=10_000,
        ge=1,
        description=(
            "Un-acked reports kept for replay after a reconnect; overflow drops the "
            "oldest and counts it (ADR-0002 Updates §4)."
        ),
    )
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    resources: ResourceSettings = Field(default_factory=ResourceSettings)
    scores: ScoreSettings = Field(default_factory=ScoreSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    hello_timeout_s: float = Field(
        default=3.0,
        gt=0,
        description=(
            "How long to wait for HelloAck before the attempt counts as failed. "
            "cap_s + hello_timeout_s must stay <= 8 s (V0.3 criterion 2, D29)."
        ),
    )
    agent_version: str = Field(
        default=__version__,
        description="Reported in Hello.agent_version; defaults to the package version.",
    )
    identity_lock_timeout_s: float = Field(
        default=10.0,
        gt=0,
        description=(
            "How long to wait for identity.lock held by another process before "
            "giving up with an IdentityError (ADR-0013 §6)."
        ),
    )

    @field_validator("controller_address")
    @classmethod
    def _address_shape(cls, value: str) -> str:
        value = value.strip()
        if value:
            parse_controller_address(value)
        return value

    @field_validator("agent_version")
    @classmethod
    def _version_length(cls, value: str) -> str:
        if not value or len(value) > _AGENT_VERSION_MAX_LENGTH:
            raise ValueError(
                f"agent_version must be 1..{_AGENT_VERSION_MAX_LENGTH} characters"
            )
        return value

    @model_validator(mode="after")
    def _criterion_2_budget(self) -> AgentSection:
        # V0.3 criterion 2 (restored link -> ONLINE <= 10 s): see RECONNECT_ATTEMPT_BUDGET_S.
        total = self.reconnect.cap_s + self.hello_timeout_s
        if total > RECONNECT_ATTEMPT_BUDGET_S:
            raise ValueError(
                f"reconnect.cap_s + hello_timeout_s is {total:g} s; the maximum is "
                f"{RECONNECT_ATTEMPT_BUDGET_S:g} s (V0.3 criterion 2: a restored link "
                "must be ONLINE within 10 s)"
            )
        return self

    @model_validator(mode="after")
    def _some_way_to_find_the_controller(self) -> AgentSection:
        if not self.controller_address and not self.discovery.enabled:
            raise ValueError(
                "controller_address is empty and discovery.enabled is false: "
                "the agent has no way to find a controller"
            )
        return self

    @property
    def uses_discovery(self) -> bool:
        return not self.controller_address


class AgentSettings(MeshForgeSettings):
    """Everything the agent process needs (``[agent]`` + the common keys)."""

    agent: AgentSection = Field(default_factory=AgentSection)

    @property
    def identity_dir(self) -> Path:
        """``data_dir/identity`` - where :mod:`meshforge.agent.identity` keeps the node."""
        return self.data_dir / IDENTITY_DIR_NAME
