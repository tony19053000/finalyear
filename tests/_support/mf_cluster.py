"""Cluster harness: a real controller + N real agents as local PROCESSES (roadmap 0.3.14).

Imported as the top-level module ``mf_cluster`` (``tests/conftest.py`` puts
``tests/_support`` on ``sys.path``); used by ``tests/cluster``.

What it gives a scenario
------------------------
:class:`Fabric` starts ``python -m meshforge.controller`` against a fresh database
(from the shared ``pg_settings`` fixture) with gRPC and HTTP on ``127.0.0.1:0``,
learns the **bound** ports from the controller's ``controller.listening`` log event
(D24 - no free-port guessing, no TOCTOU), then starts N ``python -m meshforge.agent
run`` processes, each with its own data dir (its own identity) and its own
:class:`~mf_faultproxy.FaultLink` in front of the controller. Every node's
``node_id`` is read from its ``agent.started`` log event.

Faults: :meth:`Node.kill` (SIGKILL / TerminateProcess), :meth:`Node.stop_graceful`
(SIGINT / CTRL_BREAK), :meth:`Node.pause` / :meth:`Node.resume` (psutil
suspend/resume, Windows too), :meth:`Node.restart` (same data dir -> same node_id,
higher epoch); on the controller :meth:`Fabric.kill_controller`,
:meth:`Fabric.stop_controller`, :meth:`Fabric.restart_controller` (new ports, every
link retargeted); and every link fault through ``node.link``.

Observation is only through the product surface (D24): REST ``/v1/nodes`` (and
``/v1/sessions``, ``/v1/nodes/{id}/resources``) and each process's JSON log file.
The harness imports nothing from ``meshforge.controller`` or ``meshforge.agent``.
Every wait fails with the tail of every process's log.

Containment (why a test can never leak a process)
-------------------------------------------------
1. Each child is started in its **own process group** (POSIX
   ``start_new_session``; Windows ``CREATE_NEW_PROCESS_GROUP``). Teardown snapshots
   the child's descendants (psutil), kills the group and each descendant, reaps,
   and **asserts none survived**.
2. An ``atexit`` hook kills whatever is still registered (pytest interrupted,
   ``KeyboardInterrupt`` escaping teardown).
3. Linux only: each child is exec'd through a tiny launcher that sets
   ``PR_SET_PDEATHSIG = SIGKILL``, so the kernel kills it if the test process dies
   without running any Python (SIGKILL, pytest-timeout's ``os._exit``). The signal
   is tied to the *thread* that spawned the child, so spawn only from the test's
   main thread (the harness never spawns from another). Windows would need a Job
   Object for the same guarantee - not built yet (G6-B follow-up).

Time: waits use ``time.monotonic``; log timestamps (wall clock, from the processes)
are only used to order events and to compute durations between two log events.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import os
import secrets
import signal
import ssl
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Final, NoReturn

import httpx
import pytest

# The agent extra provides psutil; skip (not crash) a plain `uv sync` collection.
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

import psutil  # type: ignore[import-untyped]  # no stubs; agent extra
from mf_faultproxy import FaultLink, FaultLoop

__all__ = [
    "ADMIN_USER",
    "FAST",
    "FORBIDDEN_EVENTS",
    "NORMAL_USER",
    "SHIPPED",
    "Fabric",
    "HarnessError",
    "LogFile",
    "MakeFabric",
    "ManagedProcess",
    "Node",
    "Timing",
    "Transition",
    "tmpfs_unsupported_reason",
]

_IS_WINDOWS: Final = sys.platform == "win32"
_IS_LINUX: Final = sys.platform.startswith("linux")

ADMIN_USER: Final = "admin"
NORMAL_USER: Final = "alice"
"""V0.7 G4: every fabric has these two REST users (see ``Fabric._create_users``)."""

START_TIMEOUT_S: Final = 60.0
"""Controller start (migrations on a fresh DB) / agent start (hardware discovery)."""
POLL_S: Final = 0.05
"""REST poll interval: measured timings are accurate to about this much."""
REAP_TIMEOUT_S: Final = 10.0
TAIL_LINES: Final = 12

FORBIDDEN_EVENTS: Final = frozenset(
    {
        "liveness.invalid_transition",
        "session.task_failed",
        "session.reader_crashed",
        "agent.session.task_failed",
    }
)
"""Log events that mean a bug, whatever the scenario did (checked after each one)."""

_PARENT_ENV: Final = "MESHFORGE_CLUSTER_PARENT_PID"
_LAUNCHER: Final = """\
import ctypes, os, signal, sys
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
    sys.exit("pdeathsig: prctl failed")
if str(os.getppid()) != os.environ.get("PARENT_ENV"):
    sys.exit("pdeathsig: parent already gone")
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""".replace("PARENT_ENV", _PARENT_ENV)

_TMPFS_BOOT: Final = """\
import ctypes, os, signal, sys
target, size = sys.argv[1], sys.argv[2]
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)
for name, text in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
    with open(f"/proc/self/{name}", "w") as fh:
        fh.write(text)
libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG again, after unshare
if libc.mount(b"none", b"/", None, 16384 | (1 << 18), None) != 0:  # MS_REC|MS_PRIVATE
    sys.exit(f"tmpfs boot: making / private failed ({ctypes.get_errno()})")
os.makedirs(target, exist_ok=True)
if libc.mount(b"meshforge-chaos", target.encode(), b"tmpfs", 0, f"size={size}".encode()) != 0:
    sys.exit(f"tmpfs boot: mount failed ({ctypes.get_errno()})")
