"""V0.17 G4 tester: bundle determinism and Windows launcher edge cases.

Brief 5d item 1: "Same determinism + MANIFEST.sha256 + size budget as G1".
A real Windows bundle build needs the network and a Windows runner (CI job
``package-windows``); here the deterministic pieces are driven on synthetic
trees: two builds of the same content with different creation order, mtimes,
umask-ish modes and directory ordering must produce byte-identical manifests
and archives, and any tampering must be seen.
"""

from __future__ import annotations

import importlib.util
import os
import random
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"{name}_g4t", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load("bundle_lib")
bb = _load("build_bundle")
WIN = lib.layout_for("Windows", "AMD64", experimental_windows=True)

TREE = {
    "bin/meshforge-agent.cmd": (b"@echo off\r\n", 0o644),
    "python/python.exe": (b"MZ" + b"\x00" * 100, 0o644),
    "python/Lib/site-packages/meshforge/__init__.py": (b"", 0o644),
    "python/Lib/site-packages/pywin32_system32/pywintypes312.dll": (b"MZ-dll", 0o600),
    "python/Lib/site-packages/ünï/日本語.py": (b"print(1)\n", 0o664),
    "python/Scripts/uv.exe": (b"MZ-uv", 0o755),
    "MANIFEST-less/empty_dir_marker": (b"", 0o644),
}


def _make(root: Path, order_seed: int, mtime: float) -> None:
    rng = random.Random(order_seed)  # noqa: S311
    items = list(TREE.items())
    rng.shuffle(items)
    for rel, (data, mode) in items:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    (root / "empty").mkdir()
    for p in [*root.rglob("*"), root]:
        os.utime(p, (mtime, mtime))


def _finish(root: Path, epoch: int) -> str:
    bb.normalise_tree(root, epoch)
    text = lib.manifest_text(lib.scan_tree(root))
    return str(text)


def test_two_builds_with_different_order_mtime_and_modes_have_identical_manifests(
    tmp_path: Path,
) -> None:
    a, b = tmp_path / "a" / "bundle", tmp_path / "b" / "bundle"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _make(a, 1, 1_000_000_000)
    _make(b, 2, 1_700_000_000)
    assert _finish(a, 1_760_000_000) == _finish(b, 1_760_000_000)
    assert lib.diff_entries(lib.scan_tree(a), lib.scan_tree(b)) == []


def test_archives_of_two_builds_are_byte_identical_and_epoch_stamped(
    tmp_path: Path,
) -> None:
    out = []
    for i, mtime in enumerate((1_000_000_000, 1_700_000_000)):
        root = tmp_path / str(i) / "meshforge-agent"
        root.mkdir(parents=True)
        _make(root, i + 10, mtime)
        bb.normalise_tree(root, 1_760_000_000)
        archive = tmp_path / f"out{i}.tar.xz"
        lib.deterministic_tar_xz(root, archive, 1_760_000_000)
        out.append(archive.read_bytes())
    assert out[0] == out[1]


def test_epoch_change_changes_the_archive_but_not_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _make(root, 3, 1_000_000_000)
    m1 = _finish(root, 1_700_000_000)
    a1 = tmp_path / "1.tar.xz"
    lib.deterministic_tar_xz(root, a1, 1_700_000_000)
    m2 = _finish(root, 1_760_000_000)
    a2 = tmp_path / "2.tar.xz"
    lib.deterministic_tar_xz(root, a2, 1_760_000_000)
    assert m1 == m2
    assert a1.read_bytes() != a2.read_bytes()


