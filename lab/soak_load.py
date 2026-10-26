# ruff: noqa: T201 - a lab tool: its output is the point
"""V1.0 lab kit (roadmap 1.0.2, D409): the soak's seeded mixed job load.

Runs on the **controller laptop** after ``compute login`` (the token and CA
``compute`` uses; ``$MESHFORGE_CLI__CONTROLLER_URL`` for the URL). The run
folder's ``plan.json`` (``soak.py plan``) decides every job: kind, size,
policy, priority, arrival time, planned cancel (``lab/_soak/load.py``).

    uv run --extra controller python lab/soak_load.py run --dir DIR
        Submit every job at its time until the plan's load end, writing the
        submit log DIR/ledger.jsonl (intent before the POST, job id after).
        Each submission carries an idempotency key, so a killed generator
        re-run with the same command re-sends unanswered submissions with the
        same key and the controller answers with the job it already has - a
        job is never submitted twice. Jobs more than --catchup-min late (the
        generator was down) are recorded as skipped, not burst.
    uv run --extra controller python lab/soak_load.py collect --dir DIR
            [--wait-min 60] [--verify-sample 100]
        Wait until every ledger job is terminal (or the wait ends), then write
        DIR/jobs-final.json: each job's controller view, the node list, and a
        content check of a seeded sample of outputs (a Mode 1 job's
        out/result.txt names its own job id; a Mode 3 job's output passes its
        family's whole-output check).

Exit codes: 0 done; 1 cannot write a file; 2 usage error; 3 a submission was
refused for good (see the ledger) / collect: jobs still not terminal;
6 the controller could not be reached at the start; 130 interrupted (resumable).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import yaml

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _fabric.api import fabric_ssl_context, lab_token  # noqa: E402
from _fabric.clock import iso  # noqa: E402
from _soak import jsonl  # noqa: E402
from _soak.ledger import LEDGER_SCHEMA, reduce  # noqa: E402
from _soak.load import (  # noqa: E402
    BACKOFF_S,
    BENCH_FAMILY,
    MAX_ATTEMPTS,
    SPLIT_FAMILY,
    JobSpec,
    idempotency_key,
    job_specs,
    ledger_manifest,
)
from _soak.plan import (  # noqa: E402
    COLLECT_FILE,
    LEDGER_FILE,
    PlanError,
    RunPlan,
    load_plan,
    write_json,
)

WORKLOAD: Final = _LAB_DIR / "workloads" / "v06-ledger"
URL_ENV: Final = "MESHFORGE_CLI__CONTROLLER_URL"
DEFAULT_URL: Final = "https://127.0.0.1:47110"
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})
EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_REFUSED: Final = 3
EXIT_UNREACHABLE: Final = 6
EXIT_INTERRUPTED: Final = 130


def _say(text: str) -> None:
    print(text, flush=True)


class TransientError(Exception):
    """No usable answer now (unreachable, timeout, 5xx): retry with the same key."""


class RefusedError(Exception):
    """A 4xx that a retry with the same request cannot fix."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status