os.execv(sys.executable, [sys.executable, *sys.argv[3:]])
"""
"""Linux: run the agent in its own user + mount namespace whose workdir is a
tiny tmpfs (no root needed) - a real ``ENOSPC`` for the chaos suite's
disk-full fault (V0.6 G3, D118). The test fills it from outside through
``/proc/<pid>/root``."""


def tmpfs_unsupported_reason() -> str | None:
    """None if an agent can run on a namespaced tmpfs workdir here."""
    if not _IS_LINUX or not hasattr(os, "unshare"):
        return "needs Linux and Python 3.12+ (os.unshare)"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNS)",
        ],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return "unprivileged user namespaces are not available here"
    return None


class HarnessError(AssertionError):
    """A cluster wait or invariant failed; the message carries every log tail."""


# --------------------------------------------------------------------- timing


@dataclasses.dataclass(frozen=True)
class Timing:
    """Liveness/reconnect settings written into the processes' config.

    ``None`` = do not write the key, i.e. the **shipped default** applies. The
    derived edges mirror ARCHITECTURE §12.3 (SUSPECT after interval + allowance,
    OFFLINE after interval x multiplier, both from the last heartbeat).
    """

    name: str
    heartbeat_interval_s: float | None = None
    miss_allowance_s: float | None = None
    lease_multiplier: int | None = None
    controller_hello_timeout_s: float | None = None
    shutdown_drain_s: float | None = None
    agent_reconnect_base_s: float | None = None
    agent_reconnect_cap_s: float | None = None
    agent_hello_timeout_s: float | None = None

    @property
    def heartbeat_s(self) -> float:
        return 2.0 if self.heartbeat_interval_s is None else self.heartbeat_interval_s

    @property
    def suspect_after_s(self) -> float:
        allowance = 0.5 if self.miss_allowance_s is None else self.miss_allowance_s
        return self.heartbeat_s + allowance

    @property
    def offline_after_s(self) -> float:
        mult = 3 if self.lease_multiplier is None else self.lease_multiplier
        return self.heartbeat_s * mult


FAST: Final = Timing(
    name="fast",
    # FAST_LIVENESS (mf_controller_harness): SUSPECT at 0.25 s, OFFLINE at 0.6 s.
    heartbeat_interval_s=0.2,
    miss_allowance_s=0.05,
    lease_multiplier=3,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
"""D25 default cluster profile (like ``FAST_LIVENESS``)."""

SHIPPED: Final = Timing(name="shipped")
"""D25 slow profile: nothing overridden - the product's shipped defaults."""


# ----------------------------------------------------------------------- logs


@dataclasses.dataclass(frozen=True)
class Transition:
    """One ``node.state_changed`` event from the controller log."""

    node_id: str
    from_state: str
    to_state: str
    liveness_event: str | None
    session_id: str | None
    silence_s: float | None
    lingering: bool | None
    at: dt.datetime
    generation: int
    """Which controller process (0 = first start) logged it."""


