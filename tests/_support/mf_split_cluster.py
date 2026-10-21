"""Helpers for Mode 3 (split job) scenarios on the cluster harness (V0.10 G9).

Imported as ``mf_split_cluster`` by ``tests/cluster/test_v010_mode3.py``. Built
on :mod:`mf_cluster` (the real controller + agent processes) - nothing here
starts a process or fakes a component; these are the REST / SQL / CLI reads a
split scenario needs, in one place:

* :func:`split_fabric` - a fabric whose agents run split jobs (subprocess
  runtime, Docker off, the job's pinned ``numpy`` from the package index);
* :func:`upload_folder`, :func:`submit`, :func:`wait_job`, :func:`output_files`
  - the user's path (tree upload, ``POST /v1/jobs``, the job's verified output);
* :func:`compute` - the real ``compute`` CLI as a subprocess, as a laptop with an
  enrolled agent runs it (its node is the Local candidate, its cache seeded);
* :func:`sql` and :func:`check_exactly_once` - the work-queue / chunk-record
  ledger every split job must satisfy (checklist 32/35: every chunk committed
  exactly once, every lease accounted for by a chunk record).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from mf_cluster import Fabric, Timing
from mf_split_ledger import assert_exactly_once

if TYPE_CHECKING:
    from meshforge.bench.workloads.materialise import BuiltJob

__all__ = [
    "EXEC",
    "TERMINAL",
    "TIMING",
    "build_split_job",
    "check_exactly_once",
    "compute",
    "index_reachable",
    "job",
    "lanes",
    "output_files",
    "split_fabric",
    "sql",
    "submit",
    "upload_folder",
    "wait_job",
]

TIMING: Final = Timing(
    name="mode3",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC: Final = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})


def index_reachable() -> bool:
    """The package index answers (the benchmark families pin ``numpy``)."""
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def split_fabric(
    make_fabric: Callable[..., Fabric],
    *,
    agents: int = 3,
    controller_extra: Mapping[str, Mapping[str, Any]] | None = None,
    **kwargs: Any,
) -> Fabric:
    """``agents`` real agents that run split jobs: subprocess runtime, Docker
    unreachable, short dispatch tick, lease timeouts sized for loopback.
    ``controller_extra`` tables are merged over the split defaults."""
    import certifi

    extra: dict[str, dict[str, Any]] = {
        "split": {"visibility_timeout_s": 20.0, "wait_retry_s": 0.3},
    }
    for table, values in (controller_extra or {}).items():
        extra.setdefault(table, {}).update(values)
    return make_fabric(
        agents=agents,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra=extra,
        agent_env={
            "DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock",
            # The harness points SSL_CERT_FILE at the test fabric CA; the env
            # builder (uv) needs the public roots to reach the package index
            # for the job's pinned numpy, as on a real laptop.
            "SSL_CERT_FILE": certifi.where(),
        },
        **kwargs,
    )


def build_split_job(
    family: str, params: Mapping[str, Any], seed: int, dest: Path
) -> BuiltJob:
    """The family's job folder (:func:`meshforge.bench.workloads.build_job`)
    for a size **below** the benchmark window (5 s on the reference laptop):
    the window is a campaign rule (a measurement must be long enough to
    mean something), not a property of the job - a harness-size split job is
    the same folder, with fewer items. Everything else (params model, input
    size caps, seed range) is still checked."""
    from meshforge.bench.workloads import base, build_job

    saved = base.MIN_RUNTIME_S
    base.MIN_RUNTIME_S = 0.0  # type: ignore[misc]
    try:
        return build_job(family, dict(params), seed, dest)
    finally:
        base.MIN_RUNTIME_S = saved  # type: ignore[misc]


def upload_folder(fabric: Fabric, root: Path) -> str:
    """Upload ``root`` as a content-addressed tree; its ``tree_id``."""
    from pydantic import SecretStr

    from meshforge.cli.client import ControllerClient
    from meshforge.cli.transfer import upload_tree
    from meshforge.common.tree import build_tree

    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base, timeout_s=60, token=SecretStr(fabric.api_token())
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()
    return str(built.tree.tree_id)


def submit(
    fabric: Fabric,
    manifest: Mapping[str, Any],
    tree: str,
    *,
    node_id: str | None = None,
) -> str:
    """``POST /v1/jobs`` (auto placement, or pinned to ``node_id``)."""
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": dict(manifest),
            "tree_id": tree,
            "placement": {"node_id": node_id} if node_id else {},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def lanes(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The job's lane attempts (``role == "lane"``)."""
    return [dict(a) for a in view["attempts"] if a.get("role") == "lane"]


