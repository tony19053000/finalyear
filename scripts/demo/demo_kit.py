"""Shared pieces of the V1.0 demo scenarios (roadmap 1.0.7, D420).

Everything a scenario does goes through the real ``compute`` CLI - the same
commands the audience sees printed - against whichever fabric ``compute`` is
logged in to (``compute login``, or the ``MESHFORGE_CLI__*`` variables). The
demo itself never fakes a node, a job or a result: Mode 2 and Mode 4 have no
scenario at all (ADR-0012), only the "deferred, designed, not built" note.

* :class:`Compute` - run one ``compute`` command, show it, parse ``--json``;
* :class:`Show` - the presenter's view: steps, commands, expectations,
  pauses (live) or none (``--dry-run``), a PASS/FAIL verdict line;
* :class:`DemoState` - what a scenario created (job ids, invite ids, killed
  nodes) in ``<work>/state.json``, so ``--reset`` can undo exactly that;
* project builders (:func:`write_job_project`, :func:`write_split_project`)
  and :func:`expected_split_lines`, the independent check of a split output.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})
JOB_PREFIX: Final = "demo-"
"""Every demo job's name starts with this (``--reset`` cancels them)."""
MODE_OF: Final = {"local": "local", "offload": "mode1", "split": "mode3"}
"""Stored candidate mode (``compute explain --json``) -> the mode's name."""


class DemoError(Exception):
    """An expectation did not hold (exit 1), or a command failed."""