class LogFile:
    """One process generation's JSON log, parsed incrementally (complete lines only)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._records: list[dict[str, Any]] = []
        self._raw: list[str] = []

    def _refresh(self) -> None:
        try:
            with self.path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._offset += end + 1
        for raw in chunk[: end + 1].decode("utf-8", "replace").splitlines():
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                self._raw.append(raw)
                continue
            if isinstance(record, dict):
                self._records.append(record)
            else:
                self._raw.append(raw)

    def records(self) -> list[dict[str, Any]]:
        self._refresh()
        return list(self._records)

    def raw_lines(self) -> list[str]:
        """Lines that are not JSON records (tracebacks, typer errors, ...)."""
        self._refresh()
        return list(self._raw)

    def events(self, name: str, **match: Any) -> list[dict[str, Any]]:
        return [
            r
            for r in self.records()
            if r.get("event") == name and all(r.get(k) == v for k, v in match.items())
        ]

    def tail(self, n: int = TAIL_LINES) -> list[str]:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ["<no log file>"]
        return [line[:400] for line in text.splitlines()[-n:]]


def _timestamp(record: Mapping[str, Any]) -> dt.datetime:
    return dt.datetime.fromisoformat(str(record["timestamp"]))


# ------------------------------------------------------------------ processes


_REGISTRY: set[ManagedProcess] = set()


def _kill_registered() -> None:
    for proc in list(_REGISTRY):
        with contextlib.suppress(Exception):
            proc.terminate_tree()


atexit.register(_kill_registered)


class ManagedProcess:
    """One child process (plus its later restarts), contained in its own group.

    Each start is a new *generation* with its own log file; ``pid`` is the
    current generation's. :meth:`terminate_tree` is the only way it goes away.
    """

    def __init__(
        self, name: str, argv: list[str], env: Mapping[str, str], log_dir: Path
    ) -> None:
        self.name = name
        self.argv = argv
        self.env = dict(env)
        self.log_dir = log_dir
        self.logs: list[LogFile] = []
        self.pids: list[int] = []
        self._popen: subprocess.Popen[bytes] | None = None
        self._seen: dict[int, psutil.Process] = {}
        self.expected_alive = False

    # --- lifecycle

    def start(self) -> None:
        if self.running:
            raise RuntimeError(f"{self.name} is already running")
        log = LogFile(self.log_dir / f"{self.name}.{len(self.logs)}.log")
        self.logs.append(log)
        env = dict(self.env)
        argv = [sys.executable, *self.argv]
        kwargs: dict[str, Any] = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined,unused-ignore]
        else:
            kwargs["start_new_session"] = True
        if _IS_LINUX:
            env[_PARENT_ENV] = str(os.getpid())
            argv = [sys.executable, "-c", _LAUNCHER, *self.argv]
        with log.path.open("wb") as out:
            self._popen = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                **kwargs,
            )
        self.pids.append(self._popen.pid)
        self.expected_alive = True
        _REGISTRY.add(self)

    @property
    def pid(self) -> int:
        if not self.pids:
            raise RuntimeError(f"{self.name} was never started")
        return self.pids[-1]

    @property
    def log(self) -> LogFile:
        return self.logs[-1]

    @property
    def generation(self) -> int:
        return len(self.logs) - 1

    @property
    def running(self) -> bool:
        return self._popen is not None and self._popen.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self._popen is None else self._popen.poll()

    def _remember_descendants(self) -> None:
        if not self.running:
            return
        with contextlib.suppress(psutil.Error):
            root = psutil.Process(self.pid)
            for proc in [root, *root.children(recursive=True)]:
                self._seen[proc.pid] = proc

    def kill(self) -> None:
        """SIGKILL the whole group (Windows: TerminateProcess)."""
        self.expected_alive = False
        self._remember_descendants()
        if self._popen is None or self._popen.poll() is not None:
            return
        if _IS_WINDOWS:
            self._popen.kill()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._popen.pid, signal.SIGKILL)
        self._popen.wait(REAP_TIMEOUT_S)

    def interrupt(self) -> None:
        """Ask for a graceful stop: SIGINT (Windows: CTRL_BREAK to its group)."""
        self.expected_alive = False
        self._remember_descendants()
        if self._popen is None or self._popen.poll() is not None:
            return
        if _IS_WINDOWS:
            self._popen.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined,unused-ignore]
        else:
            self._popen.send_signal(signal.SIGINT)

    def wait_exit(self, timeout_s: float) -> int:
        assert self._popen is not None
        return self._popen.wait(timeout_s)

    def suspend(self) -> None:
        psutil.Process(self.pid).suspend()

    def resume(self) -> None:
        psutil.Process(self.pid).resume()

    def is_suspended(self) -> bool:
        try:
            return bool(psutil.Process(self.pid).status() == psutil.STATUS_STOPPED)
        except psutil.Error:
            return False

    def terminate_tree(self) -> list[int]:
        """Kill this process's group and every remembered descendant; reap.

        Returns the pids that are *still alive* afterwards (should be empty).
        """
        self._remember_descendants()
        if self.running:
            with contextlib.suppress(psutil.Error):
                if self.is_suspended():
                    self.resume()  # a stopped process still dies to SIGKILL; tidy
            self.kill()
        elif self._popen is not None:
            self._popen.wait(REAP_TIMEOUT_S)
        self.expected_alive = False
        procs = list(self._seen.values())
        for proc in procs:
            with contextlib.suppress(psutil.Error):
                proc.kill()
        _gone, alive = psutil.wait_procs(procs, timeout=REAP_TIMEOUT_S)
        _REGISTRY.discard(self)
        return [p.pid for p in alive]

    def all_pids_seen(self) -> list[int]:
        self._remember_descendants()
        return sorted(self._seen)


# ----------------------------------------------------------------------- toml


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    raise TypeError(f"unsupported TOML value: {value!r}")


def _toml(tables: Mapping[str, Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for table, values in tables.items():
        if table:
            lines.append(f"[{table}]")
        lines.extend(f"{key} = {_toml_value(v)}" for key, v in values.items())
        lines.append("")
    return "\n".join(lines)


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def _host_port(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host.strip("[]"), int(port)


# ----------------------------------------------------------------------- nodes


@dataclasses.dataclass
class Node:
    """One agent process, its data dir, its link, and what we learned about it."""

    index: int
    process: ManagedProcess
    link: FaultLink
    data_dir: Path
    node_id: str = ""
    restarts: int = 0
    fabric: Fabric | None = None
    tmpfs_workdir: Path | None = None
    """V0.6 G3: the agent's workdir is a tmpfs in its own mount namespace."""
    peer_link: FaultLink | None = None
    """V0.7 G3 (D146, ``record_links``): the link in front of its peer server."""

    @property
    def config(self) -> Path:
        """The agent's TOML config (V0.7 G2: `meshforge-agent join --config`)."""
        assert self.fabric is not None
        return self.fabric.workdir / f"agent{self.index}.toml"

    def _in_namespace(self, path: Path) -> Path:
        return Path(f"/proc/{self.pid}/root") / path.relative_to(path.anchor)

    def fill_disk(self) -> int:
        """Fill the node's tmpfs workdir to the last byte (a real ``ENOSPC``
        for whatever it writes next); returns the bytes written."""
        if self.tmpfs_workdir is None:
            raise RuntimeError(f"{self.name} has no tmpfs workdir")
        filler = self._in_namespace(self.tmpfs_workdir / "chaos-filler")
        written = 0
        chunk = b"\0" * (64 * 1024)
        with contextlib.suppress(OSError), filler.open("ab") as fh:
            while True:
                fh.write(chunk)
                fh.flush()
                written += len(chunk)
        return written

    def free_disk(self) -> None:
        if self.tmpfs_workdir is not None:
            with contextlib.suppress(FileNotFoundError):
                self._in_namespace(self.tmpfs_workdir / "chaos-filler").unlink()

    @property
    def name(self) -> str:
        return self.process.name

    @property
    def pid(self) -> int:
        return self.process.pid

    def kill(self) -> float:
        """SIGKILL; returns the monotonic time of the fault."""
        at = time.monotonic()
        self.process.kill()
        return at

    def stop_graceful(self, timeout_s: float = 15.0) -> tuple[float, int]:
        """SIGINT and wait for exit; returns (fault time, exit code)."""
        at = time.monotonic()
        self.process.interrupt()
        try:
            code = self.process.wait_exit(timeout_s)
        except subprocess.TimeoutExpired:
            assert self.fabric is not None
            self.fabric.fail(f"{self.name} did not exit {timeout_s} s after SIGINT")
        return at, code

    def pause(self) -> float:
        at = time.monotonic()
        self.process.suspend()
        return at

    def resume(self) -> float:
        at = time.monotonic()
        self.process.resume()
        return at

    def restart(self, *, graceful: bool = True) -> float:
        """Stop (graceful or kill) and start again with the same data dir."""
        if self.process.running:
            if graceful:
                self.stop_graceful()
            else:
                self.kill()
        at = time.monotonic()
        self.process.start()
        self.restarts += 1
        return at


