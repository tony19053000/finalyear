"""V0.9 G2 tester round 1 (split from ``..._contract.py``, G3 item 0.2, D225.4):
adversarial contract tests for the materialised job folder (no leakage, LF only,
only the family's kernel, argv manifest, injection-proof params, odd and
concurrent destinations) and the bench import contracts.
"""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from meshforge.bench.workloads import (
    MaterialiseError,
    ParamsError,
    build_job,
    get_family,
    list_families,
)
from meshforge.bench.workloads.materialise import job_files
from meshforge.common.manifest import load_manifest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
BENCH_SRC = REPO / "src" / "meshforge" / "bench"
FAMILIES = list_families()
IDS = [f.name for f in FAMILIES]


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
