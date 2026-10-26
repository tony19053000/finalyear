# ruff: noqa: T201 - a lab tool: its output is the point
"""V1.0 lab kit (roadmap 1.0.2, D411): run the soak's seeded chaos schedule.

Runs on the **controller laptop**. The schedule is ``DIR/chaos-schedule.json``
(written by ``soak.py plan``, catalogue and command templates in
``lab/_soak/chaos.py``); every action is logged to ``DIR/chaos-log.jsonl``.

    uv run --extra controller python lab/chaos.py run --dir DIR
            [--manual prompt|skip] [--late-min 10] [--control-dir PATH]
        Wait for each action's time and do it: Linux laptops over SSH
        (``executor: ssh``; key-based login + a NOPASSWD sudoers rule, see the
        checklist) or on this laptop (``local``); cancel a running Mode 3 job
        through the REST API; ask a person for the manual ones (revoke +
        re-join). Windows rows are logged ``delegated`` - ``lab/chaos_win.ps1``
        on that laptop does them from the same schedule file. Killed: run the
        same command again; finished actions are skipped, an action that was
        running when the driver died is logged ``interrupted``, one more than
        --late-min late is logged ``missed``.
    uv run python lab/chaos.py show --dir DIR
        The schedule with each action's logged outcome.

Exit codes: 0 done; 1 cannot write the log; 2 usage error; 3 done but an
action failed (see the log); 130 interrupted (resumable).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _soak import jsonl  # noqa: E402
from _soak.chaos import (  # noqa: E402
    Action,
    TemplateError,
    end_record,
    finished_ids,
    render_command,
    ssh_argv,
    started_ids,
)
from _soak.plan import (  # noqa: E402
    SCHEDULE_FILE,
    PlanError,
    RunPlan,
    Target,
    load_plan,
    read_json,
)
from _soak.stats import parse_ts  # noqa: E402

LOG_FILE: Final = "chaos-log.jsonl"
COMMAND_TIMEOUT_S: Final = 120.0
HARNESS_ACK_TIMEOUT_S: Final = 180.0
EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_ACTION_FAILED: Final = 3
EXIT_INTERRUPTED: Final = 130


def _say(text: str) -> None:
    print(text, flush=True)


def run_argv(argv: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout_s:g} s"
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()[-400:]


Outcome = tuple[str, int | None, str, str | None]
"""(outcome, exit code, detail, job id)."""


class Driver:
    def __init__(
        self,
        plan: RunPlan,
        actions: list[Action],
        log_path: Path,
        *,
        manual: str,
        late_s: float,
        control_dir: Path | None,
        runner: Callable[[list[str], float], tuple[int, str]] = run_argv,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        canceller: Callable[[Action], Outcome] | None = None,
    ) -> None:
        self.plan, self.actions, self.log_path = plan, actions, log_path
        self.manual, self.late_s, self.control_dir = manual, late_s, control_dir
        self.runner, self.now, self.sleep = runner, now, sleep
        self.canceller = canceller
        self.failed = 0

    def _log(self, record: dict[str, Any]) -> None:
        jsonl.append(self.log_path, record)

    def run(self) -> int:
        records = jsonl.load(self.log_path).records
        done, started = finished_ids(records), started_ids(records)
        for action in self.actions:
            if action.id in done:
                continue
            at = parse_ts(action.at)
            assert at is not None  # noqa: S101 - schedule written by soak.py plan
            if action.id in started:
                now = self.now()
                self._log(
                    end_record(
                        action,
                        started=now,
                        ended=now,
                        outcome="interrupted",
                        executor=action.executor,
                        detail="the driver stopped while this action ran",
                    )
                )
                continue
            while (left := (at - self.now()).total_seconds()) > 0:
                self.sleep(min(left, 5.0))
            if (self.now() - at).total_seconds() > self.late_s:
                now = self.now()
                self._log(end_record(action, started=now, ended=now, outcome="missed",
                                     executor=action.executor,
                                     detail="the driver was not running at its time"))  # fmt: skip
                continue
            self.do(action)
        return EXIT_ACTION_FAILED if self.failed else EXIT_OK

    def do(self, action: Action) -> None:
        target = self.plan.target(action.target)
        started = self.now()
        self._log({"type": "start", "id": action.id, "kind": action.kind,
                   "target": action.target, "at": started.isoformat()})  # fmt: skip
        _say(f"{started.isoformat()} {action.id} {action.kind} on {action.target} "
             f"({action.executor}, {action.duration_s:g} s)")  # fmt: skip
        outcome, code, detail, job_id = self._execute(action, target)
        if outcome == "failed":
            self.failed += 1
        self._log(end_record(action, started=started, ended=self.now(), outcome=outcome,
                             executor=action.executor, exit_code=code, detail=detail,
                             job_id=job_id))  # fmt: skip
        _say(f"  -> {outcome} {detail[:120]}")

    def _execute(self, action: Action, target: Target) -> Outcome:
        executor = action.executor
        if executor == "windows":
            return "delegated", None, "run by chaos_win.ps1 on the laptop", None
        if executor == "api":
            if self.canceller is None:
                return "failed", None, "no controller client", None
            return self.canceller(action)
        if executor == "manual":
            return self._manual(action, target)
        if executor == "harness":
            return self._harness(action)
        try:
            command = render_command(action, target)
        except TemplateError as exc:
            return "failed", None, str(exc), None
        argv = ssh_argv(target, command) if executor == "ssh" else ["sh", "-c", command]
        code, out = self.runner(argv, COMMAND_TIMEOUT_S)
        if action.kind == "reboot" and code in (0, 255):
            return "done", code, out, None  # 255: the SSH session died with the laptop
        return ("done" if code == 0 else "failed"), code, out, None

    def _manual(self, action: Action, target: Target) -> Outcome:
        if self.manual == "skip":
            return "skipped", None, "manual action skipped (--manual skip)", None
        _say(f"\n>>> MANUAL {action.kind} on {target.label} ({target.hostname}): see "
             "lab/checklists/V1.0-soak.md 'Manual chaos actions'. Press Enter when "
             "done, or type 'skip' + Enter.")  # fmt: skip
        answer = sys.stdin.readline().strip().lower()
        if answer == "skip":
            return "skipped", None, "operator skipped", None
        return "done", None, "operator confirmed", None

    def _harness(self, action: Action) -> Outcome:
        if self.control_dir is None:
            return "failed", None, "executor harness needs --control-dir", None
        request = self.control_dir / f"{action.id}.request.json"
        ack = self.control_dir / f"{action.id}.done.json"
        tmp = request.with_suffix(".tmp")
        tmp.write_text(json.dumps(action.as_dict()), "utf-8")
        os.replace(tmp, request)
        deadline = time.monotonic() + HARNESS_ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            if ack.exists():
                doc = json.loads(ack.read_text("utf-8"))
                return (str(doc.get("outcome", "failed")), None, str(doc.get("detail", "")),
                        doc.get("job_id"))  # fmt: skip
            time.sleep(0.2)
        return "failed", None, "the harness did not answer", None


def make_canceller(seed: int, wait_s: float) -> Callable[[Action], Outcome]:
    """``cancel_mode3``: cancel one running split job (seeded choice)."""
    from soak_load import DEFAULT_URL, URL_ENV, Lab, RefusedError, TransientError

    rng = random.Random(f"soak-cancel-{seed}")  # noqa: S311 - reproducible choice
    lab = Lab(os.environ.get(URL_ENV) or DEFAULT_URL)

    def cancel(action: Action) -> Outcome:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                jobs = lab.call(
                    "GET", "/v1/jobs", params={"state": "running", "limit": "100"}
                )
                ids = sorted(str(j["job_id"]) for j in jobs.json().get("jobs") or [])
                rng.shuffle(ids)
                for job_id in ids:
                    view = lab.call("GET", f"/v1/jobs/{job_id}").json()
                    if "split" in (view.get("manifest") or {}):
                        resp = lab.call("POST", f"/v1/jobs/{job_id}/cancel")
                        return "done", resp.status_code, f"cancelled {job_id}", job_id
            except (TransientError, RefusedError) as exc:
                if time.monotonic() > deadline:
                    return "failed", None, f"controller: {exc}", None
            if time.monotonic() > deadline:
                return "skipped", None, "no running Mode 3 job to cancel", None
            time.sleep(10)

    return cancel


def _load(run_dir: Path) -> tuple[RunPlan, list[Action]]:
    plan = load_plan(run_dir)
    doc = read_json(run_dir / SCHEDULE_FILE)
    if doc.get("nonce") != plan.nonce:
        raise PlanError("chaos-schedule.json belongs to another plan (nonce differs)")
    return plan, [Action.from_dict(a) for a in doc.get("actions") or []]


def cmd_show(run_dir: Path) -> int:
    _plan, actions = _load(run_dir)
    outcomes: dict[str, str] = {}
    for path in sorted(run_dir.glob("chaos-log*.jsonl")):
        for r in jsonl.load(path).records:
            if r.get("type") == "end" and outcomes.get(str(r["id"])) in (
                None,
                "delegated",
            ):
                outcomes[str(r["id"])] = str(r.get("outcome"))
    for a in actions:
        print(f"{a.id} {a.at} {a.kind:<17} {a.target:<6} {a.executor:<8} "
              f"{outcomes.get(a.id, 'pending')}")  # fmt: skip
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chaos", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dir", type=Path, required=True, dest="run_dir")
    r.add_argument("--manual", choices=("prompt", "skip"), default="prompt")
    r.add_argument("--late-min", type=float, default=10.0)
    r.add_argument("--control-dir", type=Path, default=None)
    r.add_argument("--cancel-wait-min", type=float, default=5.0)
    s = sub.add_parser("show")
    s.add_argument("--dir", type=Path, required=True, dest="run_dir")
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            return cmd_show(args.run_dir)
        plan, actions = _load(args.run_dir)
    except PlanError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    needs_api = any(a.executor == "api" for a in actions)
    driver = Driver(
        plan,
        actions,
        args.run_dir / LOG_FILE,
        manual=args.manual,
        late_s=args.late_min * 60.0,
        control_dir=args.control_dir,
        canceller=make_canceller(plan.seed, args.cancel_wait_min * 60.0)
        if needs_api
        else None,
    )
    try:
        return driver.run()
    except KeyboardInterrupt:
        _say("interrupted - run the same command again to resume")
        return EXIT_INTERRUPTED
    except OSError as exc:
        _say(f"error: {exc}")
        return EXIT_IO


if __name__ == "__main__":
    sys.exit(main())
