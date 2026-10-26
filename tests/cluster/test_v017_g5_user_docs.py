"""V0.17 G5 (0.17.7, D396/Q40): the user pages' examples run, exactly as written,
against a real fabric (controller + 2 agents as processes), so they cannot rot.

* ``docs/user/run-a-workload.md``: every ``<!-- ci:file project=P path=F -->``
  block is written to ``P/F``; every ``compute ...`` line of a
  ``<!-- ci:commands project=P -->`` block is run with the real ``compute``
  CLI; the outputs are what the page says (Mode 1 ``hello``; the Mode 3 split
  ``squares``: one ``squares.txt``, 20 lines in order);
* ``docs/user/sdk-quickstart.md``: the ``<!-- ci:run-python -->`` program is run
  as a script with ``compute`` on ``PATH`` and logged in (the page's "before
  you start"); it must print ``total = 5050``.

The markers are HTML comments: invisible on the rendered page.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

DOCS: Final = Path(__file__).resolve().parents[2] / "docs" / "user"
TIMING: Final = Timing(
    name="user-docs",
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
_BLOCK: Final = re.compile(
    r"<!-- ci:(?P<kind>[a-z-]+)(?P<attrs>[^>]*?) -->\n```[a-z]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def blocks(page: Path) -> list[tuple[str, dict[str, str], str]]:
    """(kind, attributes, body) of every marked code block on ``page``."""
    found = []
    for m in _BLOCK.finditer(page.read_text(encoding="utf-8")):
        attrs = dict(a.split("=", 1) for a in m.group("attrs").split())
        found.append((m.group("kind"), attrs, m.group("body") + "\n"))
    return found


def _env(fabric: Fabric, local_data: Path, home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(local_data),
            "XDG_CONFIG_HOME": str(home / "cli-config"),
            # `compute` as installed: the entry point next to this interpreter
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), env["PATH"]]),
            "NO_COLOR": "1",
            "COLUMNS": "200",
        }
    )
    return env


@pytest.fixture
def fabric(make_fabric: Callable[..., Fabric]) -> Fabric:
    return make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def test_the_markers_are_found() -> None:
    kinds = [kind for kind, _, _ in blocks(DOCS / "run-a-workload.md")]
    assert kinds.count("file") == 4 and kinds.count("commands") == 2
    assert [k for k, _, _ in blocks(DOCS / "sdk-quickstart.md")] == ["run-python"]


def test_run_a_workload_examples_work(fabric: Fabric, tmp_path: Path) -> None:
    env = _env(fabric, fabric.nodes[0].data_dir, tmp_path)
    results: dict[str, Path] = {}
    for kind, attrs, body in blocks(DOCS / "run-a-workload.md"):
        project = tmp_path / attrs["project"]
        if kind == "file":
            target = project / attrs["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            continue
        for line in body.splitlines():
            if not line.startswith("compute "):
                continue
            argv = shlex.split(line)[1:]
            done = subprocess.run(
                [sys.executable, "-m", "meshforge.cli", *argv],
                env=env, cwd=tmp_path, capture_output=True, text=True,
                timeout=300, check=False,
            )  # fmt: skip
            assert done.returncode == 0, (
                line,
                done.stdout[-2000:],
                done.stderr[-2000:],
            )
            job_id = done.stdout.splitlines()[0].strip()
            assert job_id.startswith("job_"), done.stdout[:500]
            dest = tmp_path / "outputs" / attrs["project"]
            got = subprocess.run(
                [sys.executable, "-m", "meshforge.cli", "get", job_id, "--dest", str(dest)],
                env=env, cwd=tmp_path, capture_output=True, text=True,
                timeout=120, check=False,
            )  # fmt: skip
            assert got.returncode == 0, got.stderr[-2000:]
            results[attrs["project"]] = dest
    assert set(results) == {"hello", "squares"}
    where = (results["hello"] / "results" / "where.txt").read_text()
    assert where.startswith("hello from ")
    squares = (results["squares"] / "squares.txt").read_text().splitlines()
    assert squares == [f"{n} {n * n}" for n in range(20)]


def test_sdk_quickstart_program_works(fabric: Fabric, tmp_path: Path) -> None:
    ((kind, _, program),) = blocks(DOCS / "sdk-quickstart.md")
    assert kind == "run-python"
    script = tmp_path / "sum_on_fabric.py"
    script.write_text(program, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(script)],
        env=_env(fabric, fabric.nodes[0].data_dir, tmp_path), cwd=tmp_path,
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-2000:])
    assert "total = 5050" in done.stdout
    assert re.search(r"job job_\w+ is succeeded", done.stdout), done.stdout