class Lab:
    """The controller's REST API as the load generator needs it."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.token = lab_token()
        self.ssl = fabric_ssl_context() if self.url.startswith("https") else None
        self.http = httpx.Client(
            base_url=self.url,
            headers={"authorization": f"Bearer {self.token}"},
            timeout=30.0,
            trust_env=False,
            verify=self.ssl if self.ssl is not None else True,
        )
        self._trees: dict[tuple[str, str, int], tuple[str, dict[str, Any]]] = {}
        self._bundle: str | None = None

    def close(self) -> None:
        self.http.close()

    def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TransientError(type(exc).__name__) from None
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RefusedError(resp.status_code, resp.text)
        return resp

    # ------------------------------------------------------------ uploads
    def ledger_bundle(self) -> str:
        if self._bundle is None:
            from meshforge.common.bundle import pack_to_file

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "v06-ledger.bundle"
                packed = pack_to_file(WORKLOAD, path)
                self.call(
                    "PUT", f"/v1/blobs/{packed.digest.hex}", content=path.read_bytes()
                )
                self._bundle = str(packed.digest)
        return self._bundle

    def family_tree(
        self, family: str, params: dict[str, Any], seed: int
    ) -> tuple[str, dict[str, Any]]:
        """Build the family's job folder, upload it once per (params, seed)."""
        key = (family, json.dumps(params, sort_keys=True), seed)
        if key not in self._trees:
            from pydantic import SecretStr

            from meshforge.bench.workloads import base, build_job
            from meshforge.cli.client import ControllerClient
            from meshforge.cli.transfer import upload_tree
            from meshforge.common.tree import build_tree

            with tempfile.TemporaryDirectory() as tmp:
                saved = base.MIN_RUNTIME_S, base.MAX_RUNTIME_S
                base.MIN_RUNTIME_S, base.MAX_RUNTIME_S = 0.0, float("inf")  # type: ignore[misc]
                try:
                    built = build_job(family, params, seed, Path(tmp) / "job")
                finally:
                    base.MIN_RUNTIME_S, base.MAX_RUNTIME_S = saved  # type: ignore[misc]
                manifest = yaml.safe_load(
                    (built.path / "compute.yaml").read_text("utf-8")
                )
                tree = build_tree(built.path, chunk_size=256 * 1024)
                client = ControllerClient(
                    self.url,
                    timeout_s=120,
                    token=SecretStr(self.token),
                    ssl_context=self.ssl,
                )
                try:
                    upload_tree(client, tree)
                except Exception as exc:  # noqa: BLE001 - any upload failure is transient
                    raise TransientError(f"upload: {type(exc).__name__}") from None
                finally:
                    client.close()
            self._trees[key] = (str(tree.tree.tree_id), manifest)
        return self._trees[key]


def _body(lab: Lab, plan: RunPlan, spec: JobSpec) -> dict[str, Any]:
    placement = {"policy": spec.policy}
    if spec.kind.startswith("m1"):
        return {
            "manifest": ledger_manifest(spec, plan.nonce),
            "bundle_digest": lab.ledger_bundle(),
            "placement": placement,
        }
    if spec.kind == "m3_split":
        family, params = SPLIT_FAMILY, dict(spec.split_params or {})
    else:
        family, params = BENCH_FAMILY, {}
    tree, manifest = lab.family_tree(family, params, spec.seed)
    manifest = {
        **manifest,
        "name": f"soak-{plan.nonce}-{spec.seq:05d}-{spec.kind}",
        "priority": spec.priority,
        "retry": {"max_attempts": MAX_ATTEMPTS, "backoff_s": BACKOFF_S},
        "limits": {**(manifest.get("limits") or {}), "timeout": spec.timeout_s},
    }
    return {"manifest": manifest, "tree_id": tree, "placement": placement}


