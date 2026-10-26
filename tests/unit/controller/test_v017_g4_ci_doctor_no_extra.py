"""CI fix (run 40280010136, fresh-clone): ``meshforge-controller doctor`` must
report, not crash, when the controller extra is missing.

The dashboard check imported :mod:`meshforge.controller.web.spa` for its dist
path, and that module imports starlette: without the extra, ``doctor --json``
died with ``ModuleNotFoundError`` and printed nothing. The doctor is the tool
that explains a broken install, so it runs here with starlette (and fastapi)
made unimportable, in a child process so this session is untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_CHILD = """
import json, sys
for name in ("starlette", "fastapi", "uvicorn"):
    sys.modules[name] = None  # import -> ModuleNotFoundError, as without the extra
from typer.testing import CliRunner
from meshforge.controller.app import app
r = CliRunner().invoke(app, ["doctor", "--config", sys.argv[1], "--json"])
if r.exception is not None and not isinstance(r.exception, SystemExit):
    raise r.exception
print(json.dumps({"exit": r.exit_code, "output": r.output}))
"""


def test_doctor_json_works_without_the_controller_extra(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f"data_dir = {str(tmp_path / 'state')!r}\n"
        "[controller.db]\n"
        'mode = "managed"\n'
        f"bin_dir = {str(tmp_path / 'nope')!r}\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-c", _CHILD, str(cfg)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["exit"] == 1, result
    body = json.loads(result["output"])
    dashboard = next(c for c in body["checks"] if c["name"] == "dashboard")
    assert dashboard["status"] in ("ok", "fail")
