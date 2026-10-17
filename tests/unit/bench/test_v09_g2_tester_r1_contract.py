# ruff: noqa: E501  (long xfail reasons / parametrize ids are clearer on one line)
"""V0.9 G2 tester round 1: adversarial contract tests for the workload library.

Derived from the brief (docs/briefs/V0.9-benchmark-telemetry.md section 5c, D214),
not from the implementation: strict params, the 5 s - 3 min window at its exact
edges, monotonic hints, byte-identical folders, odd destinations, odd seeds,
registry and import contracts.
"""

from __future__ import annotations

import ast
import itertools
import math
import subprocess
import sys
import threading
import typing
from pathlib import Path
from typing import Any

import pytest
import yaml

from meshforge.bench.workloads import (
    MAX_RUNTIME_S,
    MIN_RUNTIME_S,
    MaterialiseError,
    ParamsError,
    UnknownFamilyError,
    build_job,
    get_family,
    list_families,
)
from meshforge.bench.workloads import base as base_mod
from meshforge.bench.workloads.base import HardwareHint, Unavailable
from meshforge.bench.workloads.materialise import job_files
from meshforge.common.manifest import load_manifest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
BENCH_SRC = REPO / "src" / "meshforge" / "bench"
FAMILIES = list_families()
IDS = [f.name for f in FAMILIES]

EXPECTED_NAMES = {
    "cpu.matmul",
    "cpu.primes",
    "cpu.sort",
    "cpu.zlib",
    "cpu.sha256",
    "cpu.integrate",
    "memory.copy",
    "memory.groupby",
    "gpu.matmul",
    "gpu.conv2d",
    "gpu.elementwise",
    "gpu.resize",
    "ml.cnn_infer",
    "ml.cnn_train",
    "ml.transformer_infer",
}


def _int_bounds(family: Any, field: str) -> tuple[int, int] | None:
    info = family.params_model.model_fields[field]
    if info.annotation is not int:
        return None
    ge = next(m.ge for m in info.metadata if hasattr(m, "ge"))
    le = next(m.le for m in info.metadata if hasattr(m, "le"))
    return int(ge), int(le)


def _step(family: Any, field: str) -> int:
    """Granularity of a field (transformer width must be a multiple of 64)."""
    return 64 if (family.name, field) == ("ml.transformer_infer", "width") else 1


def _int_fields(family: Any) -> list[str]:
    return [
        n
        for n in family.params_model.model_fields
        if _int_bounds(family, n) is not None
    ]


def _literal_fields(family: Any) -> dict[str, tuple[Any, ...]]:
    out: dict[str, tuple[Any, ...]] = {}
    for name, info in family.params_model.model_fields.items():
        if typing.get_origin(info.annotation) is typing.Literal:
            out[name] = typing.get_args(info.annotation)
    return out


# ------------------------------------------------------------------ registry


def test_registry_is_exactly_the_fifteen_families_by_kind() -> None:
    names = [f.name for f in FAMILIES]
    assert set(names) == EXPECTED_NAMES
    assert names == sorted(names)
    kinds = [f.kind.value for f in FAMILIES]
    assert (
        kinds.count("cpu"),
        kinds.count("memory"),
        kinds.count("gpu"),
        kinds.count("ml"),
    ) == (6, 2, 4, 3)
    for family in FAMILIES:
        assert family.name.startswith(family.kind.value + ".")


def test_registering_a_duplicate_name_is_refused() -> None:
    with pytest.raises(ValueError, match="registered twice"):
        base_mod.register(get_family("cpu.sort"))


@pytest.mark.parametrize(
    "name",
    ["", "CPU.SORT", " cpu.sort", "cpu.sort ", "cpu.sort\x00", "cpu", "../x", "cpu.*"],
)
def test_unknown_family_names_raise_the_typed_error(name: str) -> None:
    with pytest.raises(UnknownFamilyError):
        get_family(name)
    with pytest.raises(UnknownFamilyError):
        job_files(name, {}, 1)