def wait_job(fabric: Fabric, job_id: str, within: float) -> dict[str, Any]:
    """Poll until the job is terminal (bounded; fails with every log tail)."""
    deadline = time.monotonic() + within
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = job(fabric, job_id)
        if view["state"] in TERMINAL:
            return view
        time.sleep(0.2)
    fabric.fail(f"job {job_id} did not end within {within} s: {view}")


def output_files(fabric: Fabric, digest: str, dest: Path) -> dict[str, bytes]:
    """The job output bundle ``digest`` (hash checked), unpacked under
    ``dest``: relative POSIX path -> bytes."""
    from meshforge.common.bundle import unpack

    data = fabric.call("GET", f"/v1/blobs/{digest.removeprefix('sha256:')}").content
    assert hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
    archive = dest.with_suffix(".bundle")
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    unpack(archive, dest)
    return {
        p.relative_to(dest).as_posix(): p.read_bytes()
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }


async def _rows(pg_settings: Any, query: str, params: Mapping[str, Any]) -> list[Any]:
    from sqlalchemy import text

    from meshforge.controller.store.engine import create_engine

    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(query), dict(params))).all())
    finally:
        await engine.dispose()


def sql(pg_settings: Any, query: str, **params: Any) -> list[Any]:
    """One read-only query on the fabric's database (its own engine)."""
    return asyncio.run(_rows(pg_settings, query, params))


def chunk_counts(pg_settings: Any, job_id: str) -> tuple[int, int]:
    """(chunks done, chunks in the queue) of a split job."""
    rows = sql(
        pg_settings,
        "SELECT count(*) FILTER (WHERE i.state = 'done') AS done, count(*) AS total "
        "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :job",
        job=job_id,
    )
    return int(rows[0].done), int(rows[0].total)


def check_exactly_once(pg_settings: Any, job_id: str, items: int) -> list[Any]:
    """The ledger of a SUCCEEDED split job (checklist 32/35, criterion 4),
    read from the fabric's database and checked by
    :func:`mf_split_ledger.assert_exactly_once`: every chunk ``done`` with
    exactly one ``completed`` lease and one ``committed`` chunk record, the
    committed chunks covering items ``0 .. items-1`` once, a record for every
    lease ever granted - and **no lease still** ``live``.

    Returns the chunk records (for scenario-specific checks)."""
    leases = sql(
        pg_settings,
        "SELECT i.idx, i.state AS item_state, i.done_lease_seq, i.result_digest, "
        " l.seq, l.state, l.holder "
        "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
        "LEFT JOIN work_lease l ON l.item_id = i.id "
        "WHERE q.owner_id = :job ORDER BY i.idx, l.seq",
        job=job_id,
    )
    records = sql(
        pg_settings,
        "SELECT chunk_index, lease_seq, lane_attempt_id, node_id, first_item, items, "
        " speculative, won, outcome, result_digest, leased_at, ended_at "
        "FROM chunk_record WHERE job_id = :job ORDER BY chunk_index, lease_seq",
        job=job_id,
    )
    assert_exactly_once(leases, records, items)
    return records


def compute(
    fabric: Fabric, local_data: Path, *args: str, cwd: Path, timeout_s: float = 300
) -> subprocess.CompletedProcess[str]:
    """The real ``compute`` CLI on the laptop whose agent keeps ``local_data``
    (its node is the Local candidate; ``compute run`` seeds its cache)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(local_data),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
            "NO_COLOR": "1",
            "COLUMNS": "200",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
