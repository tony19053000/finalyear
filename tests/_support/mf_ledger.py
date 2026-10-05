"""A workload that leaves evidence of every execution (V0.6 G3, D115/D118).

Imported as ``mf_ledger`` by the crash-recovery and chaos cluster tests.

Every attempt of a ledger job runs :data:`WORKLOAD` (the subprocess runtime,
one shared directory on this machine), which

1. takes an exclusive, non-blocking lock on ``<ledger>/locks/<job_id>`` - the
   kernel releases it when the process dies, so a second attempt of the same
   job that finds it held is running **at the same time** as another one: it
   records ``overlap`` instead of ``start``;
2. optionally waits for ``<ledger>/release/<job_id>`` (the test decides when a
   job may finish);
3. sleeps, then appends its attempt id to ``<ledger>/side/<job_id>`` - the
   **side effect** whose count the tests check - and records ``end``;
4. exits with the requested code.

``ledger.txt`` lines are ``<unix time> <kind> <job_id> <attempt_id>``, each one
``O_APPEND`` write (atomic on a local file). :class:`Ledger` reads them back.
The ids come from the agent (``MESHFORGE_JOB_ID`` / ``MESHFORGE_ATTEMPT_ID``).
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "WORKLOAD",
    "Ledger",
    "LedgerLine",
    "job_view",
    "ledger_manifest",
    "make_ledger",
    "submit",
    "upload_workload",
]

WORKLOAD = """\
import os, sys, time
ledger, sleep_s, exit_code, wait_release = (
    sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4] == "1"
)
job = os.environ["MESHFORGE_JOB_ID"]
attempt = os.environ["MESHFORGE_ATTEMPT_ID"]


def record(kind):
    line = "%.6f %s %s %s\\n" % (time.time(), kind, job, attempt)
    fd = os.open(
        os.path.join(ledger, "ledger.txt"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
    )
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


lock = os.open(os.path.join(ledger, "locks", job), os.O_RDWR | os.O_CREAT, 0o644)
try:
    import fcntl

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        overlap = False
    except OSError:
        overlap = True
except ImportError:  # Windows
    import msvcrt

    try:
        msvcrt.locking(lock, msvcrt.LK_NBLCK, 1)
        overlap = False
    except OSError:
        overlap = True
record("overlap" if overlap else "start")
if wait_release:
    release = os.path.join(ledger, "release", job)
    deadline = time.monotonic() + 600
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.05)
time.sleep(sleep_s)
with open(os.path.join(ledger, "side", job), "a") as fh:
    fh.write(attempt + "\\n")
record("end")
sys.exit(exit_code)
"""


def make_ledger(root: Path) -> Path:
    for sub in ("locks", "release", "side"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "ledger.txt").touch()
    return root


def ledger_manifest(
    ledger: Path,
    *,
    sleep_s: float,
    exit_code: int = 0,
    wait_release: bool = False,
    name: str = "ledger",
    **extra: Any,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": name,
        "entrypoint": [
            "python",
            "main.py",
            str(ledger),
            f"{sleep_s:.3f}",
            str(exit_code),
            "1" if wait_release else "0",
        ],
    }
    manifest.update(extra)
    return manifest


@dataclasses.dataclass(frozen=True)
class LedgerLine:
    at: float
    kind: str
    job_id: str
    attempt_id: str


def upload_workload(fabric: Any, workdir: Path) -> str:
    """Pack :data:`WORKLOAD` as ``main.py``, upload it; the bundle digest."""
    from meshforge.common.bundle import pack_to_file

    project = workdir / "ledger-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    bundle = workdir / "ledger-project.bundle"
    packed = pack_to_file(project, bundle)
    put = fabric.call(
        "PUT", f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes()
    )
    assert put.status_code in (200, 201), put.text
    return str(packed.digest)


def submit(
    fabric: Any,
    bundle_digest: str,
    manifest: Mapping[str, Any],
    placement: Mapping[str, Any] | None = None,
) -> str:
    """POST an (auto-placed, unless ``placement``) job; its id."""
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": dict(manifest),
            "bundle_digest": bundle_digest,
            "placement": dict(placement or {}),
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def job_view(fabric: Any, job_id: str) -> dict[str, Any] | None:
    """``GET /v1/jobs/{id}`` or None while the controller is unreachable."""
    try:
        resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    except Exception:  # noqa: BLE001 - controller down / restarting
        return None
    return dict(resp.json()) if resp.status_code == 200 else None


class Ledger:
    """What the workloads wrote (module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def lines(self) -> list[LedgerLine]:
        out = []
        for raw in (self.root / "ledger.txt").read_text().splitlines():
            parts = raw.split()
            if len(parts) == 4:
                out.append(LedgerLine(float(parts[0]), parts[1], parts[2], parts[3]))
        return out

    def by_job(self) -> Mapping[str, list[LedgerLine]]:
        grouped: dict[str, list[LedgerLine]] = defaultdict(list)
        for line in self.lines():
            grouped[line.job_id].append(line)
        return grouped

    def side_effects(self, job_id: str) -> list[str]:
        path = self.root / "side" / job_id
        return path.read_text().split() if path.exists() else []

    def release(self, job_ids: Iterable[str]) -> None:
        for job_id in job_ids:
            (self.root / "release" / job_id).touch()