# ------------------------------------------------------------------ run
class Generator:
    def __init__(
        self,
        lab: Lab,
        plan: RunPlan,
        ledger_path: Path,
        *,
        catchup_s: float,
        retry_for_s: float,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.lab, self.plan, self.path = lab, plan, ledger_path
        self.catchup_s, self.retry_for_s = catchup_s, retry_for_s
        self.now, self.sleep = now, sleep
        self.specs = job_specs(plan)
        self.cancels: list[tuple[datetime, int, str]] = []

    def _log(self, record: dict[str, Any]) -> None:
        jsonl.append(self.path, {"at": iso(self.now()), **record})

    def run(self) -> int:
        loaded = jsonl.load(self.path)
        state = reduce(loaded.records)
        if state.header is None:
            self._log({"type": "header", "schema": LEDGER_SCHEMA, "seed": self.plan.seed,
                       "nonce": self.plan.nonce, "jobs_planned": len(self.specs)})  # fmt: skip
        else:
            _say(f"resuming: {len(state.job_ids())} jobs already submitted")
        refused = 0
        for spec in self.specs:
            entry = state.entries.get(spec.seq)
            if entry is not None and entry.settled:
                self._resume_cancel(spec, entry.job_id, entry.cancel_status)
                continue
            due = self.plan.t0 + timedelta(seconds=spec.offset_s)
            self._wait_until(due)
            if entry is None and (self.now() - due).total_seconds() > self.catchup_s:
                self._log({"type": "submit_failed", "seq": spec.seq, "final": True,
                           "error": "skipped_late"})  # fmt: skip
                continue
            key = idempotency_key(self.plan, spec)
            if entry is None:
                self._log(
                    {"type": "intent", "seq": spec.seq, "kind": spec.kind, "key": key}
                )
            job_id = self._submit(spec, key)
            if job_id is None:
                refused += 1
                continue
            if spec.cancel_after_s is not None:
                self.cancels.append(
                    (
                        self.now() + timedelta(seconds=spec.cancel_after_s),
                        spec.seq,
                        job_id,
                    )
                )
        self._wait_until(self.plan.end_load)
        self._due_cancels(force=True)
        _say(f"load done: {len(self.specs)} planned, {refused} refused")
        return EXIT_REFUSED if refused else EXIT_OK

    def _resume_cancel(
        self, spec: JobSpec, job_id: str | None, status: int | None
    ) -> None:
        if job_id and spec.cancel_after_s is not None and status is None:
            self.cancels.append((self.now(), spec.seq, job_id))

    def _wait_until(self, moment: datetime) -> None:
        while True:
            self._due_cancels()
            left = (moment - self.now()).total_seconds()
            if left <= 0:
                return
            self.sleep(min(left, 1.0))

    def _due_cancels(self, *, force: bool = False) -> None:
        keep: list[tuple[datetime, int, str]] = []
        for when, seq, job_id in self.cancels:
            if not force and when > self.now():
                keep.append((when, seq, job_id))
                continue
            try:
                resp = self.lab.call("POST", f"/v1/jobs/{job_id}/cancel")
                status = resp.status_code
            except RefusedError as exc:
                status = exc.status  # 409: already terminal - fine
            except TransientError:
                keep.append((when + timedelta(seconds=10), seq, job_id))
                continue
            self._log(
                {"type": "cancel", "seq": seq, "job_id": job_id, "status": status}
            )
        self.cancels = keep

    def _submit(self, spec: JobSpec, key: str) -> str | None:
        started = self.now()
        delay = 2.0
        while True:
            try:
                body = _body(self.lab, self.plan, spec)
                resp = self.lab.call(
                    "POST", "/v1/jobs", json=body, headers={"idempotency-key": key}
                )
            except RefusedError as exc:
                self._log({"type": "submit_failed", "seq": spec.seq, "final": True,
                           "status": exc.status, "error": str(exc)})  # fmt: skip
                _say(f"{spec.seq:05d} refused: {exc}")
                return None
            except TransientError as exc:
                waited = (self.now() - started).total_seconds()
                self._log({"type": "submit_failed", "seq": spec.seq, "final": False,
                           "error": str(exc)})  # fmt: skip
                if waited > self.retry_for_s:
                    self._log({"type": "submit_failed", "seq": spec.seq, "final": True,
                               "error": "controller_unavailable"})  # fmt: skip
                    return None
                self._wait_until(self.now() + timedelta(seconds=delay))
                delay = min(delay * 2, 30.0)
                continue
            job_id = str(resp.json()["job_id"])
            self._log({"type": "submitted", "seq": spec.seq, "job_id": job_id,
                       "status": resp.status_code})  # fmt: skip
            replay = " (replay)" if resp.status_code == 200 else ""
            _say(
                f"{iso(self.now())} {spec.seq:05d} {spec.kind:<8} {job_id} {spec.policy}{replay}"
            )
            return job_id


# --------------------------------------------------------------- collect
def _view(lab: Lab, job_id: str) -> dict[str, Any] | None:
    try:
        return dict(lab.call("GET", f"/v1/jobs/{job_id}").json())
    except RefusedError as exc:
        if exc.status == 404:
            return None
        raise


def _check_output(
    lab: Lab, view: dict[str, Any], spec: JobSpec, dest: Path
) -> dict[str, Any]:
    from meshforge.common.bundle import unpack

    digest = str(view.get("output_digest") or "")
    if not digest:
        return {"checked": True, "ok": False, "detail": "no output digest"}
    data = lab.call("GET", f"/v1/blobs/{digest.removeprefix('sha256:')}").content
    archive = dest.with_suffix(".bundle")
    archive.write_bytes(data)
    import hashlib

    if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
        return {"checked": True, "ok": False, "detail": "digest mismatch"}
    unpack(archive, dest)
    if spec.kind.startswith("m1"):
        result = dest / "out" / "result.txt"
        if not result.is_file():
            return {"checked": True, "ok": False, "detail": "out/result.txt missing"}
        ok = result.read_text("utf-8").split()[:1] == [view["job_id"]]
        return {"checked": True, "ok": ok, "detail": "" if ok else "names another job"}
    if spec.kind == "m3_split":
        from meshforge.bench.workloads import get_family

        files = {
            p.relative_to(dest).as_posix(): p.read_bytes()
            for p in sorted(dest.rglob("*"))
            if p.is_file()
        }
        verdict = get_family(SPLIT_FAMILY).verify_output(  # type: ignore[attr-defined]
            files, dict(spec.split_params or {}), spec.seed
        )
        return {
            "checked": True,
            "ok": bool(verdict.ok),
            "detail": "; ".join(verdict.problems)[:300],
        }
    return {"checked": False, "ok": None, "detail": "bench output: presence only"}


def cmd_collect(args: argparse.Namespace, lab: Lab, plan: RunPlan) -> int:
    state = reduce(jsonl.load(args.run_dir / LEDGER_FILE).records)
    ids = state.job_ids()
    deadline = time.monotonic() + args.wait_min * 60.0
    views: dict[str, Any] = {}
    while True:
        pending = [j for j in ids if (views.get(j) or {}).get("state") not in TERMINAL]
        for job_id in pending:
            with contextlib.suppress(TransientError):
                views[job_id] = _view(lab, job_id)
        left = [j for j in ids if (views.get(j) or {}).get("state") not in TERMINAL]
        _say(f"{iso(datetime.now(UTC))} {len(ids) - len(left)}/{len(ids)} terminal")
        if not left or time.monotonic() > deadline:
            break
        time.sleep(15)
    specs = {s.seq: s for s in job_specs(plan)}
    rng = random.Random(f"soak-verify-{plan.seed}")  # noqa: S311 - a reproducible sample
    succeeded = sorted(
        j for j in ids if (views.get(j) or {}).get("state") == "succeeded"
    )
    sample = sorted(rng.sample(succeeded, min(args.verify_sample, len(succeeded))))
    outputs: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for n, job_id in enumerate(sample):
            spec = specs[ids[job_id]]
            try:
                outputs[job_id] = _check_output(
                    lab, views[job_id], spec, Path(tmp) / f"o{n}"
                )
            except (TransientError, RefusedError, OSError, ValueError) as exc:
                outputs[job_id] = {
                    "checked": True,
                    "ok": False,
                    "detail": f"fetch: {exc}",
                }
    nodes: list[Any] = []
    with contextlib.suppress(TransientError, RefusedError):
        nodes = list(lab.call("GET", "/v1/nodes").json().get("nodes") or [])
    doc: dict[str, Any] = {
        "collected_at": iso(datetime.now(UTC)),
        "jobs": {j: views.get(j) for j in ids},
        "nodes": [
            {"node_id": n.get("node_id"), "hostname": n.get("hostname")} for n in nodes
        ],
        "outputs": outputs,
    }
    try:
        write_json(args.run_dir / COLLECT_FILE, doc)
    except OSError as exc:
        _say(f"error: {exc}")
        return EXIT_IO
    bad = sum(1 for o in outputs.values() if o.get("checked") and not o.get("ok"))
    still = sum(
        1 for v in doc["jobs"].values() if (v or {}).get("state") not in TERMINAL
    )
    _say(f"wrote {args.run_dir / COLLECT_FILE}: {len(ids)} jobs, {still} not terminal, "
         f"{len(outputs)} outputs checked ({bad} bad)")  # fmt: skip
    return EXIT_REFUSED if still else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soak_load", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--controller", default=None, help=f"default ${URL_ENV}")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dir", type=Path, required=True, dest="run_dir")
    r.add_argument("--catchup-min", type=float, default=30.0)
    r.add_argument("--retry-min", type=float, default=30.0)
    c = sub.add_parser("collect")
    c.add_argument("--dir", type=Path, required=True, dest="run_dir")
    c.add_argument("--wait-min", type=float, default=60.0)
    c.add_argument("--verify-sample", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_plan(args.run_dir)
    except PlanError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    lab = Lab(args.controller or os.environ.get(URL_ENV) or DEFAULT_URL)
    try:
        if args.command == "collect":
            return cmd_collect(args, lab, plan)
        gen = Generator(
            lab,
            plan,
            args.run_dir / LEDGER_FILE,
            catchup_s=args.catchup_min * 60.0,
            retry_for_s=args.retry_min * 60.0,
        )
        return gen.run()
    except KeyboardInterrupt:
        _say("interrupted - run the same command again to resume")
        return EXIT_INTERRUPTED
    except TransientError as exc:
        _say(f"controller unreachable: {exc}")
        return EXIT_UNREACHABLE
    finally:
        lab.close()


if __name__ == "__main__":
    sys.exit(main())
