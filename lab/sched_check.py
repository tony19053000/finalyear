# ruff: noqa: T201 - a lab tool: its output is the point
"""V0.6 lab kit (roadmap 0.6.13): submit the mixed job set, wait for it, and
check the ledger. The procedure is ``lab/checklists/V0.6-scheduling.md``.

Runs on the **controller laptop** (L1) after ``compute login`` (V0.7 G4: the
token is the one ``compute`` uses) and with ``$MESHFORGE_CLI__CONTROLLER_URL``.

    uv run python lab/sched_check.py submit --n 100 --seed 6 --out RUN.json
        Upload lab/workloads/v06-ledger and submit N mixed jobs (sleep 5-60 s,
        some CPU work, ~10 % exit 3, retry max 5, the policies cost_model /
        least_loaded / random / cache_affinity, priorities -2..2). Writes the
        plan (job ids + intent) to RUN.json. ``--sleep A-B`` sets the range;
        ``--fail-pct``, ``--no-fail``.
    uv run python lab/sched_check.py wait RUN.json [--timeout 3600]
        Poll until every job is terminal; one progress line per 10 s.
    uv run python lab/sched_check.py check RUN.json --ledger DIR
            [--partitioned NODE_ID ...] [--out REPORT.json]
        DIR holds every laptop's ledger file (<host>.txt, copied from
        C:\\mf-lab\\v06-ledger or /tmp/mf-lab/v06-ledger). Prints the verdict
        (lab/_sched/analysis.py) and writes it to REPORT.json.

Exit codes: 0 done (check: criterion 1 PASS); 2 usage error; 3 check FAIL or
wait timed out; 4 check INCONCLUSIVE (a committed job has no ledger evidence -
a laptop's ledger is missing; collect it and check again - or the run file
lists no jobs; never a pass, D132). Identical ledger lines (one file copied
under two names) count once.
6 the controller could not be reached.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Final

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sched.analysis import FAIL, PASS, Planned, analyse, parse_ledger

WORKLOAD: Final = Path(__file__).resolve().parent / "workloads" / "v06-ledger"
POLICIES: Final = (
    "cost_model",
    "cost_model",
    "least_loaded",
    "random",
    "cache_affinity",
)
MAX_ATTEMPTS: Final = 5
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})
EXIT_FAIL: Final = 3
EXIT_INCONCLUSIVE: Final = 4


def fabric_ssl_context() -> Any:
    """V0.7 G3: trust the fabric CA as ``compute`` does (see _fabric/api.py)."""
    lab_dir = str(Path(__file__).resolve().parent)
    if lab_dir not in sys.path:
        sys.path.insert(0, lab_dir)
    from _fabric.api import fabric_ssl_context as make

    return make()


def _token() -> str:
    """V0.7 G4: the token ``compute login`` stored (or ``MESHFORGE_CLI__TOKEN``)."""
    lab_dir = str(Path(__file__).resolve().parent)
    if lab_dir not in sys.path:
        sys.path.insert(0, lab_dir)
    from _fabric.api import lab_token

    return lab_token()


def _client() -> httpx.Client:
    url = os.environ.get("MESHFORGE_CLI__CONTROLLER_URL", "https://127.0.0.1:47110")
    return httpx.Client(
        base_url=url.rstrip("/"),
        headers={"authorization": f"Bearer {_token()}"},
        timeout=30.0,
        trust_env=False,
        verify=fabric_ssl_context(),
    )


def _upload(http: httpx.Client) -> str:
    from meshforge.common.bundle import pack_to_file

    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "v06-ledger.bundle"
        packed = pack_to_file(WORKLOAD, bundle)
        resp = http.put(f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes())
        if resp.status_code not in (200, 201):
            sys.exit(f"upload failed: HTTP {resp.status_code} {resp.text[:200]}")
        return str(packed.digest)


def _range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def cmd_submit(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)  # noqa: S311 - a reproducible job mix
    low, high = _range(args.sleep)
    plan: list[dict[str, Any]] = []
    with _client() as http:
        digest = _upload(http)
        for i in range(args.n):
            exit_code = 3 if (not args.no_fail and rng.random() < args.fail_pct) else 0
            sleep_s = round(rng.uniform(low, high), 1)
            work_s = round(rng.choice((0.0, 0.0, 2.0, 5.0)), 1)
            policy = rng.choice(POLICIES)
            priority = rng.randint(-2, 2)
            manifest = {
                "name": f"v06-lab-{i:03d}",
                "entrypoint": [
                    "python",
                    "main.py",
                    str(sleep_s),
                    str(work_s),
                    str(exit_code),
                ],
                "outputs": ["out/*"],
                "priority": priority,
                "retry": {"max_attempts": MAX_ATTEMPTS, "backoff_s": 2},
                "limits": {"timeout": 1800},
            }
            key = f"v06-lab-{args.seed}-{i}-{uuid.uuid4().hex[:8]}"
            resp = http.post(
                "/v1/jobs",
                json={
                    "manifest": manifest,
                    "bundle_digest": digest,
                    "placement": {"policy": policy},
                },
                headers={"idempotency-key": key},
            )
            if resp.status_code not in (200, 201):
                print(f"job {i}: HTTP {resp.status_code} {resp.text[:300]}")
                return 3
            job_id = str(resp.json()["job_id"])
            plan.append(
                {
                    "job_id": job_id,
                    "exit_code": exit_code,
                    "sleep_s": sleep_s,
                    "work_s": work_s,
                    "policy": policy,
                    "priority": priority,
                    "max_attempts": MAX_ATTEMPTS,
                    "submitted_at": time.time(),
                }
            )
            print(
                f"{i:03d} {job_id} sleep={sleep_s}s work={work_s}s exit={exit_code} {policy}"
            )
    Path(args.out).write_text(json.dumps({"seed": args.seed, "jobs": plan}, indent=1))
    print(f"wrote {args.out} ({len(plan)} jobs)")
    return 0


def _views(http: httpx.Client, job_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    out: dict[str, dict[str, Any] | None] = {}
    for job_id in job_ids:
        try:
            resp = http.get(f"/v1/jobs/{job_id}")
        except httpx.HTTPError:
            out[job_id] = None
            continue
        out[job_id] = dict(resp.json()) if resp.status_code == 200 else None
    return out


def _load(path: str) -> list[dict[str, Any]]:
    return list(json.loads(Path(path).read_text())["jobs"])


def cmd_wait(args: argparse.Namespace) -> int:
    jobs = _load(args.run)
    ids = [j["job_id"] for j in jobs]
    deadline = time.monotonic() + args.timeout
    with _client() as http:
        while True:
            try:
                views = _views(http, ids)
            except httpx.HTTPError:
                views = {}
            states: dict[str, int] = {}
            for view in views.values():
                state = "unreachable" if view is None else str(view.get("state"))
                states[state] = states.get(state, 0) + 1
            print(
                time.strftime("%H:%M:%S"),
                json.dumps(states, sort_keys=True),
                flush=True,
            )
            if views and all(
                v is not None and v.get("state") in TERMINAL for v in views.values()
            ):
                return 0
            if time.monotonic() > deadline:
                print("timed out: not every job is terminal")
                return 3
            time.sleep(10)


def cmd_check(args: argparse.Namespace) -> int:
    jobs = _load(args.run)
    planned = [
        Planned(
            job_id=j["job_id"],
            exit_code=int(j["exit_code"]),
            max_attempts=int(j.get("max_attempts", MAX_ATTEMPTS)),
            pinned=bool(j.get("pinned", False)),
        )
        for j in jobs
    ]
    lines: list[str] = []
    files = sorted(Path(args.ledger).glob("*.txt"))
    if not files:
        print(f"no ledger files (*.txt) in {args.ledger}")
        return 2
    for path in files:
        lines.extend(path.read_text(errors="replace").splitlines())
    with _client() as http:
        views = _views(http, [p.job_id for p in planned])
    verdict = analyse(
        planned, views, parse_ledger(lines), partitioned_nodes=args.partitioned or ()
    )
    report = {
        "run": args.run,
        "ledger_files": [p.name for p in files],
        "partitioned_nodes": args.partitioned or [],
        **verdict.as_dict(),
    }
    text = json.dumps(report, indent=1, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    outcome = verdict.criterion_1_text
    if outcome == PASS:
        return 0
    if outcome == FAIL:
        return EXIT_FAIL
    if verdict.jobs == 0:
        print(
            f"INCONCLUSIVE: {args.run} lists no jobs - nothing was checked; "
            "pass the RUN.json that `submit` wrote",
            file=sys.stderr,
        )
        return EXIT_INCONCLUSIVE
    print(
        f"INCONCLUSIVE: {len(verdict.committed_without_evidence)} committed job(s) "
        f"have no `end` line in {args.ledger} - a laptop's ledger is missing; "
        "collect every ledger file and run check again",
        file=sys.stderr,
    )
    return EXIT_INCONCLUSIVE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sched_check", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="submit the mixed job set")
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--seed", type=int, default=6)
    s.add_argument("--sleep", default="5-60", help="sleep range in seconds, A-B")
    s.add_argument("--fail-pct", type=float, default=0.1)
    s.add_argument("--no-fail", action="store_true")
    s.add_argument("--out", required=True)
    w = sub.add_parser("wait", help="wait until every job is terminal")
    w.add_argument("run")
    w.add_argument("--timeout", type=float, default=3600)
    c = sub.add_parser("check", help="the verdict (criteria 1/2)")
    c.add_argument("run")
    c.add_argument("--ledger", required=True)
    c.add_argument("--partitioned", nargs="*", default=[])
    c.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        return {"submit": cmd_submit, "wait": cmd_wait, "check": cmd_check}[args.cmd](
            args
        )
    except httpx.ConnectError as exc:
        print(f"controller unreachable: {exc}")
        return 6


if __name__ == "__main__":
    sys.exit(main())
