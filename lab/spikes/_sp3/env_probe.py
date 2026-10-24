"""SP3 (d): one real env build through the agent's EnvBuilder, run by the bundle.

Executed by the *bundled* interpreter (``<bundle>/python/bin/python3.12 env_probe.py
ROOT SUPPORT``), never imported by the harness. ``SUPPORT`` is
``tests/_support`` (for the offline wheelhouse helpers). Builds:

* ``ensure_venv`` (venv runtime): needs ``uv python find`` to locate a Python
  3.12 - which one it finds depends on ``PATH``; the result names it;
* ``ensure_target`` (subprocess runtime): installs for ``sys.executable``, i.e.
  the bundled interpreter, with no lookup at all.

Both use the default ``uv`` argv (``uv.find_uv_bin()``), i.e. the bundle's own uv,
and install from a hand-made wheelhouse with ``--no-index`` (no network). Prints
one JSON document.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REQ = b"mfdemo==1.0\n"
IMPORT = "import mfdemo, sys; print(mfdemo.VERSION, sys.executable)"


def _run(argv: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    done = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=env, check=False
    )
    return {
        "exit": done.returncode,
        "out": done.stdout.strip(),
        "err": done.stderr[-500:],
    }


async def main(root: Path, support: Path) -> dict[str, Any]:
    sys.path.insert(0, str(support))
    from mf_envs_support import make_wheel, offline_builder

    from meshforge.agent.envs import EnvError

    wheels = root / "wheels"
    make_wheel(wheels, "mfdemo", "1.0")
    builder = offline_builder(root / "envs", wheels)
    result: dict[str, Any] = {
        "sys_executable": sys.executable,
        "uv_argv": list(builder.uv_argv),
        "PATH": os.environ.get("PATH", ""),
    }
    try:
        interpreter = await builder.find_interpreter(None)
        result["venv_interpreter"] = str(interpreter.path)
        started = time.monotonic()
        venv = await builder.ensure_venv(python=None, requirements=REQ, deadline=1e18)
        result["venv"] = {
            "cache": venv.cache,
            "seconds": round(time.monotonic() - started, 3),
            "run": _run([str(venv.python), "-I", "-c", IMPORT]),
        }
    except EnvError as exc:
        result["venv_error"] = {"reason": exc.reason, "message": str(exc)}
    try:
        started = time.monotonic()
        target = await builder.ensure_target(requirements=REQ, deadline=1e18)
        result["target"] = {
            "cache": target.cache,
            "seconds": round(time.monotonic() - started, 3),
            "run": _run(
                [sys.executable, "-c", IMPORT],
                env={**os.environ, "PYTHONPATH": str(target.path)},
            ),
        }
    except EnvError as exc:
        result["target_error"] = {"reason": exc.reason, "message": str(exc)}
    return result


if __name__ == "__main__":
    result = asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
    sys.stdout.write(json.dumps(result) + "\n")
