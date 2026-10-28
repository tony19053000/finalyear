"""The V1.0 G1 soak dry run: the real lab kit against a local cluster fabric.

Imported as ``mf_soak_dryrun`` by ``tests/cluster/soak/``. Nothing here fakes a
component: the controller and the agents are the :mod:`mf_cluster` processes,
and the four lab tools run as the subprocesses a person starts on the
controller laptop -

* ``lab/soak.py run`` (the resource sampler, killed once mid-run and started
  again: the resume path),
* ``lab/fabric_check.py soak`` (the transition recorder, D410 rule 7),
* ``lab/soak_load.py run`` / ``collect`` (the seeded load and the final view),
* ``lab/chaos.py run --control-dir`` (the seeded chaos schedule).

Every laptop of the plan has ``executor: harness``: ``chaos.py`` writes one
``<id>.request.json`` per action into the control folder and waits for
``<id>.done.json``. :class:`ChaosHarness` (in the test process, which owns the
fabric) does the action on the fabric - the same fault the Linux command
template does on a real laptop, with the harness's means:

==================  ====================================================
``kill_agent``      SIGKILL the agent; the "service" restarts it 2 s later
``kill_controller`` SIGKILL the controller; restarted (new ports, every
                    link retargeted) 2 s later
``reboot``          the target laptop's processes killed, started again
                    after the (scaled) duration
``net_down_*``      the laptop's links blackholed for the duration (the
                    controller laptop: every agent link and the REST link)
``netem``           200 ms latency on the laptop's links (loss: not
                    supported by the fault proxy - stated in the report)
``kill_job_tree``   SIGKILL every descendant of the agent (its job trees)
==================  ====================================================

``cancel_mode3`` is done by ``chaos.py`` itself over REST (as in the lab).
Not in the dry run - the lab campaign covers them (``lab/checklists/V1.0-soak.md``):
``restart_postgres`` (the database is the test session's shared container),
``revoke_rejoin`` (needs a person), ``upgrade_reinstall`` (needs the packages)
and ``disk_fill``: the agent refuses work below a fixed 1 GiB free-disk floor
(``agent/cas/store.py``), so a fillable namespaced tmpfs would have to be
> 1 GiB of RAM per agent; the disk-full path is proven by
``tests/cluster/test_v06_g3_disk_full_node.py`` and the chaos suite.

The REST API is reached through one stable proxy link (:attr:`ChaosHarness.rest`)
retargeted after every controller restart, so the tools keep one URL while
the controller changes ports, like the installed service keeps 47110.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from mf_cluster import Fabric, Node, _host_port
from mf_faultproxy import FaultLink

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
LAB_DIR: Final = REPO_ROOT / "lab"
CONTROLLER_LABEL: Final = "ctl"
SERVICE_RESTART_S: Final = 2.0
"""How long the "service manager" waits before it restarts a killed process."""
HARNESS_KINDS: Final = (
    "kill_agent",
    "kill_controller",
    "reboot",
    "net_down_short",
    "net_down_long",
    "netem",
    "kill_job_tree",
    "cancel_mode3",
)
"""The D411 kinds the dry run does (module docstring)."""
NETEM_DELAY_MS: Final = 200.0


def agent_label(index: int) -> str:
    return f"a{index}"


def targets_doc(fabric: Fabric) -> dict[str, Any]:
    """``targets.json`` of the dry run: the controller laptop + one per agent
    (all on this machine; ``node_ids`` map fabric nodes to them)."""
    rows: list[dict[str, Any]] = [
        {
            "label": CONTROLLER_LABEL,
            "os": "linux",
            "roles": ["controller"],
            "executor": "harness",
        }
    ]
    rows.extend(
        {
            "label": agent_label(node.index),
            "os": "linux",
            "roles": ["agent"],
            "executor": "harness",
            "node_ids": [node.node_id],
        }
        for node in fabric.nodes
    )
    return {"targets": rows}


@dataclass
class _Pending:
    at: float
    what: str
    undo: Callable[[], None]


@dataclass
class ChaosHarness:
    """Does each ``chaos.py`` request on the fabric (module docstring)."""

    fabric: Fabric
    control_dir: Path
    rest: FaultLink
    log: list[dict[str, Any]] = field(default_factory=list)
    _pending: list[_Pending] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    @classmethod
    def start(cls, fabric: Fabric, control_dir: Path) -> ChaosHarness:
        assert fabric.loop is not None
        control_dir.mkdir(parents=True, exist_ok=True)
        rest = fabric.loop.link(_host_port(_netloc(fabric.http_base)), name="soak-rest")
        return cls(fabric, control_dir, rest)

    @property
    def url(self) -> str:
        return f"https://{self.rest.target}"

    # ----------------------------------------------------------- targets
    def _node(self, label: str) -> Node:
        for node in self.fabric.nodes:
            if agent_label(node.index) == label:
                return node
        raise KeyError(label)

    def _links(self, label: str) -> list[FaultLink]:
        if label == CONTROLLER_LABEL:
            return [n.link for n in self.fabric.nodes] + [self.rest]
        return [self._node(label).link]

    def _restart_controller(self) -> None:
        self.fabric.restart_controller(graceful=False)
        self.rest.retarget(*_host_port(_netloc(self.fabric.http_base)))

    def _start_agent(self, node: Node) -> None:
        if not node.process.running:
            node.process.start()
            node.restarts += 1

    # ------------------------------------------------------------- loop
    def busy(self) -> bool:
        return bool(self._pending)

    def poll(self) -> None:
        """Answer every new request; undo every fault that is due."""
        self.restore_due()
        for request in sorted(self.control_dir.glob("*.request.json")):
            action_id = request.name.removesuffix(".request.json")
            if action_id in self._seen:
                continue
            self._seen.add(action_id)
            action = json.loads(request.read_text("utf-8"))
            try:
                outcome, detail = self._do(action)
            except Exception as exc:  # noqa: BLE001 - a failed action is a log row
                outcome, detail = "failed", f"{type(exc).__name__}: {exc}"
            self.log.append(
                {"id": action_id, "kind": action["kind"], "target": action["target"],
                 "outcome": outcome, "detail": detail, "t": round(time.monotonic(), 2)}
            )  # fmt: skip
            ack = self.control_dir / f"{action_id}.done.json"
            tmp = ack.with_suffix(".tmp")
            tmp.write_text(json.dumps({"outcome": outcome, "detail": detail}), "utf-8")
            os.replace(tmp, ack)

    def restore_due(self, *, all_now: bool = False) -> None:
        now = time.monotonic()
        keep: list[_Pending] = []
        for p in sorted(self._pending, key=lambda p: p.at):
            if all_now or now >= p.at:
                p.undo()
            else:
                keep.append(p)
        self._pending = keep

    def _later(self, delay_s: float, what: str, undo: Callable[[], None]) -> None:
        self._pending.append(_Pending(time.monotonic() + delay_s, what, undo))

    # ----------------------------------------------------------- actions
    def _do(self, action: Mapping[str, Any]) -> tuple[str, str]:
        kind, label = str(action["kind"]), str(action["target"])
        duration = float(action.get("duration_s") or 0.0)
        if kind == "kill_controller" or (
            kind == "reboot" and label == CONTROLLER_LABEL
        ):
            self.fabric.kill_controller()
            self._later(
                duration or SERVICE_RESTART_S, "controller up", self._restart_controller
            )
            return "done", "controller SIGKILL; restarted by the harness"
        if kind in ("kill_agent", "reboot"):
            node = self._node(label)
            node.kill()
            self._later(
                duration or SERVICE_RESTART_S,
                f"{label} up",
                lambda n=node: self._start_agent(n),
            )
            return "done", f"{node.name} SIGKILL"
        if kind in ("net_down_short", "net_down_long"):
            links = self._links(label)
            for link in links:
                link.blackhole(True)
            self._later(duration, f"{label} net up", lambda: _each(links, _unblackhole))
            return "done", f"{len(links)} links blackholed for {duration:g} s"
        if kind == "netem":
            links = self._links(label)
            for link in links:
                link.latency(NETEM_DELAY_MS)
            self._later(
                duration, f"{label} netem off", lambda: _each(links, _no_latency)
            )
            return "done", f"{NETEM_DELAY_MS:g} ms on {len(links)} links (no loss)"
        if kind == "kill_job_tree":
            return "done", f"killed={_kill_descendants(self._node(label).pid)}"
        return "skipped", f"{kind} is not done by the dry-run harness"


def _netloc(url: str) -> str:
    return url.split("://", 1)[-1]


def _each(links: Sequence[FaultLink], fn: Callable[[FaultLink], None]) -> None:
    for link in links:
        fn(link)


def _unblackhole(link: FaultLink) -> None:
    link.blackhole(False)


def _no_latency(link: FaultLink) -> None:
    link.latency(0.0)


def _kill_descendants(pid: int) -> int:
    import psutil

    killed = 0
    with contextlib.suppress(psutil.Error):
        for child in psutil.Process(pid).children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.send_signal(signal.SIGKILL)
                killed += 1
    return killed


# ------------------------------------------------------------- lab tools
def tool_env(harness: ChaosHarness, scratch: Path) -> dict[str, str]:
    """What ``compute login`` would leave on the controller laptop, as env:
    the URL (the stable REST link), the fabric CA, the admin's API token; a
    private data dir / config home so a developer's own login is never read."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": harness.url,
            "MESHFORGE_CLI__CA_FILE": str(harness.fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": harness.fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(scratch / "cli-data"),
            "XDG_CONFIG_HOME": str(scratch / "cli-config"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@dataclass
class LabTool:
    """One lab script as a subprocess, its output in ``<logs>/<name>.N.log``."""

    name: str
    argv: list[str]
    env: Mapping[str, str]
    logs: Path
    proc: subprocess.Popen[bytes] | None = None
    runs: int = 0
    _out: IO[bytes] | None = None

    def start(self) -> LabTool:
        self._close()
        self.logs.mkdir(parents=True, exist_ok=True)
        self.runs += 1
        self._out = (self.logs / f"{self.name}.{self.runs}.log").open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, *self.argv],
            env=dict(self.env),
            cwd=REPO_ROOT,
            stdout=self._out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        return self

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def wait(self, timeout_s: float) -> int:
        assert self.proc is not None
        code = self.proc.wait(timeout_s)
        self._close()
        return code

    def _close(self) -> None:
        if self._out is not None:
            self._out.close()
            self._out = None

    def output(self) -> str:
        return "\n".join(
            p.read_text("utf-8", errors="replace")
            for p in sorted(self.logs.glob(f"{self.name}.*.log"))
        )

    def stop(self) -> None:
        """Teardown: whatever still runs is killed (never left behind)."""
        self.kill()
        self._close()


def lab_tool(name: str, script: str, args: Sequence[str], env: Mapping[str, str],
             logs: Path) -> LabTool:  # fmt: skip
    return LabTool(name, [str(LAB_DIR / script), *args], env, logs)
