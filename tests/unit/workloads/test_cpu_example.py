"""V0.4 G5 0.4.14: the cpu-deterministic example gives the same bytes everywhere.

The golden digest is checked on Linux here and on Windows in CI: if it ever
differs across OSes, criterion 1's "identical outputs" claim is wrong.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

EXAMPLE = Path(__file__).resolve().parents[3] / "workloads/examples/cpu-deterministic"
GOLDEN_SHA256 = "d85fca4c255d086a52b98aa22bc5aaa1ec00eb62f27b40cd1c0986e29b01c598"
"""sha256 of results/result.json for the manifest's arguments (size 200000, seed
1234), measured on Linux (Python 3.12)."""


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cpu_example", EXAMPLE / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_run_matches_the_golden_digest(tmp_path: Path) -> None:
    main = _module().main
    assert main(["--size", "200000", "--seed", "1234", "--out", str(tmp_path)]) == 0
    data = (tmp_path / "result.json").read_bytes()
    assert b"\r\n" not in data
    assert hashlib.sha256(data).hexdigest() == GOLDEN_SHA256


def test_numpy_and_pure_python_give_the_same_bytes(tmp_path: Path) -> None:
    pytest.importorskip("numpy", reason="the numpy path needs numpy")
    main = _module().main
    main(["--size", "20000", "--out", str(tmp_path / "np")])
    main(["--size", "20000", "--out", str(tmp_path / "py"), "--no-numpy"])
    assert (tmp_path / "np/result.json").read_bytes() == (
        tmp_path / "py/result.json"
    ).read_bytes()


def test_a_different_seed_changes_the_result(tmp_path: Path) -> None:
    main = _module().main
    main(["--size", "5000", "--seed", "1", "--out", str(tmp_path / "a")])
    main(["--size", "5000", "--seed", "2", "--out", str(tmp_path / "b")])
    assert (tmp_path / "a/result.json").read_bytes() != (
        tmp_path / "b/result.json"
    ).read_bytes()
