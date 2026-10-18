# ruff: noqa: E501  (long xfail reasons / parametrize ids are clearer on one line)
"""V0.9 G2 tester round 1 (split from ``..._process.py``, G3 item 0.2):
``compute bench workloads [--json]`` as a user runs it, in real processes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from mf_bench_procs import (  # tests/_support, shared by the split files
    FAMILIES,
    _env,
)

from meshforge.bench.workloads import (
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    get_family,
)

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]


# ------------------------------------------------------------------ the CLI

_CLI = "import sys; from meshforge.cli.app import main; sys.argv = ['compute'] + sys.argv[1:]; main()"


def _cli(*args: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _CLI, *args],
        cwd=cwd,
        env=_env(**env),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


FAMILY_KEYS = {
    "description",
    "env_base",
    "gpu",
    "kind",
    "name",
    "params_schema",
    "runtime_window_s",
    "size_params",
    "smallest",
    "smallest_hint_s",
}


def test_cli_json_is_stable_valid_and_independent_of_terminal_locale_and_cwd(
    tmp_path: Path,
) -> None:
    (tmp_path / "x").mkdir()
    runs = [
        _cli("bench", "workloads", "--json", cwd=tmp_path, PYTHONHASHSEED="0"),
        _cli(
            "bench",
            "workloads",
            "--json",
            cwd=tmp_path / "x",
            PYTHONHASHSEED="5",
            COLUMNS="20",
        ),
        _cli(
            "bench",
            "workloads",
            "--json",
            cwd=tmp_path,
            LC_ALL="C",
            PYTHONIOENCODING="ascii",
            NO_COLOR="1",
            TERM="dumb",
        ),
        _cli(
            "bench", "workloads", "--json", cwd=tmp_path, HOME=str(tmp_path / "nohome")
        ),
    ]
    for r in runs:
        assert r.returncode == 0, r.stderr
        assert r.stderr.strip() == "", r.stderr
    assert len({r.stdout for r in runs}) == 1
    doc = json.loads(runs[0].stdout)
    assert set(doc) == {
        "schema_version",
        "count",
        "families",
        "runtime_window_s",
        "reference_hw",
    }
    assert doc["schema_version"] == 1 and doc["count"] == 15 == len(doc["families"])
    assert doc["runtime_window_s"] == [MIN_RUNTIME_S, MAX_RUNTIME_S]
    assert set(doc["reference_hw"]) == {
        "cpu_fp32_gflops_1t",
        "cpu_fp32_gflops_all",
        "cpu_fp64_gflops_all",
        "mem_triad_gb_s",
        "gpu_fp32_tflops",
        "gpu_fp16_tflops",
        "gpu_mem_gb_s",
    }
    names = [f["name"] for f in doc["families"]]
    assert names == sorted(names) == sorted(f.name for f in FAMILIES)
    for entry in doc["families"]:
        family = get_family(entry["name"])
        assert set(entry) == FAMILY_KEYS
        assert entry["gpu"] in ("required", "none")
        assert entry["kind"] in ("cpu", "memory", "gpu", "ml")
        assert entry["env_base"] == ("cuda-pytorch" if family.gpu else "python-cpu")
        assert entry["smallest"] == family.params_dict(family.smallest_params())
        assert entry["smallest_hint_s"] == round(
            family.expected_runtime_s(family.smallest_params()), 1
        )
        assert MIN_RUNTIME_S <= entry["smallest_hint_s"] <= MAX_RUNTIME_S
        schema = entry["params_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["properties"]) == set(entry["smallest"])
        for pname, spec in schema["properties"].items():
            assert spec["default"] == entry["smallest"][pname]
            assert ("minimum" in spec and "maximum" in spec) or "enum" in spec, (
                entry["name"],
                pname,
            )
        assert set(entry["size_params"]) <= set(schema["properties"])


def test_cli_text_table_lists_every_family_and_never_needs_a_controller(
    tmp_path: Path,
) -> None:
    done = _cli(
        "bench", "workloads", cwd=tmp_path, COLUMNS="220", HOME=str(tmp_path / "nohome")
    )
    assert done.returncode == 0, done.stderr
    for family in FAMILIES:
        assert family.name in done.stdout
    assert "15 families" in done.stdout
    narrow = _cli("bench", "workloads", cwd=tmp_path, COLUMNS="40")
    assert narrow.returncode == 0, narrow.stderr


def test_cli_misuse_exits_2_with_usage_not_a_traceback(tmp_path: Path) -> None:
    for args in (
        ["bench", "workloads", "--nope"],
        ["bench", "workloads", "extra"],
        ["bench", "nothing"],
    ):
        done = _cli(*args, cwd=tmp_path)
        assert done.returncode == 2, (args, done.stdout, done.stderr)
        assert "Traceback" not in done.stderr
