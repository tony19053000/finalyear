"""What the fabric is made to hold and do while it is measured: the seeded
job history (run for real, or submitted and cancelled) and a steady job
stream - all through the real REST API with an admin token."""

from __future__ import annotations

import dataclasses
import ssl
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final

import httpx

TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})
#: Cancelled jobs added to the run history so a state filter has two
#: non-empty answers to switch between (succeeded <-> cancelled).
MIXED_CANCELLED: Final = 20
#: A pinned submit refused only because its node is SUSPECT for a moment is
#: retried this long (V1.0 G0). Under the 4x100 burst the test fabric's FAST
#: liveness (SUSPECT after 0.25 s of silence: a 50 ms allowance) sees heartbeats
#: a few tens of ms late; the node is SUSPECT for ~3 ms and back ONLINE on the
#: next heartbeat. Measured: 0 transitions with the shipped 0.5 s allowance,
#: 1 per run with FAST. Seeding history is not the measurement - wait it out.
SUSPECT_RETRY_S: Final = 10.0
SUSPECT_RETRY_PAUSE_S: Final = 0.05


@dataclasses.dataclass
class Api:
    """A bearer-token REST client for seeding and reading the truth."""

    base: str
    token: str
    verify: ssl.SSLContext

    def __post_init__(self) -> None:
        self.http = httpx.Client(
            base_url=self.base,
            verify=self.verify,
            timeout=30.0,
            headers={"authorization": f"Bearer {self.token}"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        )

    def close(self) -> None:
        self.http.close()

    def ok(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

    def node_ids(self) -> list[str]:
        nodes = self.ok("GET", "/v1/nodes")["nodes"]
        return [n["node_id"] for n in nodes if n["state"] == "online"]

    def upload_project(self, root: Path) -> str:
        """Upload ``root`` as a tree (the CLI's own tree builder); its id."""
        from meshforge.common.tree import build_tree

        built = build_tree(root)
        for hex_, path in built.sources.items():
            self.ok("PUT", f"/v1/blobs/{hex_}", content=path.read_bytes())
        self.ok("PUT", f"/v1/blobs/{built.tree.tree_id.hex}", content=built.manifest)
        return str(built.tree.tree_id)

    def submit(self, tree_id: str, name: str, node_id: str | None = None) -> str:
        body: dict[str, Any] = {
            "manifest": {
                "name": name,
                "entrypoint": ["python", "main.py"],
                "retry": {"max_attempts": 1},
            },
            "tree_id": tree_id,
        }
        if node_id is not None:
            body["placement"] = {"node_id": node_id}
        deadline = time.monotonic() + SUSPECT_RETRY_S
        while True:
            resp = self.http.post("/v1/jobs", json=body)
            if not (
                node_id is not None
                and is_transient_suspect(resp)
                and time.monotonic() < deadline
            ):
                break
            time.sleep(SUSPECT_RETRY_PAUSE_S)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST /v1/jobs -> {resp.status_code}: {resp.text}")
        return str(resp.json()["job_id"])

    def cancel(self, job_id: str) -> None:
        self.ok("POST", f"/v1/jobs/{job_id}/cancel")

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self.ok("GET", f"/v1/jobs/{job_id}"))


def is_transient_suspect(resp: httpx.Response) -> bool:
    """``409 no_feasible_plan`` because the pinned node is SUSPECT - the one
    refusal :meth:`Api.submit` retries (any other error is raised at once)."""
    if resp.status_code != 409:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    return bool(
        error.get("code") == "no_feasible_plan"
        and error.get("message") == "the node is suspect, not online"
    )


def tiny_project(root: Path) -> Path:
    project = root / "ui-perf-job"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text(
        "import sys\nprint('ui-perf', sys.argv)\n", encoding="utf-8"
    )
    return project


@dataclasses.dataclass
class Seeded:
    jobs: int
    seconds: float
    states: dict[str, int]


def wait_terminal(api: Api, ids: Sequence[str], timeout_s: float) -> dict[str, int]:
    pending = set(ids)
    states: dict[str, int] = {}
    deadline = time.monotonic() + timeout_s
    while pending:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{len(pending)} seeded jobs never finished")
        for job_id in list(pending):
            state = api.job(job_id)["state"]
            if state in TERMINAL:
                pending.discard(job_id)
                states[state] = states.get(state, 0) + 1
        if pending:
            time.sleep(1.0)
    return states


def seed_history(
    api: Api, tree_id: str, per_node: int, timeout_s: float = 1800.0
) -> Seeded:
    """``per_node`` jobs pinned to EACH online node, run for real."""
    started = time.monotonic()
    nodes = api.node_ids()
    if not nodes:
        raise RuntimeError("no online node to seed history on")
    ids = [
        api.submit(tree_id, f"history-{n:02d}-{i:03d}", node_id)
        for i in range(per_node)
        for n, node_id in enumerate(nodes)
    ]
    states = wait_terminal(api, ids, timeout_s)
    return Seeded(len(ids), round(time.monotonic() - started, 1), states)


def seed_bulk(api: Api, tree_id: str, count: int, workers: int = 8) -> Seeded:
    """``count`` jobs submitted and cancelled at once (rows in the real DB;
    no agent spends time on them)."""
    started = time.monotonic()

    def one(i: int) -> None:
        job_id = api.submit(tree_id, f"bulk-{i:05d}")
        api.cancel(job_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, range(count)):
            pass
    return Seeded(count, round(time.monotonic() - started, 1), {"cancelled": count})


class JobStream(threading.Thread):
    """A steady stream of small auto-placed jobs (one every ``interval_s``)."""

    def __init__(self, api: Api, tree_id: str, interval_s: float = 1.0) -> None:
        super().__init__(name="ui-perf-job-stream", daemon=True)
        self.api, self.tree_id, self.interval_s = api, tree_id, interval_s
        self.ids: list[str] = []
        self.errors: list[str] = []
        self._halt = threading.Event()

    def run(self) -> None:
        n = 0
        while not self._halt.is_set():
            try:
                self.ids.append(self.api.submit(self.tree_id, f"stream-{n:04d}"))
            except Exception as exc:  # noqa: BLE001 - reported, the stream goes on
                self.errors.append(str(exc))
            n += 1
            self._halt.wait(self.interval_s)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=30)