def test_importing_the_library_and_listing_does_not_import_numpy_or_torch() -> None:
    code = (
        "import sys\n"
        "from meshforge.bench.workloads import list_families, get_family\n"
        "for f in list_families():\n"
        "    f.parse_params({})\n"
        "    f.describe()\n"
        "bad = [m for m in ('numpy', 'torch', 'pandas') if m in sys.modules]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


# ------------------------------------------------------------ strict params


def _junk_for(bounds: tuple[int, int]) -> list[Any]:
    low, high = bounds
    return [
        True,
        False,
        float(low) if low else 1.0,
        float(low) + 0.5,
        str(low),
        float("nan"),
        float("inf"),
        float("-inf"),
        10**100,
        -(10**100),
        high + 1,
        low - 1,
        None,
        [low],
        {"v": low},
    ]


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_odd_int_values_are_refused_with_params_error_only(family: Any) -> None:
    for field in _int_fields(family):
        bounds = _int_bounds(family, field)
        assert bounds is not None
        for junk in _junk_for(bounds):
            with pytest.raises(ParamsError) as caught:
                family.parse_params({field: junk})
            assert field in "; ".join(caught.value.problems), (field, junk)
            # the same refusal through every other entry point
            with pytest.raises(ParamsError):
                family.params_dict({field: junk})
            with pytest.raises(ParamsError):
                job_files(family.name, {field: junk}, 1)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_odd_literal_values_unknown_keys_and_wrong_containers(family: Any) -> None:
    for field, choices in _literal_fields(family).items():
        for junk in (
            "nope",
            "",
            None,
            3.5,
            True,
            [choices[0]],
            "1" if isinstance(choices[0], int) else 1,
        ):
            if junk in choices and not isinstance(junk, bool):
                continue
            with pytest.raises(ParamsError):
                family.parse_params({field: junk})
    for raw in (
        {"nope": 1},
        {"seed": 1},
        {"__class__": "x"},
        {"": 1},
        None,
        [],
        "cpu",
        42,
        object(),
    ):
        with pytest.raises(ParamsError):
            family.parse_params(raw)


def test_a_params_instance_of_another_family_is_refused() -> None:
    other = get_family("cpu.sort").smallest_params()
    with pytest.raises(ParamsError):
        get_family("cpu.zlib").parse_params(other)


def test_a_value_inside_pydantic_bounds_but_outside_the_window_names_the_hint() -> None:
    family = get_family("cpu.matmul")
    with pytest.raises(ParamsError) as caught:
        family.parse_params({"n": 4096, "repeats": 500})
    text = " ".join(caught.value.problems)
    assert "reference mid laptop" in text
    assert "5-180" in text


# ---------------------------------------------------- window and the hint


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_the_window_edges_are_exact_for_every_size_parameter(family: Any) -> None:
    """Bisect each integer size parameter to its accepted interval; the value one
    step outside must be refused and the one inside must be inside [5, 180]."""
    defaults = family.params_dict(family.smallest_params())
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds is None:
            continue
        step = _step(family, field)
        low, high = bounds[0] // step, bounds[1] // step

        def ok(value: int, field: str = field, step: int = step) -> bool:
            try:
                family.parse_params({**defaults, field: value * step})
            except ParamsError:
                return False
            return True

        mid = defaults[field] // step
        assert ok(mid)
        lo, hi = low, mid  # smallest accepted value in [low, mid]
        while lo < hi:
            probe = (lo + hi) // 2
            if ok(probe):
                hi = probe
            else:
                lo = probe + 1
        first = lo
        lo, hi = mid, high  # largest accepted value in [mid, high]
        while lo < hi:
            probe = (lo + hi + 1) // 2
            if ok(probe):
                lo = probe
            else:
                hi = probe - 1
        last = lo
        for edge in (first, last):
            hint = family.expected_runtime_s({**defaults, field: edge * step})
            assert MIN_RUNTIME_S <= hint <= MAX_RUNTIME_S, (field, edge, hint)
        if first > low:
            outside = {**defaults, field: (first - 1) * step}
            assert family.expected_runtime_s(outside) < MIN_RUNTIME_S, outside
            with pytest.raises(ParamsError, match="reference mid laptop"):
                family.parse_params(outside)
        if last < high:
            outside = {**defaults, field: (last + 1) * step}
            assert family.expected_runtime_s(outside) > MAX_RUNTIME_S, outside
            with pytest.raises(ParamsError, match="reference mid laptop"):
                family.parse_params(outside)


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_hint_is_monotonic_in_every_size_parameter_over_the_whole_range(
    family: Any,
) -> None:
    defaults = family.params_dict(family.smallest_params())
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds is None:
            continue
        step = _step(family, field)
        low, high = bounds
        grid = sorted(
            {
                low,
                high,
                *(
                    (low + (high - low) * i // 40) // step * step or low
                    for i in range(41)
                ),
            }
        )
        hints = [family.expected_runtime_s({**defaults, field: v}) for v in grid]
        assert all(math.isfinite(h) and h > 0 for h in hints), field
        assert hints == sorted(hints), (family.name, field)
        assert hints[0] < hints[-1], f"{family.name}.{field} does not change the hint"


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_every_corner_of_the_parameter_space_is_safe(family: Any) -> None:
    """Every min/max corner x every literal choice: finite positive hint and
    manifest numbers, and parse_params either accepts or raises ParamsError."""
    int_fields = _int_fields(family)
    literals = _literal_fields(family)
    ranges = [(_int_bounds(family, f) or (0, 0)) for f in int_fields]
    lit_names = list(literals)
    for corner in itertools.product(*[(r[0], r[1]) for r in ranges]):
        for lits in itertools.product(*[literals[n] for n in lit_names]):
            raw = dict(zip(int_fields, corner, strict=True))
            raw.update(dict(zip(lit_names, lits, strict=True)))
            hint = family.expected_runtime_s(raw)
            assert math.isfinite(hint) and hint > 0, raw
            params = family._model(raw)
            ram = family.ram_gb(params)
            assert math.isfinite(ram) and ram > 0, raw
            vram = family.vram_gb(params)
            assert (vram is not None) == family.gpu, raw
            if vram is not None:
                assert math.isfinite(vram) and vram > 0
            try:
                family.parse_params(raw)
            except ParamsError:
                continue


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_defaults_are_the_smallest_run_inside_the_window(family: Any) -> None:
    hint = family.expected_runtime_s(family.smallest_params())
    assert MIN_RUNTIME_S <= hint <= 2.5 * MIN_RUNTIME_S
    # "smallest": shrinking any size parameter by one step falls below the window
    defaults = family.params_dict(family.smallest_params())
    shrinkable = 0
    for field in family.size_params:
        bounds = _int_bounds(family, field)
        if bounds and defaults[field] > bounds[0]:
            shrunk = {**defaults, field: max(bounds[0], int(defaults[field] * 0.5))}
            if family.expected_runtime_s(shrunk) < hint:
                shrinkable += 1
    assert shrinkable >= 1 or len(family.size_params) == 0


def test_hint_on_hardware_without_the_metric_is_a_typed_unavailable() -> None:
    nothing = HardwareHint()
    for family in FAMILIES:
        with pytest.raises(Unavailable):
            family.expected_runtime_s(family.smallest_params(), nothing)
    cpu_only = HardwareHint(
        cpu_fp32_gflops_1t=50,
        cpu_fp32_gflops_all=100,
        cpu_fp64_gflops_all=50,
        mem_triad_gb_s=10,
    )
    for family in FAMILIES:
        if family.gpu:
            with pytest.raises(Unavailable):
                family.expected_runtime_s(family.smallest_params(), cpu_only)
        else:
            assert family.expected_runtime_s(family.smallest_params(), cpu_only) > 0
    for bad in (0.0, -1.0):
        with pytest.raises(Unavailable):
            get_family("cpu.sort").expected_runtime_s(
                {}, HardwareHint(cpu_fp32_gflops_1t=bad)
            )


@pytest.mark.parametrize(("faster", "slower"), [(200.0, 100.0), (1000.0, 10.0)])
def test_a_faster_node_never_has_a_longer_hint(faster: float, slower: float) -> None:
    for family in FAMILIES:
        if family.gpu:
            continue
        fast = HardwareHint(
            cpu_fp32_gflops_1t=faster,
            cpu_fp32_gflops_all=faster,
            cpu_fp64_gflops_all=faster,
            mem_triad_gb_s=faster,
        )
        slow = HardwareHint(
            cpu_fp32_gflops_1t=slower,
            cpu_fp32_gflops_all=slower,
            cpu_fp64_gflops_all=slower,
            mem_triad_gb_s=slower,
        )
        assert family.expected_runtime_s({}, fast) < family.expected_runtime_s({}, slow)


# ----------------------------------------------------------------- seeds


@pytest.mark.parametrize("seed", [0, 1, 2**32 - 1])
def test_boundary_seeds_are_accepted(seed: int) -> None:
    assert "workload_params.json" in job_files("cpu.sort", {}, seed)


@pytest.mark.parametrize(
    "seed", [-1, 2**32, 2**64, True, False, 1.5, float("nan"), "1", None, [1], b"1"]
)
def test_bad_seeds_are_refused_in_job_files(seed: Any) -> None:
    with pytest.raises(ParamsError, match="seed"):
        job_files("cpu.sort", {}, seed)


@pytest.mark.parametrize(
    "seed",
    [
        -1,
        2**32,
        True,
        1.5,
        None,
        "7",
    ],
)
def test_run_refuses_bad_seeds_before_executing_anything(
    seed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reached(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"seed {seed!r} reached the kernel runner")

    monkeypatch.setattr(base_mod, "execute", reached)
    with pytest.raises(ParamsError):
        get_family("cpu.sort").run({}, seed)


# ----------------------------------------------------------- materialise


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_folder_has_no_environment_leakage_and_only_lf(
    family: Any, tmp_path: Path
) -> None:
    a = build_job(family.name, {}, 11, tmp_path / "alpha dir")
    b = build_job(family.name, {}, 11, tmp_path / "other" / "beta")
    assert _tree(a.path) == _tree(b.path)
    forbidden = [
        str(tmp_path).encode(),
        str(Path.home()).encode(),
        b"\r",
        b"\t",
        b"/home/",
        b"C:\\",
    ]
    for name, data in _tree(a.path).items():
        for needle in forbidden:
            if name.endswith(".py") and needle == b"\t":
                continue
            assert needle not in data, (name, needle)
        assert data.endswith(b"\n"), name


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_only_the_family_kernel_is_copied_and_it_matches_the_package(
    family: Any, tmp_path: Path
) -> None:
    built = build_job(family.name, {}, 1, tmp_path / "j")
    files = set(built.files)
    kernel_files = {f for f in files if f.startswith("bench_kernels/")}
    module = {"cpu": "cpu", "memory": "memory", "gpu": "gpu", "ml": "ml"}[
        family.kind.value
    ]
    assert kernel_files == {
        "bench_kernels/__init__.py",
        "bench_kernels/_support.py",
        "bench_kernels/runner.py",
        f"bench_kernels/{module}.py",
    }
    src = BENCH_SRC / "workloads" / "kernels" / f"{module}.py"
    assert (
        built.path / "bench_kernels" / f"{module}.py"
    ).read_bytes() == src.read_bytes().replace(b"\r\n", b"\n")
    assert ("requirements.txt" in files) == family.needs_numpy


@pytest.mark.parametrize("family", FAMILIES, ids=IDS)
def test_manifest_is_argv_no_shell_network_none_and_resources_follow_kind(
    family: Any, tmp_path: Path
) -> None:
    built = build_job(family.name, {}, 5, tmp_path / "j")
    doc = yaml.safe_load((built.path / "compute.yaml").read_text())
    assert isinstance(doc["entrypoint"], list)
    assert doc["entrypoint"] == ["python", "run_workload.py"]
    assert doc["limits"]["network"] == "none"
    assert doc["outputs"] == ["results/"]
    assert doc["resources"]["gpu"] == ("required" if family.gpu else "none")
    assert doc["env"]["base"] == ("cuda-pytorch" if family.gpu else "python-cpu")
    assert ("vram_gb" in doc["resources"]) == family.gpu
    assert doc["resources"]["ram_gb"] * 2 == int(doc["resources"]["ram_gb"] * 2)
    assert doc["resources"]["ram_gb"] >= family.ram_gb(family.smallest_params())
    manifest = load_manifest(built.path)
    assert manifest.limits.network == "none"
    assert len(manifest.name) < 64
    text = (built.path / "run_workload.py").read_text()
    assert "shell" not in text and "os.system" not in text and "subprocess" not in text


def test_param_values_cannot_inject_into_the_manifest_or_paths(tmp_path: Path) -> None:
    for payload in (
        "../../etc/passwd",
        "float64\nnetwork: on",
        "a: b",
        "$(id)",
        "x" * 10000,
    ):
        with pytest.raises(ParamsError):
            build_job("cpu.matmul", {"dtype": payload}, 1, tmp_path / "evil")
        assert not (tmp_path / "evil").exists()
    # a params *key* is never a path or a manifest key
    with pytest.raises(ParamsError):
        build_job("cpu.matmul", {"../x": 1}, 1, tmp_path / "evil2")
    assert not (tmp_path / "evil2").exists()


def test_refusals_leave_the_destination_untouched(tmp_path: Path) -> None:
    dest = tmp_path / "keep"
    dest.mkdir()
    (dest / "mine.txt").write_text("precious")
    with pytest.raises(MaterialiseError):
        build_job("cpu.sort", {}, 1, dest)
    assert _tree(dest) == {"mine.txt": b"precious"}
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(MaterialiseError):
        build_job("cpu.sort", {}, 1, f)
    assert f.read_text() == "x"
    # a hidden file still counts as "not empty"
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / ".keep").write_text("")
    with pytest.raises(MaterialiseError):
        build_job("cpu.sort", {}, 1, hidden)
    # invalid params never create the folder, not even an empty one
    with pytest.raises(ParamsError):
        build_job("cpu.sort", {"n": 1}, 1, tmp_path / "never")
    assert not (tmp_path / "never").exists()
    # building twice into the same folder is refused, the first build intact
    first = build_job("cpu.sort", {}, 1, tmp_path / "once")
    before = _tree(first.path)
    with pytest.raises(MaterialiseError):
        build_job("cpu.sort", {}, 2, tmp_path / "once")
    assert _tree(first.path) == before


@pytest.mark.parametrize(
    "leaf",
    [
        "job with spaces",
        "job-\u00fcnicode-\u65e5\u672c\u8a9e-\U0001f680",
        "back\\slash\\name",
        "trailing.dot.",
        "x" * 120,
        "UPPER_and-dash.ext",
    ],
)
def test_odd_destination_names_work_and_load(leaf: str, tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "er" / leaf
    built = build_job("cpu.zlib", {}, 3, dest)
    assert load_manifest(dest) == built.manifest
    again = build_job("cpu.zlib", {}, 3, tmp_path / "plain")
    assert again.content_digest == built.content_digest  # destination is not an input


def test_an_empty_existing_destination_is_accepted_and_relative_paths_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    build_job("cpu.sort", {}, 1, tmp_path / "empty")
    monkeypatch.chdir(tmp_path)
    built = build_job("cpu.sort", {}, 1, Path("rel") / "job")
    assert (tmp_path / "rel" / "job" / "compute.yaml").is_file()
    assert (
        built.content_digest
        == build_job("cpu.sort", {}, 1, tmp_path / "abs").content_digest
    )


def test_concurrent_builds_of_the_same_request_agree(tmp_path: Path) -> None:
    digests: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def work(i: int) -> None:
        try:
            barrier.wait()
            digests.append(
                build_job("ml.cnn_train", {}, 9, tmp_path / f"d{i}").content_digest
            )
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(digests)) == 1 and len(digests) == 8


def test_concurrent_builds_into_one_destination_never_corrupt_it(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "shared"
    expected = job_files("cpu.sort", {}, 4)
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def work() -> None:
        barrier.wait()
        try:
            build_job("cpu.sort", {}, 4, dest)
            outcomes.append("ok")
        except MaterialiseError:
            outcomes.append("refused")
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(f"crash {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(o in ("ok", "refused") for o in outcomes), outcomes
    assert "ok" in outcomes
    assert _tree(dest) == expected


def test_job_files_are_sorted_and_stable_between_calls() -> None:
    first = job_files("gpu.conv2d", {}, 77)
    second = job_files("gpu.conv2d", dict(get_family("gpu.conv2d").params_dict({})), 77)
    assert first == second
    assert list(first) == sorted(first)


# --------------------------------------------------------- import contracts


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def test_bench_package_imports_only_common_from_meshforge() -> None:
    offenders: dict[str, set[str]] = {}
    for path in BENCH_SRC.rglob("*.py"):
        bad = {
            m
            for m in _imports(path)
            if m.startswith("meshforge")
            and not (m.startswith(("meshforge.common", "meshforge.bench")))
        }
        if bad:
            offenders[str(path.relative_to(REPO))] = bad
    assert not offenders


def test_job_side_kernels_import_nothing_but_stdlib_numpy_torch() -> None:
    allowed_third = {"numpy", "torch"}
    stdlib = set(sys.stdlib_module_names)
    for path in (BENCH_SRC / "workloads" / "kernels").glob("*.py"):
        for module in _imports(path):
            root = module.split(".")[0]
            assert root in stdlib or root in allowed_third, (path.name, module)
            assert root != "meshforge", (path.name, module)
        source = path.read_text(encoding="utf-8")
        names = {
            n.id if isinstance(n, ast.Name) else n.attr
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Name | ast.Attribute)
        }
        banned = {
            "socket",
            "urlopen",
            "urlretrieve",
            "getaddrinfo",
            "hub",
            "Popen",
            "requests",
            "urllib",
        }
        assert not names & banned, (path.name, names & banned)
        # top-level imports must be stdlib only: numpy/torch only inside functions
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] in stdlib for a in node.names), (
                    path.name
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] in stdlib, (path.name, node.module)