# ---------------------------------------------------------------------- fabric


class Fabric:
    """A controller + N agents as local processes. Use as a context manager.

    ``database`` is a ``DatabaseSettings`` (typed loosely so this module needs
    nothing from ``meshforge.controller``); only host/port/name/user/password are
    read from it.
    """

    def __init__(
        self,
        database: Any,
        workdir: Path,
        *,
        agents: int = 5,
        timing: Timing = FAST,
        controller_resources: Mapping[str, Any] | None = None,
        agent_resources: Mapping[str, Any] | None = None,
        agent_keepalive: Mapping[str, Any] | None = None,
        agent_scores: Mapping[str, Any] | None = None,
        agent_env: Mapping[str, str] | None = None,
        agent_execution: Mapping[str, Any] | None = None,
        controller_jobs: Mapping[str, Any] | None = None,
        agent_extra: Mapping[str, Mapping[str, Any]] | None = None,
        agent_tmpfs_workdir: Mapping[int, int] | None = None,
        controller_extra: Mapping[str, Mapping[str, Any]] | None = None,
        log_level: str = "INFO",
        fabric_ca: bool = False,
        enroll_agents: bool = True,
        start_agents: bool = True,
        record_links: bool = False,
        security_mode: str = "mtls",
    ) -> None:
        self.workdir = workdir
        # V0.7 G7 (D146 control run): ``"insecure-dev"`` runs every process in
        # plaintext on loopback (job secrets allowed on it), so the recorder can
        # be shown to SEE what the mtls run must hide. Never anything else.
        if security_mode not in ("mtls", "insecure-dev"):
            raise ValueError(f"unknown security mode {security_mode!r}")
        self.security_mode = security_mode
        self.scheme = "https" if security_mode == "mtls" else "http"
        # V0.7 G3: a controller in mtls mode needs a fabric CA. ``fabric_ca``:
        # make it with the real `meshforge-controller fabric create` (its own
        # CA); otherwise the test process's fixture CA (``mf_fabric``) is
        # installed, as `fabric create` would.
        self.fabric_ca = fabric_ca
        # ``enroll_agents``: give every agent a node certificate from the
        # controller's CA before it starts (no join dance); a join scenario
        # turns it off and runs `meshforge-agent join` itself, then calls
        # :meth:`start_agents` (``start_agents=False``).
        self.enroll_agents = enroll_agents
        # V0.7 G3 (D146): every network path through a recording FaultLink -
        # agent->controller gRPC (always proxied), REST/blobs (the controller
        # advertises a "rest" link; the harness's own REST calls use it too)
        # and each agent's peer server (advertise_port = its "peer" link).
        self.record_links = record_links
        self.rest_link: FaultLink | None = None
        self._peer_links: dict[int, FaultLink] = {}
        self._start_agents = start_agents
        self.enroll_target = ""
        self.timing = timing
        self.n_agents = agents
        self.log_level = log_level
        self._controller_resources = dict(controller_resources or {})
        self._agent_resources = dict(agent_resources or {})
        self._agent_keepalive = dict(agent_keepalive or {})
        # G7-B: ``[agent.scores]`` overrides (scoring stays off unless a scenario
        # passes ``{"enabled": True, ...}``) and extra environment for the agent
        # processes - e.g. the benchmark child's test-only hooks
        # (``MESHFORGE_BENCH_DISABLE``), which the child inherits from its agent.
        self._agent_scores: dict[str, Any] = {"enabled": False, **(agent_scores or {})}
        self._agent_env = dict(agent_env or {})
        # V0.4 G4: ``[agent.execution]`` (off unless a scenario enables it) and
        # ``[controller.jobs]`` overrides for the dispatch scenarios.
        self._agent_execution = dict(agent_execution or {})
        self._controller_jobs = dict(controller_jobs or {})
        # V0.5 G4: more ``[agent.<section>]`` tables, e.g. ``{"peer": {...},
        # "fetch": {...}, "cache": {...}}`` for the peer-fetch scenarios.
        self._agent_extra = {k: dict(v) for k, v in (agent_extra or {}).items()}
        # V0.6 G3: agent index -> tmpfs size in bytes (Linux; see _TMPFS_BOOT).
        self._agent_tmpfs = dict(agent_tmpfs_workdir or {})
        # V0.6 G4: more ``[controller.<section>]`` tables, e.g.
        # ``{"planner": {"retry_exclude_s": 3}, "network": {...}}``.
        self._controller_extra = {
            k: dict(v) for k, v in (controller_extra or {}).items()
        }
        self._db_env = {
            "MESHFORGE_DATABASE__HOST": str(database.host),
            "MESHFORGE_DATABASE__PORT": str(database.port),
            "MESHFORGE_DATABASE__NAME": str(database.name),
            "MESHFORGE_DATABASE__USER": str(database.user),
            "MESHFORGE_DATABASE__PASSWORD": database.password.get_secret_value(),
        }
        self._users: dict[str, tuple[str, str]] = {}
        """V0.7 G4: user name -> (password, API token)."""
        self.loop: FaultLoop | None = None
        self.controller: ManagedProcess | None = None
        self.nodes: list[Node] = []
        self.grpc_target = ""
        self.http_base = ""
        self._http = httpx.Client(timeout=2.0)  # replaced once the CA exists
        self.ssl_context: ssl.SSLContext | None = None
        # One INFO line per poll would drown the captured output of a failure.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    # ------------------------------------------------------------- env/config

    def _base_env(self) -> dict[str, str]:
        # A developer's MESHFORGE_* variables must not leak into the processes.
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        return env

    def _controller_config(self) -> Path:
        t = self.timing
        tables: dict[str, dict[str, Any]] = {
            "": {"data_dir": str(self.workdir / "controller-data")},
            "log": {"format": "json", "level": self.log_level},
            "controller": _drop_none(
                {
                    "grpc_bind": "127.0.0.1:0",
                    "http_bind": "127.0.0.1:0",
                    "hello_timeout_s": t.controller_hello_timeout_s,
                    "shutdown_drain_s": t.shutdown_drain_s,
                }
            ),
            "controller.liveness": _drop_none(
                {
                    "heartbeat_interval_s": t.heartbeat_interval_s,
                    "miss_allowance_s": t.miss_allowance_s,
                    "lease_multiplier": t.lease_multiplier,
                }
            ),
            "security": self._security_table(),
            "controller.discovery": {"advertise": False},
            "controller.resources": self._controller_resources,
            "controller.jobs": self._controller_jobs,
            # V0.6 G4: probes off unless a scenario passes controller_extra
            # {"network": {...}} (they would add traffic other scenarios count).
            "controller.network": {"enabled": False},
            # V0.7 G2: the enrolment listener (only when a CA exists), free port.
            "controller.enroll": {"bind": "127.0.0.1:0"},
            **{f"controller.{k}": v for k, v in self._controller_extra.items()},
        }
        path = self.workdir / "controller.toml"
        path.write_text(_toml({k: v for k, v in tables.items() if v}), "utf-8")
        return path

    def _security_table(self) -> dict[str, Any]:
        if self.security_mode == "mtls":
            return {}
        return {"mode": self.security_mode, "allow_insecure_secrets": True}

    def _tmpfs_dir(self, index: int) -> Path:
        return self.workdir / f"agent{index}-tmpfs-work"

    def _agent_config(self, index: int, data_dir: Path) -> Path:
        t = self.timing
        tables: dict[str, dict[str, Any]] = {
            "": {"data_dir": str(data_dir)},
            "log": {"format": "json", "level": self.log_level},
            "agent": _drop_none({"hello_timeout_s": t.agent_hello_timeout_s}),
            "agent.reconnect": _drop_none(
                {"base_s": t.agent_reconnect_base_s, "cap_s": t.agent_reconnect_cap_s}
            ),
            "agent.keepalive": self._agent_keepalive,
            "security": self._security_table(),
            "agent.discovery": {"enabled": False},
            "agent.resources": self._agent_resources,
            # G7 (0.3.4): N agents share one host here, so each one's background
            # hardware-score run would put all-core benchmark load on the others'
            # liveness. Off by default; a scenario that tests scoring turns it on.
            "agent.scores": self._agent_scores,
            "agent.execution": (
                {**self._agent_execution, "workdir_root": str(self._tmpfs_dir(index))}
                if index in self._agent_tmpfs
                else self._agent_execution
            ),
            **{f"agent.{name}": table for name, table in self._agent_extra.items()},
        }
        peer_link = self._peer_links.get(index)
        if peer_link is not None:
            tables["agent.peer"] = {
                **tables.get("agent.peer", {}),
                "advertise_port": peer_link.address[1],
            }
        path = self.workdir / f"agent{index}.toml"
        path.write_text(_toml({k: v for k, v in tables.items() if v}), "utf-8")
        return path

    # ------------------------------------------------------------------ start

    def __enter__(self) -> Fabric:
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "logs").mkdir(exist_ok=True)
        self.loop = FaultLoop(f"faultproxy-{self.workdir.name}")
        if self.record_links:
            # Upstream fixed once the controller has bound its REST port.
            self.rest_link = self.loop.link(("127.0.0.1", 9), name="rest")
            self.rest_link.record()
            self._controller_extra.setdefault("api", {})["advertise_url"] = (
                f"{self.scheme}://{self.rest_link.target}"
            )
        env = self._base_env()
        env.update(self._db_env)
        controller_config = self._controller_config()
        if self.fabric_ca:
            created = subprocess.run(
                [sys.executable, "-m", "meshforge.controller", "fabric", "create",
                 "--config", str(controller_config)],
                env=env, capture_output=True, text=True, timeout=60, check=False,
            )  # fmt: skip
            if created.returncode != 0:
                self.fail(f"fabric create failed: {created.stderr[-2000:]}")
        else:
            seed_ca_dir(self.ca_dir)
        # V0.7 G3: the REST port is HTTPS; trust exactly this controller's CA
        # (standard host-name check: its certificate names 127.0.0.1).
        self.ssl_context = ssl.create_default_context(
            cafile=str(self.ca_dir / "ca.crt")
        )
        self._http.close()
        self._http = httpx.Client(timeout=2.0, verify=self.ssl_context)
        self.controller = ManagedProcess(
            "controller",
            ["-m", "meshforge.controller", "--config", str(controller_config)],
            env,
            self.workdir / "logs",
        )
        self._start_controller()
        self._create_users(controller_config, env)
        for i in range(self.n_agents):
            data_dir = self.workdir / f"agent{i}-data"
            link = self.loop.link(_host_port(self.grpc_target), name=f"agent{i}")
            peer_link: FaultLink | None = None
            if self.record_links:
                link.record()
                if self._agent_extra.get("peer", {}).get("bind"):
                    peer_link = self.loop.link(("127.0.0.1", 9), name=f"agent{i}-peer")
                    peer_link.record()
                    self._peer_links[i] = peer_link
            config = self._agent_config(i, data_dir)
            agent_env = self._base_env()
            agent_env.update(self._agent_env)
            argv = [
                "-m",
                "meshforge.agent",
                "run",
                "--config",
                str(config),
                "--controller",
                link.target,
            ]
            tmpfs: Path | None = None
            if i in self._agent_tmpfs:
                tmpfs = self._tmpfs_dir(i)
                tmpfs.mkdir(parents=True, exist_ok=True)
                argv = ["-c", _TMPFS_BOOT, str(tmpfs), str(self._agent_tmpfs[i]), *argv]
            proc = ManagedProcess(f"agent{i}", argv, agent_env, self.workdir / "logs")
            node = Node(
                i,
                proc,
                link,
                data_dir,
                fabric=self,
                tmpfs_workdir=tmpfs,
                peer_link=peer_link,
            )
            if self.enroll_agents:
                enroll_data_dir(data_dir, self.ca_dir)
            self.nodes.append(node)
        if self._start_agents:
            self.start_agents()

    def start_agents(self) -> None:
        """Start every agent process and wait until all are connected."""
        for node in self.nodes:
            node.process.start()
        for node in self.nodes:
            node.node_id = self._wait_node_id(node)
            if node.peer_link is not None:
                event = self.wait_log(
                    node.process, "agent.peer.listening", within=START_TIMEOUT_S
                )
                node.peer_link.retarget("127.0.0.1", int(event["port"]))
        ids = [n.node_id for n in self.nodes]
        if len(set(ids)) != len(ids):
            self.fail(f"agents share a node_id: {ids}")
        self.wait_all_connected(within=START_TIMEOUT_S)

    def links(self) -> list[FaultLink]:
        """Every link of this fabric (gRPC per agent, and with ``record_links``
        the REST link and each agent's peer link)."""
        return [] if self.loop is None else list(self.loop.links)

    @property
    def ca_dir(self) -> Path:
        """The controller's fabric CA folder."""
        return self.workdir / "controller-data" / "controller" / "ca"

    def _start_controller(self) -> None:
        assert self.controller is not None
        self.controller.start()
        event = self.wait_log(
            self.controller, "controller.listening", within=START_TIMEOUT_S
        )
        self.grpc_target = str(event["grpc"])
        self.http_base = f"{self.scheme}://{event['http']}"
        self.enroll_target = str(event.get("enroll") or "")
        if self.rest_link is not None:
            self.rest_link.retarget(*_host_port(str(event["http"])))
            self.http_base = f"{self.scheme}://{self.rest_link.target}"

    def _create_users(self, config: Path, env: Mapping[str, str]) -> None:
        """V0.7 G4 (D141): an admin and a normal user, created with the real
        ``meshforge-controller user create`` and logged in over the real
        ``POST /v1/auth/login`` (through the recorded REST link when
        ``record_links``). Their passwords and tokens are test secrets: the
        D146 recorder checks that none of them crosses a link in plaintext."""
        for name, admin in ((ADMIN_USER, True), (NORMAL_USER, False)):
            password = f"pw-{name}-{secrets.token_hex(12)}"
            created = subprocess.run(
                [sys.executable, "-m", "meshforge.controller", "user", "create",
                 name, "--password-stdin", "--config", str(config),
                 *(["--admin"] if admin else [])],
                env=dict(env), input=password + "\n", capture_output=True,
                text=True, timeout=60, check=False,
            )  # fmt: skip
            if created.returncode != 0:
                self.fail(f"user create {name} failed: {created.stderr[-2000:]}")
            response = self._http.post(
                self.http_base + "/v1/auth/login",
                json={"name": name, "password": password},
                timeout=30.0,
            )
            if response.status_code != 200:
                self.fail(f"login {name} failed: {response.status_code}")
            self._users[name] = (password, str(response.json()["token"]))

    def api_token(self, user: str = ADMIN_USER) -> str:
        """``user``'s API token from the harness login (default: the admin)."""
        return self._users[user][1]

    def password(self, user: str = ADMIN_USER) -> str:
        return self._users[user][0]

    def _wait_node_id(self, node: Node) -> str:
        event = self.wait_log(node.process, "agent.started", within=START_TIMEOUT_S)
        return str(event["node_id"])

    # ------------------------------------------------------------- teardown

    def close(self) -> None:
        """Kill every process and the proxy; assert nothing survived."""
        survivors: dict[str, list[int]] = {}
        procs = [n.process for n in self.nodes]
        if self.controller is not None:
            procs.append(self.controller)
        for proc in procs:
            alive = proc.terminate_tree()
            if alive:
                survivors[proc.name] = alive
        if self.loop is not None:
            self.loop.close()
        self._http.close()
        if not survivors:
            self._drop_secret_bases()
        if survivors:
            raise HarnessError(f"processes survived teardown: {survivors}")

    def _drop_secret_bases(self) -> None:
        """The agents are dead and their data dirs are never used again: do
        what their next start would have done (sweep), and remove the base
        folder itself so ``/dev/shm`` does not fill with one per test agent."""
        from meshforge.agent.execution.secret_files import (
            SecretVault,
            agent_tag,
            default_root,
        )

        for node in self.nodes:
            vault = SecretVault(
                default_root(node.data_dir), tag=agent_tag(node.data_dir)
            )
            with contextlib.suppress(OSError):
                vault.sweep()
                vault.base.rmdir()

    def all_pids(self) -> list[int]:
        procs = [n.process for n in self.nodes]
        if self.controller is not None:
            procs.append(self.controller)
        return sorted({pid for p in procs for pid in [*p.pids, *p.all_pids_seen()]})

    # ------------------------------------------------------------ controller

    def kill_controller(self) -> float:
        assert self.controller is not None
        at = time.monotonic()
        self.controller.kill()
        return at

    def stop_controller(self, timeout_s: float = 20.0) -> float:
        assert self.controller is not None
        at = time.monotonic()
        self.controller.interrupt()
        try:
            self.controller.wait_exit(timeout_s)
        except subprocess.TimeoutExpired:
            self.fail(f"controller did not exit {timeout_s} s after SIGINT")
        return at

    def restart_controller(self, *, graceful: bool = False) -> float:
        """Stop (kill by default) and start the controller on new ports; retarget
        every link. Returns the monotonic time the new process was started."""
        assert self.controller is not None
        if self.controller.running:
            if graceful:
                self.stop_controller()
            else:
                self.kill_controller()
        at = time.monotonic()
        self._start_controller()
        for node in self.nodes:
            node.link.retarget(*_host_port(self.grpc_target))
        return at

    # ------------------------------------------------------------- REST

    def get(self, path: str, **params: Any) -> Any | None:
        """GET ``/v1...`` as JSON (as the admin: D148, every read needs a user);
        ``None`` if the controller is unreachable."""
        try:
            resp = self._http.get(
                self.http_base + path,
                params=params or None,
                headers={"authorization": f"Bearer {self.api_token()}"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        return resp.json()

    def fabric_token(self) -> str:
        """The controller's fabric token (D44), read from its data dir."""
        path = self.workdir / "controller-data" / "controller" / "fabric.token"
        return path.read_text("utf-8").strip()

    def call(
        self, method: str, path: str, *, as_user: str = ADMIN_USER, **kwargs: Any
    ) -> httpx.Response:
        """An authenticated REST call (jobs, blobs) as ``as_user`` (default the
        admin, who sees every job); raises on transport errors."""
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["authorization"] = f"Bearer {self.api_token(as_user)}"
        return self._http.request(
            method, self.http_base + path, headers=headers, timeout=10.0, **kwargs
        )

    def nodes_by_id(self) -> dict[str, dict[str, Any]] | None:
        body = self.get("/v1/nodes")
        if body is None:
            return None
        return {n["node_id"]: n for n in body["nodes"]}

    def node_view(self, node: Node) -> dict[str, Any] | None:
        nodes = self.nodes_by_id()
        return None if nodes is None else nodes.get(node.node_id)

    def node_detail(self, node: Node) -> dict[str, Any] | None:
        body = self.get(f"/v1/nodes/{node.node_id}")
        return body if isinstance(body, dict) else None

    def sessions(self, node: Node, limit: int = 50) -> list[dict[str, Any]]:
        body = self.get("/v1/sessions", node_id=node.node_id, limit=limit)
        return [] if body is None else list(body["sessions"])

    def session(self, node: Node, session_id: str | None) -> dict[str, Any]:
        """One of ``node``'s sessions by id (REST lists them oldest first)."""
        for record in self.sessions(node):
            if record["session_id"] == session_id:
                return dict(record)
        self.fail(f"{node.name}: session {session_id} not listed over REST")

    def resources(self, node: Node, **params: Any) -> dict[str, Any] | None:
        body = self.get(f"/v1/nodes/{node.node_id}/resources", **params)
        return body if isinstance(body, dict) else None

    # -------------------------------------------------------------- waits

    def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        within: float,
        what: str,
        since: float | None = None,
    ) -> float:
        """Poll ``predicate`` every POLL_S; return seconds since ``since`` (or
        since the call) when it first holds; fail with every log tail if not."""
        start = time.monotonic() if since is None else since
        deadline = time.monotonic() + within
        while True:
            if predicate():
                return time.monotonic() - start
            if time.monotonic() > deadline:
                self.fail(f"timed out after {within} s waiting for: {what}")
            self._check_no_unexpected_exit()
            time.sleep(POLL_S)

    def wait_state(
        self,
        node: Node,
        state: str,
        *,
        within: float,
        connected: bool | None = None,
        since: float | None = None,
    ) -> float:
        """Wait until REST shows ``node`` in ``state`` (and ``connected`` if given)."""

        def holds() -> bool:
            view = self.node_view(node)
            if view is None or view["state"] != state:
                return False
            return connected is None or view["connected"] is connected

        extra = "" if connected is None else f", connected={connected}"
        return self.wait_until(
            holds,
            within=within,
            what=f"{node.name} ({node.node_id}) state={state}{extra} via REST",
            since=since,
        )

    def wait_all_connected(
        self,
        *,
        within: float,
        nodes: Iterable[Node] | None = None,
        since: float | None = None,
    ) -> float:
        """Wait until every node reads ``online`` and ``connected`` over REST."""
        wanted = list(self.nodes if nodes is None else nodes)

        def holds() -> bool:
            views = self.nodes_by_id()
            if views is None:
                return False
            return all(
                (v := views.get(n.node_id)) is not None
                and v["state"] == "online"
                and v["connected"] is True
                for n in wanted
            )

        return self.wait_until(
            holds,
            within=within,
            what=f"{[n.name for n in wanted]} online and connected via REST",
            since=since,
        )

    def wait_log(
        self,
        proc: ManagedProcess,
        event: str,
        *,
        within: float,
        after: int = 0,
        **match: Any,
    ) -> dict[str, Any]:
        """Wait for the ``after``-th+1 ``event`` in ``proc``'s current log."""
        found: list[dict[str, Any]] = []

        def holds() -> bool:
            found[:] = proc.log.events(event, **match)
            if len(found) > after:
                return True
            if not proc.running and proc.expected_alive:
                self.fail(f"{proc.name} exited ({proc.returncode}) before {event}")
            return False

        self.wait_until(holds, within=within, what=f"{proc.name} logs {event} {match}")
        return found[after]

    def _check_no_unexpected_exit(self) -> None:
        for proc in self._processes():
            if proc.expected_alive and not proc.running:
                self.fail(f"{proc.name} exited unexpectedly ({proc.returncode})")

    def _processes(self) -> list[ManagedProcess]:
        procs = [n.process for n in self.nodes]
        if self.controller is not None:
            procs.insert(0, self.controller)
        return procs

    # ------------------------------------------------------------ logs

    def controller_records(self) -> list[dict[str, Any]]:
        """Every controller log record, across restarts, tagged with ``_gen``."""
        assert self.controller is not None
        out: list[dict[str, Any]] = []
        for gen, log in enumerate(self.controller.logs):
            out.extend({**r, "_gen": gen} for r in log.records())
        return out

    def transitions(self, node: Node) -> list[Transition]:
        """``node.state_changed`` events for ``node``, in log order."""
        return [
            Transition(
                node_id=node.node_id,
                from_state=str(r["from"]),
                to_state=str(r["to"]),
                liveness_event=r.get("liveness_event"),
                session_id=r.get("session_id"),
                silence_s=r.get("silence_s"),
                lingering=r.get("lingering"),
                at=_timestamp(r),
                generation=int(r["_gen"]),
            )
            for r in self.controller_records()
            if r.get("event") == "node.state_changed"
            and r.get("node_id") == node.node_id
        ]

    def transition_counts(self) -> dict[str, int]:
        """A baseline: how many transitions each node has logged so far."""
        return {n.node_id: len(self.transitions(n)) for n in self.nodes}

    def new_transitions(
        self, node: Node, baseline: Mapping[str, int]
    ) -> list[Transition]:
        return self.transitions(node)[baseline.get(node.node_id, 0) :]

    def arrows(
        self, node: Node, baseline: Mapping[str, int]
    ) -> list[tuple[str, str, str | None]]:
        """``(from, to, liveness_event)`` of every transition after ``baseline``."""
        return [
            (t.from_state, t.to_state, t.liveness_event)
            for t in self.new_transitions(node, baseline)
        ]

    def assert_quiet(self, nodes: Iterable[Node], baseline: Mapping[str, int]) -> None:
        """No node in ``nodes`` logged any transition after ``baseline``."""
        noisy = {
            n.name: self.arrows(n, baseline) for n in nodes if self.arrows(n, baseline)
        }
        if noisy:
            self.fail(f"collateral transitions on untouched nodes: {noisy}")

    def agent_records(
        self, node: Node, event: str | None = None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for log in node.process.logs:
            out.extend(
                r for r in log.records() if event is None or r.get("event") == event
            )
        return out

    # ------------------------------------------------------- invariants

    def check_invariants(self, *, allow_events: Iterable[str] = ()) -> None:
        """The checks every scenario ends with (see the module docstring).

        No forbidden event and no traceback in any log; every agent not
        deliberately restarted still runs under its original pid; every process
        that should be alive is alive and not suspended.
        """
        problems: list[str] = []
        forbidden = FORBIDDEN_EVENTS - set(allow_events)
        for proc in self._processes():
            for gen, log in enumerate(proc.logs):
                label = f"{proc.name}.{gen}"
                for record in log.records():
                    if record.get("event") in forbidden:
                        problems.append(
                            f"{label}: forbidden event {record.get('event')}"
                        )
                    if "exception" in record:
                        problems.append(
                            f"{label}: traceback logged with {record.get('event')!r}"
                        )
                for raw in log.raw_lines():
                    if "Traceback" in raw:
                        problems.append(f"{label}: raw traceback in the log")
                        break
            if proc.expected_alive:
                if not proc.running:
                    problems.append(f"{proc.name}: exited ({proc.returncode})")
                elif proc.is_suspended():
                    problems.append(f"{proc.name}: still suspended")
        problems.extend(
            f"{node.name}: restarted without the test asking"
            for node in self.nodes
            if node.restarts == 0 and len(node.process.pids) != 1
        )
        if problems:
            self.fail("invariants violated:\n  " + "\n  ".join(problems))

    # ---------------------------------------------------------- failure

    def describe(self) -> str:
        parts = []
        for proc in self._processes():
            state = "running" if proc.running else f"exited({proc.returncode})"
            parts.append(
                f"--- {proc.name} gen {proc.generation} pid {proc.pid} {state}"
            )
            if proc.logs:
                parts.extend("    " + line for line in proc.log.tail())
        return "\n".join(parts)

    def fail(self, message: str) -> NoReturn:
        raise HarnessError(f"{message}\n{self.describe()}")


def seed_ca_dir(ca_dir: Path) -> None:
    """Install the test process's fixture CA (``mf_fabric``) at ``ca_dir``."""
    import mf_fabric

    mf_fabric.seed_ca_dir(ca_dir)


def enroll_data_dir(data_dir: Path, ca_dir: Path) -> str:
    """Create the agent identity in ``data_dir`` (if needed) and give it a node
    certificate from the CA at ``ca_dir``; returns its ``node_id``."""
    from meshforge.agent import enrollment
    from meshforge.agent.identity import load_or_create
    from meshforge.common import pki
    from meshforge.controller.fabric_ca import load_fabric

    authority = load_fabric(ca_dir).authority
    identity = load_or_create(data_dir / "identity")
    cert = pki.issue_node_cert(
        authority,
        pki.build_csr(
            identity.private_key,
            fabric_id=authority.fabric_id,
            node_id=identity.node_id,
        ),
        node_id=identity.node_id,
        fabric_id=authority.fabric_id,
    )
    enrollment.store(identity, cert, authority.cert, force=True)
    return str(identity.node_id)


MakeFabric = Callable[..., Fabric]
"""Type of the ``make_fabric`` fixture (``tests/cluster/conftest.py``)."""


@contextlib.contextmanager
def fabric(database: Any, workdir: Path, **kwargs: Any) -> Iterator[Fabric]:
    """``with fabric(pg_settings, tmp_path) as f:`` - start, yield, always close."""
    f = Fabric(database, workdir, **kwargs)
    with f:
        yield f