def test_normalise_makes_modes_uniform(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _make(root, 4, 1_000_000_000)
    bb.normalise_tree(root, 1_760_000_000)
    for path in root.rglob("*"):
        mode = path.stat().st_mode & 0o777
        assert mode in (0o644, 0o755), (path, oct(mode))
        assert int(path.stat().st_mtime) == 1_760_000_000


def test_verify_bundle_detects_tamper_addition_and_removal(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _make(root, 5, 1_000_000_000)
    bb.normalise_tree(root, 1_760_000_000)
    (root / lib.MANIFEST_NAME).write_text(lib.manifest_text(lib.scan_tree(root)))
    assert lib.verify_bundle(root) == []
    (root / "python/python.exe").write_bytes(b"MZ-evil")
    assert any("python.exe" in d for d in lib.verify_bundle(root))
    (root / "python/python.exe").write_bytes(b"MZ" + b"\x00" * 100)
    assert lib.verify_bundle(root) == []
    (root / "extra.dll").write_bytes(b"x")
    assert any("extra.dll" in d for d in lib.verify_bundle(root))
    (root / "extra.dll").unlink()
    (root / "bin/meshforge-agent.cmd").unlink()
    assert any("meshforge-agent.cmd" in d for d in lib.verify_bundle(root))


def test_exec_bit_flip_is_a_difference(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _make(root, 6, 1_000_000_000)
    bb.normalise_tree(root, 1_760_000_000)
    before = lib.scan_tree(root)
    (root / "python/python.exe").chmod(0o755)
    assert lib.diff_entries(before, lib.scan_tree(root))


def test_manifest_roundtrips_unicode_and_empty_dirs(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    _make(root, 7, 1_000_000_000)
    bb.normalise_tree(root, 1_760_000_000)
    entries = lib.scan_tree(root)
    again = lib.parse_manifest_text(lib.manifest_text(entries))
    assert (
        lib.diff_entries(
            [{k: v for k, v in e.items() if k != "size"} for e in entries], again
        )
        == []
    )
    assert any(e["path"] == "empty" and e["type"] == "dir" for e in again)
    assert any("日本語" in str(e["path"]) for e in again)


@pytest.mark.parametrize("name", ["a\\b", "a\nb", "a\rb"])
def test_manifest_refuses_names_that_sha256sum_would_mangle(name: str) -> None:
    with pytest.raises(lib.BundleError):
        lib.manifest_text(
            [{"path": name, "type": "file", "sha256": "0" * 64, "mode": "0644"}]
        )


def test_size_budget_boundary() -> None:
    lib.check_size_budget(100_000_000, 100, "agent")  # exactly at the limit passes
    with pytest.raises(lib.BundleError) as exc:
        lib.check_size_budget(100_000_001, 100, "agent")
    assert exc.value.reason == "size_budget"


# ------------------------------------------------------------ Windows launchers
@pytest.mark.parametrize("name", ["meshforge-agent", "compute"])
def test_windows_launcher_is_relocatable_isolated_crlf_and_exit_code_preserving(
    name: str,
) -> None:
    text = lib.launcher_text(lib.EntryPoint(name, "meshforge.agent.cli", "main"), WIN)
    assert text.startswith("@echo off\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert '"%~dp0..\\python\\python.exe" -I -c' in text  # isolated, own folder
    assert text.rstrip().endswith("exit /b %ERRORLEVEL%")
    assert " %*" in text
    assert "C:\\" not in text.replace("%~dp0", "")  # no absolute build path
    assert "/" not in text.split("\r\n")[2].split("-c", 1)[0]  # backslashes in the path
    assert text.encode("ascii")  # pure ASCII: no code-page surprises in cmd


@pytest.mark.parametrize(
    "bad",
    [
        lib.EntryPoint("x", "a.b", 'main"; import os #'),
        lib.EntryPoint("x", "a.b;import os", "main"),
        lib.EntryPoint('x" & calc & "', "a.b", "main"),
        lib.EntryPoint("x", "a.b", "main()"),
        lib.EntryPoint("x y", "a.b", "main"),
        lib.EntryPoint("%PATH%", "a.b", "main"),
    ],
)
def test_launcher_refuses_entry_points_that_could_inject_into_cmd(bad: object) -> None:
    with pytest.raises(lib.BundleError):
        lib.launcher_text(bad, WIN)
