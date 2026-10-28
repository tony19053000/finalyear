"""The demo scenarios' ``--kill-cmd`` / ``--revive-cmd`` on the cluster harness
(V1.0 G5, D420). Imported as ``mf_demo_ctl`` by ``tests/cluster/``; also run as
a script, which is the command the demo runs::

    python tests/_support/mf_demo_ctl.py CONTROL_DIR kill NODE_ID
    python tests/_support/mf_demo_ctl.py CONTROL_DIR revive NODE_ID

The script writes ``<id>.request.json`` into ``CONTROL_DIR`` and waits for
``<id>.done.json``. :class:`DemoControl` (in the test process, which
owns the fabric) does the action **through the harness** - ``Node.kill()``
(SIGKILL, so the invariant check knows the death was asked for) and
``Node.restart()`` - like the soak dry run's chaos harness: on a lab laptop the
same flags name ``ssh lap3 sudo systemctl kill meshforge-agent`` instead.

The requests are served on the **calling (main) thread** while it waits for
the demo process (:meth:`DemoControl.run`): an agent started from a helper
thread would die with that thread (the agents' parent-death signal is per
thread on Linux, ``PR_SET_PDEATHSIG``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from mf_cluster import Fabric

WAIT_S: Final = 60.0


@dataclass
class DemoControl:
    """Serves the requests the demo's hooks write while the demo runs."""

    fabric: Fabric
    folder: Path
    done: list[tuple[str, str]] = field(default_factory=list)

    def run(
        self, argv: list[str], env: dict[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        """Run the demo, serving its hook requests until it exits."""
        self.folder.mkdir(parents=True, exist_ok=True)
        out, err = self.folder / "demo.stdout", self.folder / "demo.stderr"
        deadline = time.monotonic() + timeout_s
        with out.open("w", encoding="utf-8") as o, err.open("w", encoding="utf-8") as e:
            proc = subprocess.Popen(argv, env=env, stdout=o, stderr=e, text=True)
            try:
                while proc.poll() is None:
                    for request in sorted(self.folder.glob("*.request.json")):
                        self._serve(request)
                    if time.monotonic() > deadline:
                        proc.kill()
                        break
                    time.sleep(0.1)
            finally:
                code = proc.wait(timeout=30)
        return subprocess.CompletedProcess(
            argv, code, out.read_text(encoding="utf-8"), err.read_text(encoding="utf-8")
        )

    def _serve(self, request: Path) -> None:
        data = json.loads(request.read_text(encoding="utf-8"))
        action, node_id = str(data["action"]), str(data["node_id"])
        result = {"ok": True, "detail": ""}
        try:
            node = next(n for n in self.fabric.nodes if n.node_id == node_id)
            if action == "kill":
                node.kill()
            elif action == "revive":
                node.restart(graceful=False)
            else:
                raise ValueError(f"unknown action {action!r}")
            self.done.append((action, node_id))
        except Exception as exc:  # noqa: BLE001 - reported to the hook
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        request.unlink()
        out = request.with_name(request.name.replace(".request.", ".done."))
        out.write_text(json.dumps(result), encoding="utf-8")


def command(folder: Path, action: str) -> str:
    """The ``--kill-cmd`` / ``--revive-cmd`` template for the demo."""
    return f'"{sys.executable}" "{Path(__file__).resolve()}" "{folder}" {action} {{node_id}}'


def main(argv: list[str]) -> int:
    folder, action, node_id = Path(argv[0]), argv[1], argv[2]
    rid = uuid.uuid4().hex
    tmp = folder / f"{rid}.tmp"
    tmp.write_text(json.dumps({"action": action, "node_id": node_id}), encoding="utf-8")
    tmp.replace(folder / f"{rid}.request.json")
    done = folder / f"{rid}.done.json"
    deadline = time.monotonic() + WAIT_S
    while not done.exists():
        if time.monotonic() > deadline:
            print(f"mf_demo_ctl: no answer to {action} {node_id}", file=sys.stderr)  # noqa: T201
            return 1
        time.sleep(0.1)
    result = json.loads(done.read_text(encoding="utf-8"))
    if not result.get("ok"):
        print(f"mf_demo_ctl: {result.get('detail')}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
