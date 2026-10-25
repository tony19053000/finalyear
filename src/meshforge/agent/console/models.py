"""Request and response bodies of the console API (``/console/v1``), the
contract ``frontend/src/agentConsole`` is generated from
(``scripts/export_openapi.py --console``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CheckView",
    "ChecksView",
    "ConsoleError",
    "ControllerView",
    "DiscoverView",
    "DockerAccessView",
    "ExchangeRequest",
    "HealthView",
    "JoinRequest",
    "JoinView",
    "LaunchCodeView",
    "LeaveRequest",
    "LogLineView",
    "LogsView",
    "NodeNameRequest",
    "NodeNameView",
    "OkView",
    "PolicyRequest",
    "PolicyValues",
    "PolicyView",
    "StatusView",
    "StopReasonView",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsoleError(_Model):
    code: str
    message: str
    fix: str = ""


class OkView(_Model):
    ok: bool = True
    message: str = ""


class HealthView(_Model):
    ok: bool
    version: str


class ExchangeRequest(_Model):
    """The page trades the launcher's one-time code (``#c=``) for the session
    cookie, once. (The token itself is never accepted from a browser: it only
    buys launch codes, ``POST /launch-code``.)"""

    code: str = Field(min_length=1, max_length=256)


class LaunchCodeView(_Model):
    code: str
    url: str
    expires_in_s: float


class StopReasonView(_Model):
    code: str
    message: str
    failure_type: str | None = None


class StatusView(_Model):
    version: str
    install_mode: Literal["user", "system"]
    phase: Literal["waiting_for_join", "starting", "running", "stopping", "stopped"]
    phase_since: float
    """Unix time the phase began."""
    stop_reason: StopReasonView | None = None
    joined: bool
    setup_complete: bool
    """The wizard's last step was done (it is shown until then)."""
    node_id: str | None = None
    node_name: str
    hostname: str
    fabric_id: str | None = None
    cert_not_after: str | None = None
    identity_problem: str | None = None
    connection: str | None = None
    """The session state (``ONLINE``, ``BACKOFF``, ...) while running."""
    controller: str | None = None
    session_id: str | None = None
    dashboard_url: str | None = None
    """The controller's dashboard (``https://<controller host>:47110/``), best
    guess from the address the agent connected to."""


class ControllerView(_Model):
    name: str
    host: str
    port: int


class DiscoverView(_Model):
    controllers: list[ControllerView]
    problem: str | None = None


class JoinRequest(_Model):
    code: str = Field(min_length=1, max_length=512)
    controller_host: str | None = Field(
        default=None,
        max_length=255,
        description="A controller found on this network (its address replaces the "
        "code's; the code's CA pin still decides).",
    )


class JoinView(_Model):
    fabric_id: str
    node_id: str
    controller: str
    cert_not_after: str


class PolicyValues(_Model):
    """The owner's live settings (``meshforge-agent policy`` keys). In a PUT,
    absent keys stay as they are; ``null`` on a limit removes the limit."""

    accept_jobs: bool | None = None
    accept_gpu_sessions: bool | None = None
    max_cpu_jobs: int | None = Field(default=None, ge=0, le=64)
    max_gpu_jobs: int | None = Field(default=None, ge=0, le=64)
    max_ram_bytes: int | None = Field(default=None, ge=0)
    max_cpu_percent: int | None = Field(default=None, ge=1, le=100)
    max_vram_bytes: int | None = Field(default=None, ge=0)
    allow_unsandboxed: bool | None = None
    allow_network: bool | None = None
    allowed_users: list[str] | None = Field(default=None, max_length=64)
    pause_benchmarks: bool | None = None


class PolicyRequest(_Model):
    values: PolicyValues


class PolicyView(_Model):
    policy: dict[str, object]
    max_cpu_jobs: int
    max_gpu_jobs: int | None
    live_keys: list[str]
    invalid: str | None
    deprecated_keys: list[str]
    restart_needed: list[str] = []
    """Keys just turned on that an agent started with them off only uses after
    a restart (the page offers it)."""
    owner_activity_guard: bool
    """``[agent.bench] activity`` (configuration): benchmark jobs yield while
    the owner uses the laptop."""
    cpu_threads: int | None


class NodeNameRequest(_Model):
    name: str = Field(max_length=256)


class NodeNameView(_Model):
    name: str
    source: Literal["console", "config", "none"]
    hostname: str
    applies_after_restart: bool


class CheckView(_Model):
    id: str
    title: str
    status: Literal["ok", "warn", "missing"]
    why: str
    fix: str
    action: str | None


class ChecksView(_Model):
    checks: list[CheckView]


class LogLineView(_Model):
    seq: int
    timestamp: str
    level: str
    event: str
    logger: str
    fields: dict[str, object]


class LogsView(_Model):
    lines: list[LogLineView]
    capacity: int


class LeaveRequest(_Model):
    confirm: Literal["leave"]


class DockerAccessView(_Model):
    """How to give the service Docker access (it cannot do it itself: Q24 and
    the progress note's decision 4)."""

    supported: bool
    menu_action: str
    command: str
    explanation: str
