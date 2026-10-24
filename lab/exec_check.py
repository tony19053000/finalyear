# ruff: noqa: T201 - a lab tool: its output is the point
"""V0.4 lab kit (roadmap 0.4.15): measure what the checklist needs to measure.

Runs on the **controller laptop** (L1) after ``compute login`` (V0.7 G4: the
token is the one ``compute`` uses). The procedure is ``lab/checklists/V0.4-remote-exec.md``.

    uv run python lab/exec_check.py latency JOB_ID --name L2
        Follow the job's live log over the WebSocket; for every ``tick <i> <t>``
        line (lab/workloads/log-ticker) print receive-time minus print-time.
        Reports p50/p95/max. Criterion 2: max <= 1 s.
    uv run python lab/exec_check.py cancel JOB_ID --name L2
        Cancel a running job and time cancel request -> ``cancelled``.
        Criterion 3: <= 5 s.

Both append one JSON line to ``lab/reports/raw/<day>-V0.4-exec-check.jsonl``.
Wall clocks: ``latency`` compares two laptops' clocks - synchronise them first
(checklist A4) and give the offset you measured with ``--clock-offset-ms``
(positive = the remote laptop's clock is ahead); it is subtracted and recorded.

Exit codes: 0 done and within the bound; 2 usage error; 3 done but over the
bound (or too few lines); 6 the controller could not be reached.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Final

import httpx

REPORTS: Final = Path(__file__).resolve().parent / "reports" / "raw"
LATENCY_BOUND_S: Final = 1.0
CANCEL_BOUND_S: Final = 5.0


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


def _url(args: argparse.Namespace) -> str:
    return (
        args.controller
        or os.environ.get("MESHFORGE_CLI__CONTROLLER_URL")
        or "https://127.0.0.1:47110"
    ).rstrip("/")


def _record(entry: dict[str, Any], reports: Path) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.now(dt.UTC).date().isoformat()
    with (reports / f"{day}-V0.4-exec-check.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def latency(args: argparse.Namespace) -> int:
    from websockets.sync.client import connect

    base = _url(args)
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/v1/jobs/{args.job}/logs/ws?stream=stdout"
    offset_s = args.clock_offset_ms / 1000.0
    samples: list[float] = []
    pending = b""
    try:
        with connect(
            url, additional_headers={"Authorization": f"Bearer {_token()}"}
        ) as ws:
            for raw in ws:
                received = time.time()
                frame = json.loads(raw)
                if frame.get("end"):
                    break
                if frame.get("retry"):
                    pending = b""  # the job was retried: the next attempt's frames
                    continue
                if "data" not in frame:
                    continue
                pending += base64.b64decode(frame["data"])
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    parts = line.decode(errors="replace").split()
                    if len(parts) == 3 and parts[0] == "tick":
                        samples.append(received - (float(parts[2]) + offset_s))
    except OSError as exc:
        print(f"cannot reach the controller: {exc}", file=sys.stderr)
        return 6
    if len(samples) < 50:
        print(f"only {len(samples)} tick lines received (need >= 50)", file=sys.stderr)
        return 3
    ordered = sorted(samples)
    result = {
        "check": "latency",
        "name": args.name,
        "job_id": args.job,
        "lines": len(ordered),
        "p50_ms": round(statistics.median(ordered) * 1000, 1),
        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))] * 1000, 1),
        "max_ms": round(ordered[-1] * 1000, 1),
        "min_ms": round(ordered[0] * 1000, 1),
        "clock_offset_ms": args.clock_offset_ms,
        "bound_ms": LATENCY_BOUND_S * 1000,
    }
    result["within"] = ordered[-1] <= LATENCY_BOUND_S
    _record(result, args.reports)
    print(json.dumps(result, indent=2))
    if result["min_ms"] < -50:
        print("NOTE: negative latencies - the clocks are not in sync (checklist A4)")
    return 0 if result["within"] else 3


def cancel(args: argparse.Namespace) -> int:
    base = _url(args)
    headers = {"authorization": f"Bearer {_token()}"}
    try:
        with httpx.Client(
            base_url=base, headers=headers, timeout=10, verify=fabric_ssl_context()
        ) as client:
            started = time.monotonic()
            resp = client.post(f"/v1/jobs/{args.job}/cancel")
            if resp.status_code >= 400:
                print(
                    f"cancel refused: {resp.status_code} {resp.text[:200]}",
                    file=sys.stderr,
                )
                return 3
            state = ""
            while time.monotonic() - started < 30:
                state = client.get(f"/v1/jobs/{args.job}").json()["state"]
                if state in ("cancelled", "failed", "succeeded"):
                    break
                time.sleep(0.05)
            took = time.monotonic() - started
    except httpx.HTTPError as exc:
        print(f"cannot reach the controller: {type(exc).__name__}", file=sys.stderr)
        return 6
    result = {
        "check": "cancel",
        "name": args.name,
        "job_id": args.job,
        "state": state,
        "seconds": round(took, 3),
        "bound_s": CANCEL_BOUND_S,
        "within": state == "cancelled" and took <= CANCEL_BOUND_S,
    }
    _record(result, args.reports)
    print(json.dumps(result, indent=2))
    return 0 if result["within"] else 3


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.4 lab measurements (0.4.15)")
    parser.add_argument("--controller", help="REST URL, e.g. http://192.168.1.20:47110")
    parser.add_argument(
        "--reports", type=Path, default=REPORTS, help="where the JSONL result goes"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lat = sub.add_parser("latency", help="criterion 2: live log latency")
    lat.add_argument("job")
    lat.add_argument("--name", required=True, help="the laptop that ran it, e.g. L2")
    lat.add_argument("--clock-offset-ms", type=float, default=0.0)
    lat.set_defaults(func=latency)
    can = sub.add_parser("cancel", help="criterion 3: cancel latency")
    can.add_argument("job")
    can.add_argument("--name", required=True)
    can.set_defaults(func=cancel)
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
