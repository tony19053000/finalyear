"""V0.9 G2 tester round 1: real processes against the workload library.

* determinism of the job folder and of the run output across processes
  (``PYTHONHASHSEED``, cwd, umask, locale, timezone, thread count);
* the self-checks of the torch families, run on the CPU device.

Split by topic in G3 item 0.2 (D225.4): the offline audit, the exit codes and
the CLI live in ``test_v09_g2_tester_r1_offline.py``, ``..._exit_codes.py`` and
``..._cli_process.py``; shared helpers in ``tests/_support/mf_bench_procs.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mf_bench_procs import (  # tests/_support, shared by the split files
    CPU_NAMES,
    FAMILIES,
    TINY,
    TORCH_NAMES,
    _env,
    _py,
    needs_torch,
)

from meshforge.bench.workloads import (
    build_job,
)
from meshforge.bench.workloads.kernels import gpu as gpu_kernels
from meshforge.bench.workloads.kernels import ml as ml_kernels
from meshforge.bench.workloads.kernels import runner

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]


# ------------------------------------------------- folder determinism (procs)

_BUILD_ALL = """
import hashlib, json, sys
from pathlib import Path
from meshforge.bench.workloads import build_job, list_families
from meshforge.common.bundle import pack_to_file
out = {}
dest = Path(sys.argv[1])
for f in list_families():
    built = build_job(f.name, {}, 424242, dest / f.name.replace('.', '_'))
    bundle = str(pack_to_file(built.path, dest / (f.name + '.bundle')).digest)
    h = hashlib.sha256()
    for name, data in sorted((n, (built.path / n).read_bytes()) for n in built.files):
        h.update(name.encode() + b'\\0' + data)
    out[f.name] = [built.content_digest, bundle, h.hexdigest()]
print(json.dumps(out, sort_keys=True))
"""


def _build_in_process(tmp: Path, **env: str) -> dict[str, Any]:
    sub = tmp / "wd"
    sub.mkdir(parents=True, exist_ok=True)
    dest = tmp / "out"
    code = f"import sys; sys.argv = ['x', {str(dest)!r}]\n" + _BUILD_ALL
    done = _py(code, cwd=sub, env=_env(**env))
    assert done.returncode == 0, done.stderr
    loaded: Any = json.loads(done.stdout)
    return loaded  # type: ignore[no-any-return]


def test_job_folders_and_bundles_are_identical_across_processes_and_environments(
    tmp_path: Path,
) -> None:
    variants = [
        {"PYTHONHASHSEED": "0"},
        {"PYTHONHASHSEED": "1", "TZ": "Asia/Kolkata", "LANG": "C"},
        {"PYTHONHASHSEED": "4242", "TZ": "Pacific/Kiritimati", "LC_ALL": "C.UTF-8"},
        {"PYTHONHASHSEED": "random", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        {"PYTHONHASHSEED": "7", "COLUMNS": "20", "NO_COLOR": "1"},
    ]
    results = [
        _build_in_process(tmp_path / f"v{i}", **v) for i, v in enumerate(variants)
    ]
    assert all(r == results[0] for r in results[1:])
    assert len(results[0]) == 15
    # umask must not change content either (it changes modes only)
    code = "import os; os.umask(0o077)\n" + _BUILD_ALL
    sub = tmp_path / "umask"
    sub.mkdir()
    done = _py(f"import sys; sys.argv = ['x', {str(sub / 'o')!r}]\n" + code, cwd=sub)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == results[0]
    # ... and equals what this very process builds
    here = {}
    for f in FAMILIES:
        built = build_job(f.name, {}, 424242, tmp_path / "here" / f.name)
        here[f.name] = built.content_digest
    assert {k: v[0] for k, v in results[0].items()} == here


# ------------------------------------------------------ result determinism

_RUN_ALL = """
import json, sys
from meshforge.bench.workloads.kernels.runner import execute
tiny = json.loads(sys.argv[1]); names = json.loads(sys.argv[2]); seed = int(sys.argv[3])
out = {}
for name in names:
    dev = 'cpu'
    d = execute(name, tiny[name], seed, device=dev)
    out[name] = [d['checksum'], d['checksum_exact'], d['verified'], d['status']]