# ------------------------------------------------------------------ output
@dataclass
class Show:
    """What the presenter (and a CI log) sees."""

    dry_run: bool
    out: Any = field(default_factory=lambda: sys.stdout)
    started: float = field(default_factory=time.monotonic)
    failures: list[str] = field(default_factory=list)

    def line(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def step(self, title: str) -> None:
        self.line()
        self.line(f"== {title}  [{self.elapsed():5.1f} s]")

    def say(self, text: str) -> None:
        self.line(f"   {text}")

    def pause(self, prompt: str = "press Enter to continue") -> None:
        """Wait for the presenter (never in a dry run or without a terminal)."""
        if self.dry_run or not sys.stdin.isatty():
            return
        input(f"   ... {prompt} ")

    def expect(self, ok: bool, what: str, detail: object = "") -> None:
        """One checked expectation: ``EXPECTED`` or ``UNEXPECTED`` (recorded)."""
        if ok:
            self.say(f"EXPECTED   {what}")
            return
        message = f"{what} - got {detail}" if detail != "" else what
        self.say(f"UNEXPECTED {message}")
        self.failures.append(message)

    def elapsed(self) -> float:
        return time.monotonic() - self.started


# ----------------------------------------------------------------- compute
@dataclass
class Compute:
    """The real ``compute`` CLI as a subprocess (``argv0`` = how to start it)."""

    show: Show
    argv0: Sequence[str] = (sys.executable, "-m", "meshforge.cli")
    cwd: Path | None = None
    timeout_s: float = 300.0

    def run(self, *args: str, quiet: bool = False, check: bool = True) -> str:
        """Run ``compute ARGS``; print it (unless ``quiet``); return stdout."""
        if not quiet:
            self.show.say(f"$ compute {shlex.join(args)}")
        env = {
            **os.environ,
            "NO_COLOR": "1",
            "COLUMNS": os.environ.get("COLUMNS", "120"),
        }
        try:
            done = subprocess.run(
                [*self.argv0, *args], cwd=self.cwd, env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=self.timeout_s, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise DemoError(
                f"compute {args[0]} did not finish in {self.timeout_s:.0f} s"
            ) from exc
        if check and done.returncode != 0:
            tail = (done.stderr or done.stdout).strip().splitlines()[-5:]
            raise DemoError(
                f"compute {shlex.join(args)} exited {done.returncode}: "
                + " | ".join(tail)
            )
        if not quiet and done.stdout.strip():
            for text in done.stdout.rstrip().splitlines()[:40]:
                self.show.say(f"  {text}")
        return done.stdout

    def json(self, *args: str, quiet: bool = True) -> Any:
        text = self.run(*args, "--json", quiet=quiet)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DemoError(
                f"compute {args[0]} --json printed no JSON: {text[:200]!r}"
            ) from exc

    # ---- the reads every scenario needs
    def submit(self, project: Path, *extra: str) -> str:
        out = self.run("run", str(project), *extra)
        job_id = out.strip().splitlines()[0].strip() if out.strip() else ""
        if not job_id.startswith("job_"):
            raise DemoError(f"compute run printed no job id: {out[:200]!r}")
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        found = self.json("jobs", job_id)
        return dict(found.get("job", found))

    def nodes(self) -> list[dict[str, Any]]:
        return list(self.json("nodes").get("nodes", []))

    def decision(self, job_id: str) -> dict[str, Any] | None:
        """The job's newest dispatched planner decision, or None."""
        decisions = self.json("explain", job_id).get("decisions") or []
        dispatched = [d for d in decisions if d.get("outcome") == "dispatched"]
        return dispatched[-1] if dispatched else None

    def split(self, job_id: str) -> dict[str, Any]:
        return dict(self.json("watch", job_id))


def chosen(decision: Mapping[str, Any]) -> dict[str, Any] | None:
    """The chosen candidate row of a decision (rank 1 on the chosen node)."""
    for row in decision.get("candidates") or ():
        if row.get("rank") == 1 and row.get("node_id") == decision.get(
            "chosen_node_id"
        ):
            return dict(row)
    return None


def chosen_mode(decision: Mapping[str, Any] | None) -> str | None:
    """``local`` / ``mode1`` / ``mode3`` of a decision's choice."""
    row = chosen(decision) if decision else None
    return MODE_OF.get(str(row.get("mode"))) if row else None


def wait_for[T](
    what: str, probe: Callable[[], T | None], *, within_s: float, every_s: float = 0.5
) -> T:
    """Poll ``probe`` until it returns a value (not None) or the time is up."""
    deadline = time.monotonic() + within_s
    while True:
        got = probe()
        if got is not None:
            return got
        if time.monotonic() >= deadline:
            raise DemoError(f"timed out after {within_s:.0f} s waiting for {what}")
        time.sleep(every_s)


def wait_job(compute: Compute, job_id: str, *, within_s: float) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        job = compute.job(job_id)
        return job if job.get("state") in TERMINAL else None

    return wait_for(f"job {job_id} to end", probe, within_s=within_s)


# ------------------------------------------------------------------- state
@dataclass
class DemoState:
    """``<work>/state.json``: what this scenario created, for ``--reset``."""

    path: Path
    jobs: list[str] = field(default_factory=list)
    invites: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> DemoState:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path)
        return cls(
            path,
            [str(j) for j in data.get("jobs", [])],
            [str(i) for i in data.get("invites", [])],
            [str(n) for n in data.get("killed", [])],
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"jobs": self.jobs, "invites": self.invites, "killed": self.killed}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def add_job(self, job_id: str) -> None:
        self.jobs.append(job_id)
        self.save()


def run_hook(
    show: Show, template: str, *, stdin: str | None = None, **values: str
) -> None:
    """Run a presenter-supplied command (``--kill-cmd`` ...): split like a
    shell line, ``{placeholders}`` filled in, never through a shell."""
    try:
        argv = [part.format(**values) for part in shlex.split(template)]
    except (KeyError, IndexError, ValueError) as exc:
        raise DemoError(f"bad hook command {template!r}: {exc}") from exc
    if not argv:
        raise DemoError("empty hook command")
    show.say(
        f"$ {shlex.join(argv) if stdin is None else shlex.join(argv) + ' < (code)'}"
    )
    try:
        done = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DemoError(f"hook {argv[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise DemoError(
            f"hook {argv[0]} exited {done.returncode}: {done.stderr.strip()[-300:]}"
        )


def reset_fabric_objects(compute: Compute, state: DemoState) -> None:
    """Cancel every non-terminal demo job; revoke unused demo invites."""
    listed = compute.json("jobs", "--limit", "200").get("jobs", [])
    open_jobs = {
        str(j["job_id"])
        for j in listed
        if j.get("state") not in TERMINAL
        and str(j.get("name", "")).startswith(JOB_PREFIX)
    }
    for job_id in state.jobs:
        if job_id not in open_jobs and compute.job(job_id).get("state") not in TERMINAL:
            open_jobs.add(job_id)
    for job_id in sorted(open_jobs):
        compute.run("cancel", job_id)
    if state.invites:
        invites = compute.json("fabric", "invites").get("invites", [])
        for row in invites:
            if row.get("id") in state.invites and row.get("state") == "pending":
                compute.run("fabric", "invites", "--revoke", str(row["id"]))


# ---------------------------------------------------------------- projects
_JOB_PY: Final = '''"""MeshForge demo job: a deterministic CPU computation (scripts/demo).

Usage: python job.py ROUNDS [--hold-s S]

Writes results/result.json: a SHA-256 chain of ROUNDS rounds - the same bytes on
every laptop. With --hold-s the job keeps computing (more chain rounds) for at
least S seconds and also records how many extra rounds it did (a "busy" job).
"""

import hashlib
import json
import sys
import time
from pathlib import Path

rounds = int(sys.argv[1])
hold_s = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[2] == "--hold-s" else 0.0
started = time.monotonic()
digest = b"meshforge-demo"
for _ in range(rounds):
    digest = hashlib.sha256(digest).digest()
result = {"rounds": rounds, "sha256": digest.hex()}
extra = 0
while time.monotonic() - started < hold_s:
    for _ in range(10_000):
        digest = hashlib.sha256(digest).digest()
    extra += 10_000
if hold_s:
    result["extra_rounds"] = extra
out = Path("results")
out.mkdir(exist_ok=True)
(out / "result.json").write_text(json.dumps(result, sort_keys=True) + "\\n", newline="\\n")
print(f"done: {rounds} rounds -> {digest.hex()[:16]}... in {time.monotonic() - started:.1f} s")
'''

_ITEM_PY: Final = '''"""MeshForge demo split piece (scripts/demo): one line per item.

Usage: python piece.py ITEMS_FILE OUT_DIR ROUNDS

For each item n of this piece: "n <hex>", hex = a SHA-256 chain of ROUNDS rounds
started from the text "item-n". Pieces are joined in order (combine: concat).
"""

import hashlib
import json
import sys
from pathlib import Path

items = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
rounds = int(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
lines = []
for n in items:
    digest = f"item-{n}".encode()
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    lines.append(f"{n} {digest.hex()}\\n")
(out / "hashes.txt").write_text("".join(lines), newline="\\n")
'''


def write_job_project(
    dest: Path,
    *,
    name: str,
    rounds: int,
    hold_s: float = 0.0,
    objective: str | None = None,
) -> Path:
    """A whole-job project (01, 02, 04): ``job.py`` + its ``compute.yaml``."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / "job.py").write_text(_JOB_PY, encoding="utf-8", newline="\n")
    argv = ["python", "job.py", str(rounds)]
    if hold_s:
        argv += ["--hold-s", f"{hold_s:g}"]
    lines = [
        f"name: {name}",
        f"entrypoint: {json.dumps(argv)}",
        "outputs: [results/]",
        "limits:",
        "  timeout: 10m",
    ]
    if objective:
        lines.append(f"objective: {objective}")
    (dest / "compute.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    return dest


def write_split_project(
    dest: Path, *, name: str, items: int, chunk: int, rounds: int
) -> Path:
    """A Mode 3 project (03, 04): ``piece.py`` over the range ``0..items-1``."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / "piece.py").write_text(_ITEM_PY, encoding="utf-8", newline="\n")
    argv = ["python", "piece.py", "{items_file}", "{out}", str(rounds)]
    text = "\n".join(
        [
            f"name: {name}",
            f"entrypoint: {json.dumps(argv)}",
            "split:",
            f"  over: {{range: {{stop: {items}}}}}",
            f"  chunk: {{items: {chunk}}}",
            "  combine: concat",
            "  min_nodes: 2",
            "limits:",
            "  timeout: 10m",
        ]
    )
    (dest / "compute.yaml").write_text(text + "\n", encoding="utf-8", newline="\n")
    return dest


def expected_job_result(rounds: int) -> str:
    """``result.json`` of a whole demo job without ``--hold-s``."""
    digest = b"meshforge-demo"
    for _ in range(rounds):
        digest = hashlib.sha256(digest).digest()
    return json.dumps({"rounds": rounds, "sha256": digest.hex()}, sort_keys=True) + "\n"


def expected_split_lines(items: int, rounds: int) -> list[str]:
    """The combined ``hashes.txt`` of a split demo job, computed here."""
    lines = []
    for n in range(items):
        digest = f"item-{n}".encode()
        for _ in range(rounds):
            digest = hashlib.sha256(digest).digest()
        lines.append(f"{n} {digest.hex()}")
    return lines


def fetch_outputs(compute: Compute, job_id: str, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    compute.run("get", job_id, "--dest", str(dest))
    return dest


def last_node(job: Mapping[str, Any]) -> str | None:
    """The node of the job's newest whole-job attempt."""
    attempts = [a for a in job.get("attempts") or () if a.get("role", "run") == "run"]
    return str(attempts[-1].get("node_id")) if attempts else None


def node_label(nodes: Sequence[Mapping[str, Any]], node_id: str | None) -> str:
    for node in nodes:
        if node.get("node_id") == node_id:
            name = node.get("display_name") or node.get("hostname") or ""
            return f"{name} ({node_id})" if name else str(node_id)
    return str(node_id)
