# ruff: noqa: E501  (long xfail reasons / parametrize ids are clearer on one line)
"""V0.9 G2 tester round 1: real processes against the workload library.

* determinism of the job folder and of the run output across processes
  (``PYTHONHASHSEED``, cwd, umask, locale, timezone, thread count);
* the generated entrypoint run offline: an audit hook proves no socket, no
  download, no subprocess, no write outside ``results/``, and a import blocker
  proves the job folder does not need MeshForge installed;
* every exit code of ``run_workload.py`` (0 / 2 / 3 / 4) in a real process;
* ``compute bench workloads [--json]`` as a user runs it;
* the self-checks of the torch families, run on the CPU device;
* the cost model against a real run on this host.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads import (
    CALIBRATION_HW,
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    build_job,
    get_family,
    list_families,
)
from meshforge.bench.workloads.kernels import gpu as gpu_kernels
from meshforge.bench.workloads.kernels import ml as ml_kernels
from meshforge.bench.workloads.kernels import runner

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

FAMILIES = list_families()
CPU_NAMES = [f.name for f in FAMILIES if not f.gpu]
TORCH_NAMES = [f.name for f in FAMILIES if f.gpu]

TINY: dict[str, dict[str, Any]] = {
    "cpu.matmul": {"n": 64, "repeats": 2, "dtype": "float64"},
    "cpu.primes": {"limit": 100_000, "repeats": 1},
    "cpu.sort": {"n": 20_000, "repeats": 2},
    "cpu.zlib": {"size_mb": 1, "level": 6, "repeats": 1},
    "cpu.sha256": {"size_mb": 2, "chunk_kb": 64},
    "cpu.integrate": {"points_m": 1, "repeats": 1},
    "memory.copy": {"size_mb": 2, "repeats": 2},
    "memory.groupby": {"rows_m": 1, "groups": 50, "repeats": 1},
    "gpu.matmul": {"n": 64, "repeats": 1, "dtype": "float32"},
    "gpu.conv2d": {"batch": 2, "channels": 4, "size": 8, "repeats": 1},
    "gpu.elementwise": {"elements_m": 1, "ops": 3, "repeats": 1},
    "gpu.resize": {"batch": 1, "size": 8, "out_size": 16, "repeats": 1},
    "ml.cnn_infer": {"batch": 2, "width": 8, "depth": 2, "image": 32, "batches": 2},
    "ml.cnn_train": {"batch": 8, "width": 8, "depth": 2, "image": 32, "steps": 20},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 8,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}
assert set(TINY) == {f.name for f in FAMILIES}


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


needs_torch = pytest.mark.skipif(
    not _torch_available(), reason="torch is not installed"
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHASHSEED",)}
    env.update(extra)
    return env


def _py(
    code: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 200,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd,
        env=env if env is not None else _env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


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
    return json.loads(done.stdout)


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
    return json.loads(done.stdout)


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


# ---------------------------------------------- the entrypoint, offline audit

_SITECUSTOMIZE = """
import atexit, json, os, sys
_fd = os.open(os.environ["MF_AUDIT_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND)
_cwd = os.path.realpath(os.getcwd())
def _log(kind, detail):
    os.write(_fd, (json.dumps([kind, detail]) + "\\n").encode())
def _hook(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.bind", "socket.sendto", "socket.__new__"):
        _log("net", [event, repr(args)[:120]])
    elif event.startswith("urllib.") or event.startswith("http.client."):
        _log("net", [event, repr(args)[:120]])
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"):
        _log("proc", [event, repr(args)[:160]])
    elif event == "open":
        path, mode = args[0], args[1]
        if mode and any(c in str(mode) for c in "wax+"):
            _log("write", str(path))
    elif event == "os.remove" or event == "os.rename" or event == "os.mkdir":
        _log("fsop", [event, repr(args)[:120]])
sys.addaudithook(_hook)

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "meshforge" or name.startswith("meshforge."):
            raise ImportError("MeshForge is not installed in a job environment: " + name)
        return None
sys.meta_path.insert(0, _Block())
"""


def _audited(
    tmp: Path, argv: list[str], cwd: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[list[Any]]]:
    hooks = tmp / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    log = tmp / "audit.log"
    log.write_text("")
    env = _env(
        MF_AUDIT_LOG=str(log),
        PYTHONPATH=str(hooks),
        PYTHONDONTWRITEBYTECODE="1",
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        **(extra_env or {}),
    )
    done = subprocess.run(
        [sys.executable, *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=250,
        check=False,
    )
    events = [json.loads(line) for line in log.read_text().splitlines() if line]
    return done, events


def test_the_audit_harness_itself_sees_network_writes_and_blocks_meshforge(
    tmp_path: Path,
) -> None:
    """Negative control: without this the offline tests below could pass vacuously."""
    work = tmp_path / "w"
    work.mkdir()
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=0.2)\n"
        "except OSError:\n"
        "    pass\n"
        "open('leak.txt', 'w').write('x')\n"
        "try:\n"
        "    import meshforge\n"
        "except ImportError:\n"
        "    print('blocked')\n"
    )
    done, events = _audited(tmp_path, ["-c", code], work)
    assert done.returncode == 0, done.stderr
    assert "blocked" in done.stdout
    kinds = {e[0] for e in events}
    assert {"net", "write"} <= kinds, events


@pytest.mark.parametrize("name", CPU_NAMES)
def test_cpu_job_entrypoint_runs_offline_without_meshforge_and_writes_only_results(
    name: str, tmp_path: Path
) -> None:
    built = build_job(name, {}, 3, tmp_path / "job")
    (built.path / "workload_params.json").write_text(
        json.dumps({"family": name, "params": TINY[name], "seed": 3}), encoding="utf-8"
    )
    before = _tree(built.path)
    done, events = _audited(tmp_path, ["run_workload.py"], built.path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [e for e in events if e[0] == "net"], events
    assert not [e for e in events if e[0] == "proc"], events
    writes = {Path(e[1]).as_posix() for e in events if e[0] == "write"}
    allowed = {(built.path / "results" / "workload.json").resolve().as_posix()}
    assert {Path(w).resolve().as_posix() for w in writes} <= allowed | {"/dev/null"}, (
        writes
    )
    after = _tree(built.path)
    assert set(after) - set(before) == {"results/workload.json"}
    assert {k: v for k, v in after.items() if k != "results/workload.json"} == before
    doc = json.loads(after["results/workload.json"])
    assert doc["status"] == "ok" and doc["verified"] is True and doc["compute_ns"] > 0


@needs_torch
@pytest.mark.parametrize("name", TORCH_NAMES)
def test_torch_kernels_in_the_job_folder_run_offline_on_cpu(
    name: str, tmp_path: Path
) -> None:
    built = build_job(name, {}, 3, tmp_path / "job")
    code = (
        "import json, sys\n"
        "from bench_kernels.runner import execute\n"
        f"d = execute({name!r}, {TINY[name]!r}, 3, device='cpu')\n"
        "print(json.dumps([d['status'], d['verified'], d['compute_ns']]))\n"
    )
    done, events = _audited(tmp_path, ["-c", code], built.path)
    assert done.returncode == 0, done.stdout + done.stderr
    status, verified, compute_ns = json.loads(done.stdout.strip().splitlines()[-1])
    assert (status, verified) == ("ok", True) and compute_ns > 0
    assert not [e for e in events if e[0] == "net"], events


# ------------------------------------------------- exit codes, real processes


def _job_with_request(
    tmp: Path, family: str, request: Any, *, raw: str | None = None
) -> Path:
    built = build_job(family, {}, 1, tmp / "job")
    target = built.path / "workload_params.json"
    target.write_text(raw if raw is not None else json.dumps(request), encoding="utf-8")
    return built.path


def _entry(
    job: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "run_workload.py"],
        cwd=job,
        env=env or _env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _doc(job: Path) -> dict[str, Any]:
    return json.loads((job / "results" / "workload.json").read_text())


def test_exit_0_for_a_verified_run(tmp_path: Path) -> None:
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    done = _entry(job)
    assert done.returncode == 0, done.stderr
    assert "benchmark: ok" in done.stdout


@pytest.mark.parametrize(
    ("label", "request_doc", "raw"),
    [
        ("file is not json", None, "{nope"),
        ("json array", None, "[]"),
        ("empty file", None, ""),
        ("unknown family", {"family": "nope.nope", "params": {}, "seed": 1}, None),
        ("family not a string", {"family": 7, "params": {}, "seed": 1}, None),
        (
            "params not an object",
            {"family": "cpu.sort", "params": [1], "seed": 1},
            None,
        ),
        (
            "missing param",
            {"family": "cpu.sort", "params": {"repeats": 1}, "seed": 1},
            None,
        ),
        (
            "bool for int",
            {"family": "cpu.sort", "params": {"n": True, "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "str for int",
            {"family": "cpu.sort", "params": {"n": "9", "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "float for int",
            {"family": "cpu.sort", "params": {"n": 9.5, "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "bad literal",
            {
                "family": "cpu.matmul",
                "params": {"n": 8, "repeats": 1, "dtype": "bf16"},
                "seed": 1,
            },
            None,
        ),
        (
            "seed is float",
            {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1.5},
            None,
        ),
        (
            "seed is bool",
            {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": True},
            None,
        ),
        ("seed missing", {"family": "cpu.sort", "params": TINY["cpu.sort"]}, None),
        (
            "sha chunk not dividing 1 MiB",
            {
                "family": "cpu.sha256",
                "params": {"size_mb": 1, "chunk_kb": 3},
                "seed": 1,
            },
            None,
        ),
    ],
)
def test_exit_2_and_a_bad_params_document_for_a_damaged_request(
    label: str, request_doc: Any, raw: str | None, tmp_path: Path
) -> None:
    job = _job_with_request(tmp_path, "cpu.sort", request_doc, raw=raw)
    done = _entry(job)
    assert done.returncode == runner.EXIT_BAD_PARAMS, (label, done.stderr)
    doc = _doc(job)
    assert doc["status"] == "bad_params" and doc["verified"] is False
    assert doc["compute_ns"] is None and doc["reason"]
    assert "Traceback" not in done.stderr, label


@pytest.mark.xfail(
    reason="TR1-D6: the job side range-checks nothing: a negative seed / dimension is an uncaught traceback (exit 1, no workload.json)",
)
@pytest.mark.parametrize(
    "request_doc",
    [
        {"family": "cpu.sort", "params": {"n": 100, "repeats": 1}, "seed": -1},
        {"family": "cpu.sort", "params": {"n": -5, "repeats": 1}, "seed": 1},
        {
            "family": "cpu.matmul",
            "params": {"n": -3, "repeats": 1, "dtype": "float64"},
            "seed": 1,
        },
    ],
    ids=["negative seed", "negative n sort", "negative n matmul"],
)
def test_exit_2_for_out_of_range_numbers_in_a_damaged_request(
    request_doc: Any, tmp_path: Path
) -> None:
    job = _job_with_request(tmp_path, request_doc["family"], request_doc)
    done = _entry(job)
    assert done.returncode == runner.EXIT_BAD_PARAMS, done.stderr
    assert _doc(job)["status"] == "bad_params"


def test_exit_3_unavailable_when_numpy_is_missing(tmp_path: Path) -> None:
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    blocker = tmp_path / "hooks2"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\nsys.modules['numpy'] = None\n"
    )
    done = _entry(job, _env(PYTHONPATH=str(blocker)))
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    doc = _doc(job)
    assert doc["status"] == "unavailable" and "numpy" in doc["reason"]
    assert doc["verified"] is False and doc["compute_ns"] is None


@needs_torch
@pytest.mark.skipif(
    _torch_available() and __import__("torch").cuda.is_available(),
    reason="this host has CUDA: the no-GPU contract cannot be observed here",
)
@pytest.mark.parametrize("name", TORCH_NAMES)
def test_exit_3_unavailable_for_every_gpu_family_on_a_host_without_cuda(
    name: str, tmp_path: Path
) -> None:
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    done = _entry(job)
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    doc = _doc(job)
    assert doc["status"] == "unavailable" and "CUDA" in doc["reason"]
    assert doc["compute_ns"] is None and doc["verified"] is False
    assert "Traceback" not in done.stderr
    family = get_family(name)
    from meshforge.bench.workloads import WorkloadResult

    assert not family.verify(WorkloadResult.model_validate(doc)).ok


def test_exit_3_when_torch_itself_is_missing(tmp_path: Path) -> None:
    name = "gpu.matmul"
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    blocker = tmp_path / "hooks3"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\nsys.modules['torch'] = None\n"
    )
    done = _entry(job, _env(PYTHONPATH=str(blocker)))
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    assert "torch" in _doc(job)["reason"]


@pytest.mark.parametrize(
    "tamper",
    [
        "KERNELS['cpu.sort'] = Kernel(_sort_prepare, lambda s: s['values'], _sort_check, _sort_warmup)",
        "KERNELS['cpu.sort'] = Kernel(_sort_prepare, lambda s: s['np'].sort(s['values'])[::-1].copy(), _sort_check, _sort_warmup)",
    ],
    ids=["unsorted", "reversed"],
)
def test_exit_4_and_a_check_failed_document_when_the_kernel_output_is_wrong(
    tamper: str, tmp_path: Path
) -> None:
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    kernel_file = job / "bench_kernels" / "cpu.py"
    kernel_file.write_text(kernel_file.read_text() + "\n" + tamper + "\n")
    done = _entry(job)
    assert done.returncode == runner.EXIT_CHECK_FAILED, done.stderr
    doc = _doc(job)
    assert doc["status"] == "check_failed" and doc["verified"] is False
    assert (
        doc["reason"] and doc["compute_ns"] > 0
    )  # time is still reported, flagged unverified
    from meshforge.bench.workloads import WorkloadResult

    assert not get_family("cpu.sort").verify(WorkloadResult.model_validate(doc)).ok


@pytest.mark.parametrize("name", CPU_NAMES)
def test_every_cpu_job_survives_an_unwritable_results_dir_loudly(
    name: str, tmp_path: Path
) -> None:
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    (job / "results").write_text("a file where the folder must go")
    done = _entry(job)
    assert done.returncode not in (0, 2, 3, 4), (
        "a lost result must not look like a clean outcome"
    )


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
@pytest.mark.xfail(
    reason="TR1-D8: gpu.elementwise verifies only the first 4096 elements; later corruption passes",
)
def test_elementwise_check_sees_corruption_beyond_the_first_4096_elements() -> None:
    def late(_s: Any, o: Any) -> Any:
        o = o.clone()
        o[500_000:] = 0
        return o

    assert not _torch_run(
        gpu_kernels, "gpu.elementwise", TINY["gpu.elementwise"], late
    ).ok


@needs_torch
@pytest.mark.xfail(
    reason="TR1-D8: gpu.resize verifies only image 0 of the batch; corruption of the other images passes",
)
def test_resize_check_sees_corruption_of_images_after_the_first() -> None:
    def late(_s: Any, o: Any) -> Any:
        o = o.clone()
        o[1:] = 0
        return o

    params = {"batch": 3, "size": 16, "out_size": 32, "repeats": 1}
    assert not _torch_run(gpu_kernels, "gpu.resize", params, late).ok


@needs_torch
@pytest.mark.xfail(
    reason="TR1-D9: a wrong-shaped resize output makes check() raise (job exit 1) instead of check_failed (exit 4)",
    raises=RuntimeError,
)
def test_resize_check_reports_a_wrong_shape_as_a_failed_check_not_an_exception() -> (
    None
):
    params = {"batch": 3, "size": 16, "out_size": 32, "repeats": 1}
    check = _torch_run(
        gpu_kernels, "gpu.resize", params, lambda _s, o: o[..., :16, :16]
    )
    assert not check.ok


# --------------------------------------------------- cost model vs reality


@pytest.mark.parametrize("name", list(CPU_NAMES))
def test_default_run_time_on_this_host_is_near_the_calibration_hint(
    name: str, tmp_path: Path
) -> None:
    """The hints were calibrated on Laptop 1. This host may be a different, busier
    machine, so the band is wide (0.3x - 3x); it still catches an efficiency that
    is off by an order of magnitude, which would silently move runs out of 5 s - 3 min."""
    family = get_family(name)
    built = build_job(name, {}, 9, tmp_path / "job")
    done = _entry(built.path)
    assert done.returncode == 0, done.stderr
    measured = _doc(built.path)["compute_ms"] / 1000.0
    hint = family.expected_runtime_s(family.smallest_params(), CALIBRATION_HW)
    assert 0.3 * hint <= measured <= 3.0 * hint, (name, measured, hint)
    # prepare/check are real work not counted in compute: the job takes longer
    doc = _doc(built.path)
    assert doc["prepare_ms"] >= 0 and doc["check_ms"] >= 0
    digest = hashlib.sha256(
        (built.path / "results" / "workload.json").read_bytes()
    ).hexdigest()
    assert digest  # the file is readable, complete JSON (parsed above)


# ------------------------------------------- network none, in a real container

STDLIB_ONLY = ["cpu.zlib", "cpu.sha256", "cpu.primes"]


@pytest.mark.docker
@pytest.mark.parametrize("name", STDLIB_ONLY)
def test_stdlib_families_run_with_the_network_really_off_in_a_container(
    name: str, tmp_path: Path
) -> None:
    """``limits.network: none`` is only honest if the job truly needs none. The
    smallest job of each stdlib-only family runs in ``python:3.12-slim`` with
    ``--network none``, a non-root user and no capabilities; a control proves the
    container cannot reach anything."""
    image = "python:3.12-slim"
    probe = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip(f"{image} is not present locally (the test never pulls)")
    built = build_job(name, {}, 5, tmp_path / "job")
    (built.path / "workload_params.json").write_text(
        json.dumps({"family": name, "params": TINY[name], "seed": 5}), encoding="utf-8"
    )
    (built.path / "results").mkdir()
    (built.path / "results").chmod(0o777)
    base = [
        "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{built.path}:/work", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
        image,
    ]  # fmt: skip
    control = subprocess.run(
        [*base, "python", "-c",
         "import socket,sys\n"
         "try:\n"
         "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
         "except OSError:\n"
         "    sys.exit(0)\n"
         "sys.exit(1)\n"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert control.returncode == 0, "the container had a network: the control failed"
    done = subprocess.run(
        [*base, "python", "run_workload.py"],
        capture_output=True, text=True, timeout=200, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    doc = _doc(built.path)
    assert doc["status"] == "ok" and doc["verified"] is True
    assert doc["libraries"]["numpy"] is None  # the stdlib families need no packages
