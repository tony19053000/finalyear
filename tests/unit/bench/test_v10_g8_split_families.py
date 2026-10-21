"""V0.10 G8 (0.10.9, D272): the four Mode 3 benchmark families.

* every family: deterministic data and job folder from (params, seed); a job
  folder ``compute run`` accepts (``split:`` spec, the D260 placeholders, the
  built-in combine); its runtime hint inside the 5 s - 3 min window;
* self-checks: the whole-output ``verify_output`` accepts the real output and
  refuses a changed, missing, reordered or extra one; the per-chunk check
  (job side) refuses a wrong chunk output;
* **chunking independence** (criterion 1's mechanism): running the items in
  any chunking and joining the parts like the built-in combine gives exactly
  the one-chunk output;
* the job-side ``main`` (``run_chunk.py``): exit 0 + files, exit 2 on a bad
  request, exit 4 on a failed self-check.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy", reason="the image / monte carlo kernels need numpy")

import numpy as np

from meshforge.bench.workloads import get_family
from meshforge.bench.workloads.base import (
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    ParamsError,
    list_families,
    list_split_families,
)
from meshforge.bench.workloads.kernels import split as kernel
from meshforge.bench.workloads.materialise import SPLIT_ENTRYPOINT, job_files
from meshforge.bench.workloads.split import MAX_FILE_BYTES, SplitFamily
from meshforge.common.manifest import parse_manifest_data

pytestmark = pytest.mark.unit

SEED = 11
SMALL: Mapping[str, dict[str, Any]] = {
    "split.image_classify": {"images": 24, "size": 16, "width": 4},
    "split.hash_files": {"files": 8, "size_kb": 4, "rounds": 2},
    "split.montecarlo_range": {"items": 16, "samples": 20_000},
    "split.csv_aggregate": {"rows": 120, "cols": 4, "rounds": 3},
}
NAMES = sorted(SMALL)


def _family(name: str) -> SplitFamily[Any]:
    fam = get_family(name)
    assert isinstance(fam, SplitFamily)
    return fam


def _params(name: str) -> Any:
    # small points (below the 5 s window) for speed: the model, not parse_params
    return _family(name)._model(SMALL[name])


@pytest.fixture
def in_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    def write(name: str) -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in _family(name).input_files(_params(name), SEED).items():
            target = root.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        monkeypatch.chdir(root)
        return root

    return write


def _items(name: str) -> bytes:
    return _family(name).items_file(_params(name), SEED)


def _chunks(name: str, size: int) -> list[bytes]:
    """The family's items split into chunks of ``size``, as a lane's items
    files look (JSON lists; ``lines``: the header + the chunk's records)."""
    data = _items(name)
    if name == "split.csv_aggregate":
        header, *records = data.decode().splitlines(keepends=True)
        return [
            (header + "".join(records[i : i + size])).encode()
            for i in range(0, len(records), size)
        ]
    items = json.loads(data)
    return [
        json.dumps(items[i : i + size]).encode() for i in range(0, len(items), size)
    ]


def _combine(name: str, parts: list[dict[str, bytes]]) -> dict[str, bytes]:
    """The built-in combine's result (concat / merge-csv / merge-json)."""
    fam = _family(name)
    out_name = fam.output_file
    blobs = [p[out_name] for p in parts]
    if fam.combine == "concat":
        return {out_name: b"".join(blobs)}
    if fam.combine == "merge-csv":
        header = blobs[0].splitlines(keepends=True)[0]
        body = b"".join(b"".join(b.splitlines(keepends=True)[1:]) for b in blobs)
        return {out_name: header + body}
    merged: list[Any] = []
    for blob in blobs:
        merged.extend(json.loads(blob))
    return {out_name: (json.dumps(merged, separators=(",", ":")) + "\n").encode()}


# --------------------------------------------------------------- registry


def test_the_four_families_are_registered_apart_from_the_whole_run_ones() -> None:
    assert [f.name for f in list_split_families()] == NAMES
    assert not any(f.split for f in list_families())  # V0.9 listing unchanged
    for name in NAMES:
        fam = _family(name)
        assert fam.split and not fam.gpu and fam.env_base == "python-cpu"
        assert name in kernel.SPLIT_KERNELS
        assert fam.combine in ("concat", "merge-csv", "merge-json")


@pytest.mark.parametrize("name", NAMES)
def test_defaults_are_a_valid_run_inside_the_window(name: str) -> None:
    fam = _family(name)
    params = fam.parse_params({})
    hint = fam.expected_runtime_s(params)
    assert MIN_RUNTIME_S <= hint <= MAX_RUNTIME_S
    # size params grow the hint
    for size in fam.size_params:
        field = fam.params_model.model_fields[size]
        if field.annotation is int:
            try:
                bigger = fam._model(
                    {**fam.params_dict(params), size: getattr(params, size) + 1}
                )
            except ParamsError:
                continue  # already at its maximum
            assert fam.expected_runtime_s(bigger) >= hint


def test_criterion_one_workload_is_the_3000_image_default() -> None:
    params = _family("split.image_classify").parse_params({})
    assert params.images == 3000


@pytest.mark.parametrize("name", NAMES)
def test_inputs_are_bounded(name: str) -> None:
    fam = _family(name)
    big = {
        "split.image_classify": {"images": 20_000},
        "split.hash_files": {"files": 4096, "size_kb": 1024},
        "split.montecarlo_range": {"items": 100_000},
        "split.csv_aggregate": {"rows": 100_000, "cols": 32},
    }[name]
    problems = fam.combination_problems(fam._model(big))
    if name in ("split.montecarlo_range", "split.image_classify"):
        assert problems == []  # no data / at most ~62 MB: inside the bound
    else:
        assert problems and "more than" in problems[0]
    for rel, data in fam.input_files(_params(name), SEED).items():
        assert rel.startswith("data/") and len(data) <= MAX_FILE_BYTES


# --------------------------------------------------------------- job folder


@pytest.mark.parametrize("name", NAMES)
def test_job_folder_is_a_split_job_and_byte_identical(name: str) -> None:
    fam = _family(name)
    smallest = {"images": 1500} if name == "split.image_classify" else {}
    params = fam.parse_params(smallest)
    one = job_files(fam, params, SEED)
    two = job_files(fam, params, SEED)
    assert one == two
    assert "run_chunk.py" in one and "bench_kernels/split.py" in one
    assert "run_workload.py" not in one
    import yaml

    manifest = parse_manifest_data(yaml.safe_load(one["compute.yaml"]))
    assert manifest.split is not None
    assert tuple(manifest.entrypoint) == SPLIT_ENTRYPOINT
    combine = manifest.split.combine
    assert combine.builtin == fam.combine
    other = job_files(fam, params, SEED + 1)
    data_one = {k: v for k, v in one.items() if k.startswith("data/")}
    data_two = {k: v for k, v in other.items() if k.startswith("data/")}
    if data_one:
        assert data_one.keys() == data_two.keys() and data_one != data_two


def test_kernels_are_standard_library_at_import() -> None:
    source = Path(kernel.__file__).read_text()
    assert "import numpy" not in source.split("def require_numpy")[0]
    assert (
        "meshforge"
        not in source.replace("meshforge.split|", "")
        .replace("meshforge.image_classify", "")
        .split('"""', 2)[2]
    )


# --------------------------------------------------------------- self-checks


@pytest.mark.parametrize("name", NAMES)
def test_whole_output_check_accepts_the_real_output_and_refuses_changes(
    name: str, in_tree: Callable[[str], Path]
) -> None:
    fam = _family(name)
    in_tree(name)
    files, problem, ns = kernel.run_items(
        name, fam.params_dict(_params(name)), SEED, _items(name)
    )
    assert problem is None and ns > 0
    assert set(files) == {fam.output_file}
    assert fam.verify_output(files, _params(name), SEED).ok
    data = files[fam.output_file]
    lines = data.splitlines(keepends=True)
    changed = {
        "flipped": data.replace(b"1", b"2", 1),
        "dropped": b"".join(lines[:-1]) if len(lines) > 1 else data[:-2],
        "extra": data + data[-20:],
    }
    if name == "split.montecarlo_range":
        rows = json.loads(data)
        changed["reordered"] = json.dumps([rows[1], rows[0], *rows[2:]]).encode()
        changed["flipped"] = json.dumps([[r[0], r[1] + 1] for r in rows]).encode()
    for label, bad in changed.items():
        verdict = fam.verify_output({fam.output_file: bad}, _params(name), SEED)
        assert not verdict.ok, label
    assert not fam.verify_output({}, _params(name), SEED).ok
    assert not fam.verify_output({**files, "x.txt": b"1"}, _params(name), SEED).ok


@pytest.mark.parametrize("name", NAMES)
def test_the_chunk_check_refuses_a_wrong_chunk_output(
    name: str, in_tree: Callable[[str], Path]
) -> None:
    fam = _family(name)
    in_tree(name)
    items = _chunks(name, 5)[0]
    params = fam.params_dict(_params(name))
    run = kernel.SPLIT_KERNELS[name]
    out = run.run(params, SEED, items)
    assert run.check(params, SEED, items, out) is None
    data = out[fam.output_file]
    if name == "split.montecarlo_range":
        rows = json.loads(data)
        wrong = json.dumps([[rows[0][0], rows[0][1] + 7], *rows[1:]]).encode()
    elif name == "split.csv_aggregate":
        head, first, *rest = data.decode().splitlines(keepends=True)
        wrong = (head + first.replace(",", ",9", 1) + "".join(rest)).encode()
    else:
        first, *rest = data.decode().splitlines(keepends=True)
        wrong = (first.replace(",", ",x", 1) + "".join(rest)).encode()
    assert run.check(params, SEED, items, {fam.output_file: wrong}) is not None
    shorter = b"".join(data.splitlines(keepends=True)[:-1])
    if name == "split.montecarlo_range":
        shorter = json.dumps(json.loads(data)[:-1]).encode()
    assert run.check(params, SEED, items, {fam.output_file: shorter}) is not None


def test_a_wrong_cnn_is_caught_by_the_independent_path(
    in_tree: Callable[[str], Path],
) -> None:
    name = "split.image_classify"
    in_tree(name)
    params = _params(name)
    image = kernel._decode_ppm(
        np, kernel.image_bytes(SEED, 0, params.size), params.size
    )
    batch_labels, batch_scores = kernel.classify_batch(np, image[None], params.width)
    assert (batch_labels[0], batch_scores[0]) == kernel.classify_reference(
        np, image, params.width
    )


def test_montecarlo_estimates_pi() -> None:
    inside = kernel.inside_count(np, SEED, 0, 400_000, 1 << 20)
    assert abs(4 * inside / 400_000 - 3.14159) < 0.02
    # batching never changes a stream
    assert inside == kernel.inside_count(np, SEED, 0, 400_000, 4096)


# --------------------------------------------------------------- chunking


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("size", [1, 3, 7])
def test_any_chunking_combined_equals_the_one_chunk_output(
    name: str, size: int, in_tree: Callable[[str], Path]
) -> None:
    fam = _family(name)
    in_tree(name)
    params = fam.params_dict(_params(name))
    whole, problem, _ = kernel.run_items(name, params, SEED, _items(name))
    assert problem is None
    parts = []
    for items in _chunks(name, size):
        out, problem, _ = kernel.run_items(name, params, SEED, items)
        assert problem is None
        parts.append(out)
    combined = _combine(name, parts)
    if fam.combine == "merge-json":
        assert json.loads(combined[fam.output_file]) == json.loads(
            whole[fam.output_file]
        )
    else:
        assert combined == whole  # byte-identical (criterion 1's mechanism)
    assert fam.verify_output(combined, _params(name), SEED).ok


def test_outputs_are_identical_across_runs_and_processes(
    in_tree: Callable[[str], Path],
) -> None:
    name = "split.image_classify"
    in_tree(name)
    params = _family(name).params_dict(_params(name))
    one = kernel.run_items(name, params, SEED, _items(name))[0]
    two = kernel.run_items(name, params, SEED, _items(name))[0]
    assert one == two
    digest = hashlib.sha256(one["predictions.csv"]).hexdigest()
    assert len(digest) == 64


# --------------------------------------------------------------- main


@pytest.mark.parametrize("name", ["split.hash_files", "split.csv_aggregate"])
def test_main_writes_the_chunk_and_exits_by_outcome(
    name: str, in_tree: Callable[[str], Path], tmp_path: Path
) -> None:
    root = in_tree(name)
    fam = _family(name)
    (root / "workload_params.json").write_text(
        json.dumps(
            {"family": name, "params": fam.params_dict(_params(name)), "seed": SEED}
        )
    )
    items = root / "items.json"
    items.write_bytes(_chunks(name, 4)[0])
    out = tmp_path / f"out-{name}"
    assert kernel.main([str(items), "0", str(out)]) == 0
    assert (out / fam.output_file).is_file()
    # bad requests: exit 2
    assert kernel.main(["only-one"]) == kernel.EXIT_BAD_PARAMS
    (root / "workload_params.json").write_text(
        json.dumps({"family": name, "params": {"rounds": -1}, "seed": SEED})
    )
    assert kernel.main([str(items), "0", str(out)]) == kernel.EXIT_BAD_PARAMS
    (root / "workload_params.json").write_text(
        json.dumps({"family": "split.nope", "params": {}, "seed": SEED})
    )
    assert kernel.main([str(items), "0", str(out)]) == kernel.EXIT_BAD_PARAMS


def test_main_exits_4_when_the_chunk_check_fails(
    in_tree: Callable[[str], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "split.hash_files"
    root = in_tree(name)
    fam = _family(name)
    (root / "workload_params.json").write_text(
        json.dumps(
            {"family": name, "params": fam.params_dict(_params(name)), "seed": SEED}
        )
    )
    items = root / "items.json"
    items.write_bytes(_chunks(name, 4)[0])
    broken = kernel.SplitKernel(
        run=kernel.SPLIT_KERNELS[name].run,
        check=lambda *_a: "planted mismatch",
        check_params=kernel.SPLIT_KERNELS[name].check_params,
        items_label="file",
    )
    monkeypatch.setitem(kernel.SPLIT_KERNELS, name, broken)
    assert (
        kernel.main([str(items), "0", str(tmp_path / "o")]) == kernel.EXIT_CHECK_FAILED
    )


def test_in_process_run_is_checked_and_restores_the_working_directory() -> None:
    before = os.getcwd()
    fam = _family("split.csv_aggregate")
    result = fam.run({"rows": 4000, "cols": 8, "rounds": 7000}, SEED)
    assert os.getcwd() == before
    assert result.verified and result.status == "ok"
    assert fam.verify(result).ok
    assert (result.checksum or "").startswith("sha256:")
    with pytest.raises(ParamsError):
        fam.run({"rows": 100, "cols": 2, "rounds": 1}, SEED)  # outside the window
    assert os.getcwd() == before