print(json.dumps(out, sort_keys=True))
"""


def _run_all(
    names: list[str], seed: int, cwd: Path, **env: str
) -> dict[str, list[Any]]:
    done = _py(
        f"import sys; sys.argv = ['x', {json.dumps(json.dumps(TINY))}, "
        f"{json.dumps(json.dumps(names))}, {str(seed)!r}]\n" + _RUN_ALL,
        cwd=cwd,
        env=_env(**env),
    )
    assert done.returncode == 0, done.stderr
    loaded: Any = json.loads(done.stdout)
    return loaded  # type: ignore[no-any-return]


def test_numpy_family_checksums_repeat_across_processes_hashseeds_cwd_and_threads(
    tmp_path: Path,
) -> None:
    names = list(CPU_NAMES)
    (tmp_path / "a").mkdir()
    (tmp_path / "b with space").mkdir()
    first = _run_all(names, 11, tmp_path / "a", PYTHONHASHSEED="0")
    second = _run_all(names, 11, tmp_path / "b with space", PYTHONHASHSEED="99")
    assert first == second
    assert all(v[2] and v[3] == "ok" for v in first.values()), first
    # exact checksums are host-independent: they must also survive other thread counts
    single = _run_all(
        names,
        11,
        tmp_path / "a",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
    )
    for name, (checksum, exact, *_rest) in first.items():
        if exact:
            assert single[name][0] == checksum, name
    other_seed = _run_all(names, 12, tmp_path / "a")
    for name in (
        "cpu.matmul",
        "cpu.sort",
        "cpu.zlib",
        "cpu.sha256",
        "cpu.integrate",
        "memory.copy",
        "memory.groupby",
    ):
        assert other_seed[name][0] != first[name][0], f"{name} ignores its seed"


@needs_torch
def test_torch_family_checksums_repeat_across_processes_on_cpu(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    first = _run_all(TORCH_NAMES, 5, tmp_path / "t", PYTHONHASHSEED="0")
    second = _run_all(TORCH_NAMES, 5, tmp_path / "t", PYTHONHASHSEED="3")
    assert first == second
    assert all(v[2] and v[3] == "ok" for v in first.values()), first


def test_in_process_run_equals_the_subprocess_job_run(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    sub = _run_all(["cpu.sort", "cpu.zlib"], 21, tmp_path / "w")
    for name in ("cpu.sort", "cpu.zlib"):
        here = runner.execute(name, TINY[name], 21)
        assert [
            here["checksum"],
            here["checksum_exact"],
            here["verified"],
            here["status"],
        ] == sub[name]


# --------------------------------------------------- torch-family self-checks


def _torch_run(mod: Any, family: str, params: dict[str, Any], mutate: Any) -> Any:
    kernel = mod.KERNELS[family]
    state = kernel.prepare(params, 1, "cpu")
    if kernel.warmup:
        kernel.warmup(state)
    return kernel.check(state, mutate(state, kernel.compute(state)))


@needs_torch
def test_torch_checks_catch_gross_corruption() -> None:
    same = lambda _s, o: o  # noqa: E731
    for mod, fam, mutators in (
        (
            gpu_kernels,
            "gpu.matmul",
            [lambda _s, o: o.T.contiguous(), lambda _s, o: o * 0],
        ),
        (gpu_kernels, "gpu.conv2d", [lambda _s, o: o * 1.1, lambda _s, o: o * 0]),
        (gpu_kernels, "gpu.elementwise", [lambda _s, o: o * 0, lambda _s, o: o + 1]),
        (gpu_kernels, "gpu.resize", [lambda _s, o: o * 0, lambda _s, o: o.roll(1, -1)]),
        (
            ml_kernels,
            "ml.cnn_infer",
            [lambda _s, o: o * 0, lambda _s, o: o * float("nan")],
        ),
        (
            ml_kernels,
            "ml.cnn_train",
            [lambda _s, o: o.flip(0), lambda _s, o: o * float("nan")],
        ),
        (
            ml_kernels,
            "ml.transformer_infer",
            [lambda _s, o: o * 0, lambda _s, o: o + 1],
        ),
    ):
        params = TINY[fam]
        if fam == "gpu.conv2d":
            params = {**params, "channels": 8, "size": 16}
        assert _torch_run(mod, fam, params, same).ok, fam
        for i, mutate in enumerate(mutators):
            assert not _torch_run(mod, fam, params, mutate).ok, (fam, i)


@needs_torch
def test_elementwise_check_sees_corruption_beyond_the_first_4096_elements() -> None:
    def late(_s: Any, o: Any) -> Any:
        o = o.clone()
        o[500_000:] = 0
        return o

    assert not _torch_run(
        gpu_kernels, "gpu.elementwise", TINY["gpu.elementwise"], late
    ).ok


@needs_torch
def test_resize_check_sees_corruption_of_images_after_the_first() -> None:
    def late(_s: Any, o: Any) -> Any:
        o = o.clone()
        o[1:] = 0
        return o

    params = {"batch": 3, "size": 16, "out_size": 32, "repeats": 1}
    assert not _torch_run(gpu_kernels, "gpu.resize", params, late).ok


@needs_torch
def test_resize_check_reports_a_wrong_shape_as_a_failed_check_not_an_exception() -> (
    None
):
    params = {"batch": 3, "size": 16, "out_size": 32, "repeats": 1}
    check = _torch_run(
        gpu_kernels, "gpu.resize", params, lambda _s, o: o[..., :16, :16]
    )
    assert not check.ok
