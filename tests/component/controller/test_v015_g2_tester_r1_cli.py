"""V0.15 G2 tester round 1: the real child process (``python -m
meshforge.controller.ml.train compare``) and ``compute planner ml compare``
on hostile files - exit codes, no traceback, determinism across Parquet
layouts / hash seeds / thread counts, and no pickle anywhere (brief §5b, D352,
D353, §4)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

import pyarrow as pa
import pyarrow.parquet as pq
from mf_ml_synthetic import parquet_file, synthetic_rows

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

CHILD = [sys.executable, "-m", "meshforge.controller.ml.train", "compare"]
CLI = [sys.executable, "-m", "meshforge.cli", "planner", "ml", "compare"]
FAST = ["--min-rows", "50", "--algorithms", "ridge,tree"]
ROOT = Path(__file__).resolve().parents[3]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cli-config")
    env["MESHFORGE_DATA_DIR"] = str(tmp_path / "no-agent")
    env["COLUMNS"] = "200"
    env.update(extra)
    return env


def _run(
    argv: list[str], tmp_path: Path, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
        env=_env(tmp_path, **extra),
        cwd=str(tmp_path),
    )


def _rows() -> list[dict[str, Any]]:
    return synthetic_rows("signal", families=4, groups_per_family=4, runs_per_group=6)


@pytest.fixture(scope="module")
def good(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return parquet_file(_rows(), tmp_path_factory.mktemp("good") / "dataset.parquet")


def _no_traceback(proc: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stdout, proc.stdout


# ------------------------------------------------------------- the good path


def test_the_child_succeeds_on_a_good_file(good: Path, tmp_path: Path) -> None:
    proc = _run(
        [*CHILD, "--dataset", str(good), "--out", str(tmp_path / "o"), *FAST], tmp_path
    )
    assert proc.returncode == 0, proc.stderr
    printed = json.loads(proc.stdout.strip().splitlines()[-1])
    assert Path(printed["report"]).is_file()


# ------------------------------------------------------------- exit codes


def _write_with_metadata(src: Path, dst: Path, **meta: bytes) -> Path:
    table = pq.read_table(src)
    schema_meta = dict(table.schema.metadata or {})
    for key, value in meta.items():
        schema_meta[key.encode()] = value
    pq.write_table(table.replace_schema_metadata(schema_meta), dst)
    return dst


def _meta_keys(src: Path) -> list[str]:
    return [k.decode() for k in (pq.read_table(src).schema.metadata or {})]


def test_unknown_dataset_and_feature_versions_exit_2(
    good: Path, tmp_path: Path
) -> None:
    keys = [k for k in _meta_keys(good) if "version" in k]
    assert len(keys) >= 2, keys
    for key in keys:
        bad = _write_with_metadata(good, tmp_path / f"{key}.parquet", **{key: b"99"})
        proc = _run(
            [*CHILD, "--dataset", str(bad), "--out", str(tmp_path / "o"), *FAST],
            tmp_path,
        )
        assert proc.returncode == 2, (key, proc.stderr)
        _no_traceback(proc)
        assert proc.stderr.strip()
        assert not (tmp_path / "o" / "report.json").exists()


def test_a_foreign_parquet_file_exits_2_not_a_traceback(tmp_path: Path) -> None:
    path = tmp_path / "foreign.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), path)
    proc = _run(
        [*CHILD, "--dataset", str(path), "--out", str(tmp_path / "o")], tmp_path
    )
    assert proc.returncode == 2
    _no_traceback(proc)


def test_a_non_version_garbled_version_value_exits_2(
    good: Path, tmp_path: Path
) -> None:
    key = next(k for k in _meta_keys(good) if "dataset" in k and "version" in k)
    bad = _write_with_metadata(good, tmp_path / "g.parquet", **{key: b"not-a-number"})
    proc = _run([*CHILD, "--dataset", str(bad), "--out", str(tmp_path / "o")], tmp_path)
    assert proc.returncode == 2
    _no_traceback(proc)


@pytest.mark.parametrize(
    "kind", ["empty", "garbage", "truncated", "directory", "missing", "header_only"]
)
def test_unreadable_files_exit_1_with_a_one_line_message(
    good: Path, tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "bad.parquet"
    if kind == "empty":
        path.write_bytes(b"")
    elif kind == "garbage":
        path.write_bytes(os.urandom(4096))
    elif kind == "truncated":
        data = good.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    elif kind == "directory":
        path.mkdir()
    elif kind == "header_only":
        path.write_bytes(b"PAR1")
    proc = _run(
        [*CHILD, "--dataset", str(path), "--out", str(tmp_path / "o"), *FAST], tmp_path
    )
    # a directory is read by pyarrow as an (empty) dataset: no version -> 2 is fine
    assert proc.returncode == (2 if kind == "directory" else 1), (kind, proc.stderr)
    _no_traceback(proc)
    assert 0 < len(proc.stderr.strip().splitlines()) <= 3
    assert not (tmp_path / "o" / "report.json").exists()


@pytest.mark.parametrize(
    "flags",
    [
        ["--terms", "nope"],
        ["--algorithms", "deepnet"],
        ["--min-rows", "abc"],
        ["--terms", "compute,nope"],
    ],
)
def test_bad_arguments_exit_2(good: Path, tmp_path: Path, flags: list[str]) -> None:
    proc = _run(
        [*CHILD, "--dataset", str(good), "--out", str(tmp_path / "o"), *flags], tmp_path
    )
    assert proc.returncode == 2, proc.stderr
    _no_traceback(proc)


def test_a_zero_or_negative_floor_is_clamped_not_a_crash(
    good: Path, tmp_path: Path
) -> None:
    for value in ("0", "-5"):
        proc = _run(
            [
                *CHILD,
                "--dataset",
                str(good),
                "--out",
                str(tmp_path / f"o{value}"),
                "--min-rows",
                value,
                "--min-families",
                value,
                "--algorithms",
                "ridge",
                "--terms",
                "compute",
            ],
            tmp_path,
        )
        assert proc.returncode in (0, 2), proc.stderr
        _no_traceback(proc)


def test_missing_extra_exits_2_with_the_hint_never_a_traceback(
    good: Path, tmp_path: Path
) -> None:
    code = (
        "import sys\n"
        "for m in ('sklearn','lightgbm','xgboost'): sys.modules[m] = None\n"
        "from meshforge.controller.ml.train.__main__ import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )
    proc = _run(
        [
            sys.executable,
            "-c",
            code,
            "compare",
            "--dataset",
            str(good),
            "--out",
            str(tmp_path / "o"),
        ],
        tmp_path,
    )
    assert proc.returncode == 2, proc.stderr
    _no_traceback(proc)
    assert "uv sync" in proc.stderr or "extra" in proc.stderr
    assert not (tmp_path / "o").exists()


def test_an_unwritable_out_is_exit_1_not_a_traceback(
    good: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    proc = _run(
        [*CHILD, "--dataset", str(good), "--out", str(blocker / "sub"), *FAST], tmp_path
    )
    assert proc.returncode == 1, proc.stderr
    _no_traceback(proc)


_T1_D4 = pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1-D4 (major): a schema-valid file with a NaN/inf y or an inf "
    "feature (corrupt/hand-edited data) crashes the child with a raw Python "
    "traceback (exit 1): __main__.py:136 calls compare() outside any try, and "
    "scikit-learn's ValueError ('Input y contains NaN') escapes from "
    "algorithms.py:259-274. Expected: a one-line message, exit 1 (or 2).",
)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("nan_y", marks=_T1_D4),
        pytest.param("inf_y", marks=_T1_D4),
        "null_y",
        pytest.param("inf_x", marks=_T1_D4),
        "null_x",
    ],
)
def test_corrupt_numbers_in_a_valid_file_never_a_traceback(
    tmp_path: Path, bad: str
) -> None:
    rows = _rows()
    r = rows[5]
    if bad == "nan_y":
        r["y"] = float("nan")
    elif bad == "inf_y":
        r["y"] = float("inf")
    elif bad == "null_y":
        r["y"] = None
    elif bad == "inf_x":
        r["load_cpu_percent"] = float("inf")
    elif bad == "null_x":
        r["load_cpu_percent"] = None
    from meshforge.controller.ml.dataset import parquet_bytes, sort_key

    path = tmp_path / "corrupt.parquet"
    try:
        path.write_bytes(parquet_bytes(sorted(rows, key=sort_key)))
    except Exception:  # noqa: BLE001 - the writer itself refuses: fine
        pytest.skip("the G1 writer refuses this value")
    proc = _run(
        [
            *CHILD,
            "--dataset",
            str(path),
            "--out",
            str(tmp_path / "o"),
            *FAST,
            "--terms",
            "compute",
        ],
        tmp_path,
    )
    _no_traceback(proc)
    assert proc.returncode in (0, 1, 2), proc.stderr
    if proc.returncode == 0:
        text = (tmp_path / "o" / "report.json").read_text()
        assert "NaN" not in text and "Infinity" not in text


# ------------------------------------------------------------- compute planner ml compare


def test_the_cli_maps_child_failures_to_its_own_exit_codes(
    good: Path, tmp_path: Path
) -> None:
    missing = _run([*CLI, "--dataset", str(tmp_path / "nope.parquet")], tmp_path)
    assert missing.returncode == 2
    _no_traceback(missing)
    empty = tmp_path / "empty.parquet"
    empty.write_bytes(b"")
    proc = _run([*CLI, "--dataset", str(empty)], tmp_path)
    assert proc.returncode == 1
    _no_traceback(proc)
    key = next(k for k in _meta_keys(good) if "feature" in k and "version" in k)
    bad = _write_with_metadata(good, tmp_path / "v.parquet", **{key: b"77"})
    proc = _run([*CLI, "--dataset", str(bad)], tmp_path)
    assert proc.returncode == 2
    _no_traceback(proc)
    ok = _run([*CLI, "--dataset", str(good), *FAST, "--terms", "compute"], tmp_path)
    assert ok.returncode == 0, ok.stderr
    assert "compute" in ok.stdout


def test_a_unicode_and_spaced_path_works(tmp_path: Path) -> None:
    folder = tmp_path / "dätä sét ü 数据"
    folder.mkdir()
    path = parquet_file(_rows(), folder / "ds ü.parquet")
    out = folder / "out dir"
    proc = _run([*CHILD, "--dataset", str(path), "--out", str(out), *FAST], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (out / "report.json").is_file()


# ------------------------------------------------------------- determinism


def _report(dataset: Path, out: Path, tmp_path: Path, **env: str) -> bytes:
    proc = _run(
        [*CHILD, "--dataset", str(dataset), "--out", str(out), *FAST], tmp_path, **env
    )
    assert proc.returncode == 0, proc.stderr
    return (out / "report.json").read_bytes()


def test_report_bytes_do_not_depend_on_parquet_layout_hash_seed_or_threads(
    good: Path, tmp_path: Path
) -> None:
    base = _report(good, tmp_path / "a", tmp_path)
    table = pq.read_table(good)
    layouts: dict[str, dict[str, Any]] = {
        "rg7": {"row_group_size": 7},
        "rg1": {"row_group_size": 1},
        "snappy_v2": {
            "row_group_size": 50,
            "compression": "snappy",
            "data_page_version": "2.0",
        },
        "none": {"compression": "NONE", "use_dictionary": False},
    }
    for name, opts in layouts.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path, **opts)
        assert _report(path, tmp_path / name, tmp_path) == base, name
    assert _report(good, tmp_path / "h1", tmp_path, PYTHONHASHSEED="1") == base
    assert _report(good, tmp_path / "h2", tmp_path, PYTHONHASHSEED="777") == base
    threads = {
        "OMP_NUM_THREADS": "4",
        "OPENBLAS_NUM_THREADS": "4",
        "MKL_NUM_THREADS": "4",
    }
    assert _report(good, tmp_path / "t4", tmp_path, **threads) == base


def test_a_reordered_copy_with_dropped_metadata_order_gives_the_same_bytes(
    good: Path, tmp_path: Path
) -> None:
    base = _report(good, tmp_path / "a", tmp_path)
    shuffled = parquet_file(_rows(), tmp_path / "s.parquet", shuffle_seed=3)
    assert _report(shuffled, tmp_path / "s", tmp_path) == base


# ------------------------------------------------------------- no pickle anywhere

_FORBIDDEN = re.compile(
    r"\b(pickle|cPickle|joblib|dill|cloudpickle|marshal|shelve)\b|allow_pickle|"
    r"torch\.load|\.pkl\b|\.joblib\b",
    re.IGNORECASE,
)


def test_no_pickle_or_joblib_under_the_ml_package() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src" / "meshforge" / "controller" / "ml").rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _FORBIDDEN.search(code) and "no pickle" not in line.lower():
                offenders.append(f"{path.relative_to(ROOT)}:{no}: {line.strip()}")
    assert not offenders, offenders


def test_a_run_writes_only_json_and_markdown(good: Path, tmp_path: Path) -> None:
    out = tmp_path / "o"
    _report(good, out, tmp_path)
    assert sorted(p.name for p in out.iterdir()) == [
        "report.json",
        "report.md",
        "timings.json",
    ]
    assert not [
        p for p in tmp_path.rglob("*") if p.suffix in (".pkl", ".joblib", ".pickle")
    ]
