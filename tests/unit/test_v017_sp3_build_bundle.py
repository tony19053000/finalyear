"""Unit tests for the tool-running half of the D384 bundle builder (V0.17 G0, SP3).

``scripts/build_bundle.py``: each build step is run against a small fake tree (a
fake interpreter prefix, a fake site-packages, a stand-in ``uv`` that records its
argv) so the step's contract is pinned without downloading anything. The pure
helpers are in ``test_v017_sp3_bundle_lib.py``; the real build is the slow
component test ``tests/component/lab/test_v017_sp3_bundle.py``.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib.util
import json
import marshal
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load("bundle_lib")
bb = _load("build_bundle")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX layout only (SP3)")
LINUX = lib.LINUX_X86_64
EPOCH = 1_700_000_000


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def _fake_uv(tmp_path: Path, stdout: str = "") -> tuple[Path, Path]:
    """A stand-in ``uv``: writes its argv (one per line) to a file, prints
    ``stdout``. Returns (executable, argv file)."""
    argv_file = tmp_path / "uv-argv.txt"
    out_file = tmp_path / "uv-stdout.txt"
    out_file.write_text(stdout, encoding="utf-8")
    exe = tmp_path / "fake-uv"
    exe.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_file}'\ncat '{out_file}'\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return exe, argv_file


# ------------------------------------------------------------------ run()
def test_run_returns_stdout() -> None:
    out = bb.run(["/bin/sh", "-c", "echo hello"], env=os.environ, what="echo")
    assert out == "hello\n"


def test_run_failure_is_typed_with_the_stderr_tail() -> None:
    with pytest.raises(lib.BundleError) as info:
        bb.run(
            ["/bin/sh", "-c", "echo boom >&2; exit 7"], env=os.environ, what="the tool"
        )
    assert info.value.reason == "tool_failed"
    assert "the tool failed (exit 7)" in str(info.value)
    assert "boom" in str(info.value)


# ---------------------------------------------------- version + epoch inputs
def test_project_version_reads_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    assert bb.project_version(tmp_path) == "1.2.3"
    assert bb.project_version(REPO)  # the real one parses


def test_source_date_epoch_prefers_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1234")
    assert bb.source_date_epoch(tmp_path) == 1234


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_source_date_epoch_is_the_head_commit_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "@1600000000 +0000",
        "GIT_COMMITTER_DATE": "@1600000000 +0000",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for argv in (
        ["git", "init", "-q", str(tmp_path)],
        ["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"],
    ):
        subprocess.run(argv, env=env, check=True, capture_output=True)
    assert bb.source_date_epoch(tmp_path) == 1_600_000_000


def test_source_date_epoch_without_git_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.setattr(bb.shutil, "which", lambda _name: None)
    with pytest.raises(lib.BundleError) as info:
        bb.source_date_epoch(tmp_path)
    assert info.value.reason == "no_epoch"


def test_build_uv_is_the_lock_pin() -> None:
    pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")
    path, version = bb.build_uv()
    assert Path(path).exists()
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert version == bb.locked_version(lock, "uv")


# ------------------------------------------------------------ copy_python
def _fake_prefix(prefix: Path) -> None:
    """A python-build-standalone-like prefix after uv patched ``/install``."""
    files = {
        "bin/python3.12": b"\x7fELF fake interpreter",
        "bin/pip3": b"#!/bin/sh\n",
        "bin/idle3.12": b"#!/bin/sh\n",
        "lib/libpython3.12.so.1.0": b"\x7fELF shared",
        "lib/python3.12/os.py": b"# os\n",
        "lib/python3.12/__pycache__/os.cpython-312.pyc": b"stale",
        "lib/python3.12/tkinter/__init__.py": b"",
        "lib/python3.12/ensurepip/__init__.py": b"",
        "lib/python3.12/site-packages/pip/__init__.py": b"",
        "lib/python3.12/_sysconfigdata__linux_x86_64-linux-gnu.py": (
            f"build_time_vars = {{'prefix': '{prefix}', 'LIBDIR': '{prefix}/lib'}}\n"
        ).encode(),
        "lib/tcl8.6/init.tcl": b"",
        "share/man/man1/python3.1": b"",
        "EXTERNALLY-MANAGED": b"[externally-managed]\n",
    }
    for rel, data in files.items():
        path = prefix / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (prefix / "bin/python3").symlink_to("python3.12")


def test_copy_python_strips_and_restores_the_placeholder(tmp_path: Path) -> None:
    prefix = tmp_path / "cache dir" / "cpython-3.12.14"
    _fake_prefix(prefix)
    source = bb.PythonSource(prefix=prefix, version="3.12.14", url="", pbs_release="")
    dest = tmp_path / "bundle" / "python"
    bb.copy_python(source, dest, LINUX)

    assert (dest / "bin/python3.12").exists()
    assert (dest / "bin/python3").is_symlink()
    assert os.readlink(dest / "bin/python3") == "python3.12"
    assert (dest / "EXTERNALLY-MANAGED").exists()  # kept on purpose
    for gone in (
        "bin/pip3",
        "bin/idle3.12",
        "lib/libpython3.12.so.1.0",
        "lib/python3.12/tkinter",
        "lib/python3.12/ensurepip",
        "lib/python3.12/site-packages/pip",
        "lib/python3.12/__pycache__",
        "lib/tcl8.6",
        "share/man",
    ):
        assert not (dest / gone).exists(), gone
    sysconfig = (
        dest / "lib/python3.12/_sysconfigdata__linux_x86_64-linux-gnu.py"
    ).read_text()
    assert str(prefix) not in sysconfig
    assert f"'{lib.PLACEHOLDER}/lib'" in sysconfig
    assert lib.find_leaks(dest, lib.leak_needles([tmp_path])) == []


# ------------------------------------------------------------------ wheel
def _wheel(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def test_wheel_text_and_has(tmp_path: Path) -> None:
    wheel = _wheel(
        tmp_path / "meshforge-0-py3-none-any.whl",
        {
            "meshforge-0.dist-info/entry_points.txt": "[console_scripts]\nx = a:b\n",
            bb.DASHBOARD_INDEX: "<html>",
        },
    )
    assert bb.wheel_text(wheel, ".dist-info/entry_points.txt") == (
        "[console_scripts]\nx = a:b\n"
    )
    assert bb.wheel_text(wheel, ".dist-info/nothing") is None
    assert bb.wheel_has(wheel, bb.DASHBOARD_INDEX)
    assert not bb.wheel_has(wheel, "meshforge/other.html")


def test_requirements_pins_the_wheel_by_sha256(tmp_path: Path) -> None:
    uv, argv_file = _fake_uv(tmp_path, stdout="numpy==2.0 --hash=sha256:aa\n")
    wheel = _wheel(tmp_path / "meshforge-0-py3-none-any.whl", {"x": "y"})
    text = bb.requirements(str(uv), tmp_path, ("agent", "ml"), wheel, os.environ)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert text == (
        "numpy==2.0 --hash=sha256:aa\n"
        f"meshforge @ {wheel.as_uri()} --hash=sha256:{digest}\n"
    )
    argv = argv_file.read_text().splitlines()
    assert argv[:2] == ["export", "--frozen"]
    assert "--no-dev" in argv and "--no-emit-project" in argv
    assert argv.count("--extra") == 2 and "agent" in argv and "ml" in argv


def test_install_is_hash_checked_binary_only_for_a_fixed_platform(
    tmp_path: Path,
) -> None:
    uv, argv_file = _fake_uv(tmp_path)
    root = tmp_path / "bundle"
    bb.install(str(uv), root, LINUX, tmp_path / "req.txt", os.environ)
    argv = argv_file.read_text().splitlines()
    assert argv[:2] == ["pip", "install"]
    for flag in ("--require-hashes", "--no-deps", "--break-system-packages"):
        assert flag in argv
    assert argv[argv.index("--only-binary") + 1] == ":all:"
    assert argv[argv.index("--link-mode") + 1] == "copy"
    assert argv[argv.index("--python-platform") + 1] == "x86_64-manylinux_2_28"
    assert argv[argv.index("--python") + 1] == str(root / LINUX.python_exe)


# --------------------------------------------------------- relocate_installed
def test_relocate_installed_rewrites_scripts_and_drops_build_notes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build dir" / "bundle"
    site = LINUX.site_packages_path(root)
    info = site / "meshforge-0.dist-info"
    info.mkdir(parents=True)
    scripts = root / LINUX.scripts_dir
    scripts.mkdir(parents=True)
    python_abs = root / LINUX.python_exe
    script_body = b"import sys\nfrom meshforge.cli import main\nsys.exit(main())\n"
    script = f"#!{python_abs}\n".encode() + script_body
    (scripts / "compute").write_bytes(script)
    (scripts / "python3.12").write_bytes(b"\x7fELF")
    (scripts / "python3").symlink_to("python3.12")
    (info / "direct_url.json").write_text('{"url": "file:///build/x.whl"}')
    (info / "uv_cache.json").write_text('{"timestamp": 1}')
    (info / "METADATA").write_text("Name: meshforge\n")
    (info / "RECORD").write_text(
        "meshforge-0.dist-info/METADATA,,\n"
        f"meshforge-0.dist-info/direct_url.json,{_record_hash(b'x')},1\n"
        f"meshforge-0.dist-info/uv_cache.json,{_record_hash(b'y')},1\n"
        f"../../../bin/compute,{_record_hash(script)},{len(script)}\n"
    )

    changed = bb.relocate_installed(root, LINUX)

    assert changed == [
        "python/bin/compute",
        "python/lib/python3.12/site-packages/meshforge-0.dist-info/direct_url.json",
        "python/lib/python3.12/site-packages/meshforge-0.dist-info/uv_cache.json",
    ]
    assert not (info / "direct_url.json").exists()
    assert not (info / "uv_cache.json").exists()
    new_script = (scripts / "compute").read_bytes()
    assert str(tmp_path).encode() not in new_script
    assert new_script.endswith(script_body)
    assert (scripts / "python3").is_symlink()  # untouched
    record = (info / "RECORD").read_text()
    assert "direct_url.json" not in record and "uv_cache.json" not in record
    assert (
        f"../../../bin/compute,{_record_hash(new_script)},{len(new_script)}" in record
    )
    assert "meshforge-0.dist-info/METADATA,," in record
    # running it again is a no-op
    assert bb.relocate_installed(root, LINUX) == []


# ------------------------------------------------------------ compile_bytecode
def _compile_tree(root: Path) -> tuple[dict[str, bytes], list[str]]:
    """Compile a small fake stdlib under ``root`` with the real interpreter."""
    layout = dataclasses.replace(LINUX, python_exe="python/bin/py")
    stdlib = root / layout.stdlib_dir
    (stdlib / "pkg").mkdir(parents=True)
    (stdlib / "pkg" / "__init__.py").write_text(
        "X = frozenset({'a', 'b', 'c', 'd'})\ndef f():\n    return X\n"
    )
    (stdlib / "broken.py").write_text("def (:\n")
    (stdlib / "pkg" / "__pycache__").mkdir()
    (stdlib / "pkg" / "__pycache__" / "stale.cpython-312.pyc").write_bytes(b"x")
    (root / "python/bin").mkdir(parents=True)
    (root / layout.python_exe).symlink_to(os.path.realpath(sys.executable))
    bad = bb.compile_bytecode(root, layout, bb.clean_env({}))
    pycs = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*.pyc"))
    }
    return pycs, bad


def test_compile_bytecode_is_deterministic_and_path_free(tmp_path: Path) -> None:
    one, bad_one = _compile_tree(tmp_path / "first build")
    two, bad_two = _compile_tree(tmp_path / "second")
    assert one == two
    assert not any("stale" in name for name in one)
    assert len(one) == 1
    (pyc,) = one.values()
    # PEP 552 flags: bit 0 = hash-based, bit 1 = check_source (0 = unchecked)
    assert int.from_bytes(pyc[4:8], "little") == 0b01
    code = marshal.loads(pyc[16:])  # noqa: S302 - our own freshly compiled pyc
    assert not code.co_filename.startswith(str(tmp_path))
    assert code.co_filename.endswith("pkg/__init__.py")
    assert str(tmp_path).encode() not in pyc
    # root-relative (it goes into MANIFEST.json), identical across build dirs
    assert bad_one == bad_two == ["python/lib/python3.12/broken.py"]


# ------------------------------------------------------------ write_launchers
ENTRIES = {
    "meshforge-agent": lib.EntryPoint(
        "meshforge-agent", "meshforge.agent.main", "main"
    ),
    "compute": lib.EntryPoint("compute", "meshforge.cli.main", "main"),
}


def test_write_launchers_and_relative_uv_link(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / LINUX.scripts_dir).mkdir(parents=True)
    (root / LINUX.scripts_dir / "uv").write_bytes(b"\x7fELF uv")
    bb.write_launchers(root, LINUX, ENTRIES, "agent")
    for name in ("meshforge-agent", "compute"):
        path = root / "bin" / name
        assert path.stat().st_mode & 0o111
        assert path.read_text().startswith("#!/bin/sh")
    assert os.readlink(root / "uv") == "python/bin/uv"
    assert (root / "uv").read_bytes() == b"\x7fELF uv"


def test_write_launchers_without_uv_has_no_link(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / LINUX.scripts_dir).mkdir(parents=True)
    entries = {
        **ENTRIES,
        "meshforge-controller": lib.EntryPoint(
            "meshforge-controller", "meshforge.controller.main", "main"
        ),
    }
    bb.write_launchers(root, LINUX, entries, "controller")
    assert not (root / "uv").is_symlink()
    assert sorted(p.name for p in (root / "bin").iterdir()) == [
        "compute",
        "meshforge-controller",
    ]


def test_write_launchers_refuses_a_missing_entry_point(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    with pytest.raises(lib.BundleError) as info:
        bb.write_launchers(root, LINUX, {"compute": ENTRIES["compute"]}, "agent")
    assert info.value.reason == "bad_entry_point"


# -------------------------------------------------------------- normalise_tree
def test_normalise_tree_fixes_modes_and_mtimes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "d").mkdir(parents=True)
    (root / "d" / "exe").write_bytes(b"x")
    (root / "d" / "exe").chmod(0o700)
    (root / "d" / "data").write_bytes(b"y")
    (root / "d" / "data").chmod(0o600)
    (root / "link").symlink_to("d/exe")
    (root / "d").chmod(0o700)
    bb.normalise_tree(root, EPOCH)
    expect = {
        root: 0o755,
        root / "d": 0o755,
        root / "d" / "exe": 0o755,
        root / "d" / "data": 0o644,
    }
    for path, mode in expect.items():
        st = path.lstat()
        assert stat.S_IMODE(st.st_mode) == mode, path
        assert int(st.st_mtime) == EPOCH, path
    assert int((root / "link").lstat().st_mtime) == EPOCH


# --------------------------------------------------------------------- CLI
def test_parse_args_requires_out_for_a_build() -> None:
    with pytest.raises(SystemExit) as info:
        bb.parse_args(["--role", "agent"])
    assert info.value.code == 2


def test_parse_args_modes_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        bb.parse_args(["--role", "agent", "--verify", str(tmp_path)])
    with pytest.raises(SystemExit):
        bb.parse_args([])
    args = bb.parse_args(
        ["--role", "controller", "--out", str(tmp_path), "--extra", "ml"]
    )
    assert args.role == "controller" and args.extra == ["ml"]


def test_cli_verify_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "a.txt").write_text("hello")
    manifest = lib.Manifest(meta={}, files=lib.scan_tree(root))
    (root / lib.MANIFEST_NAME).write_text(manifest.to_json())
    assert bb.main(["--verify", str(root)]) == 0
    assert capsys.readouterr().out.strip().endswith("intact")
    (root / "a.txt").write_text("changed")
    assert bb.main(["--verify", str(root)]) == 1
    assert "a.txt" in capsys.readouterr().out
    assert bb.main(["--verify", str(tmp_path / "missing")]) == 1


def test_cli_build_error_exits_1_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_options: object) -> Path:
        raise lib.BundleError("nope", reason="path_leak")

    monkeypatch.setattr(bb, "build", fail)
    assert bb.main(["--role", "agent", "--out", str(tmp_path)]) == 1
    assert "error (path_leak): nope" in capsys.readouterr().err


# -------------------------------------------- build() guards before any work
def _pinned_uv(monkeypatch: pytest.MonkeyPatch, version: str | None) -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    pinned = version or bb.locked_version(lock, "uv")
    monkeypatch.setattr(bb, "build_uv", lambda: ("/nonexistent/uv", pinned))
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux layout")
def test_build_refuses_a_uv_that_is_not_the_lock_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pinned_uv(monkeypatch, "0.0.1")
    with pytest.raises(lib.BundleError) as info:
        bb.build(bb.BuildOptions(role="agent", out=tmp_path, cache=tmp_path / "c"))
    assert info.value.reason == "uv_mismatch"
    assert list(tmp_path.iterdir()) == []  # nothing written


@pytest.mark.skipif(sys.platform != "linux", reason="Linux layout")
def test_build_refuses_to_overwrite_without_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pinned_uv(monkeypatch, None)
    existing = tmp_path / lib.bundle_name(
        bb.project_version(REPO), LINUX, lib.normalise_arch("x86_64")
    )
    existing.mkdir()
    (existing / "keep").write_text("mine")
    with pytest.raises(lib.BundleError) as info:
        bb.build(bb.BuildOptions(role="agent", out=tmp_path, cache=tmp_path / "c"))
    assert info.value.reason == "exists"
    assert (existing / "keep").read_text() == "mine"


def test_manifest_meta_round_trips_as_json() -> None:
    manifest = lib.Manifest(meta={"role": "agent", "uncompiled": []}, files=[])
    assert json.loads(manifest.to_json())["role"] == "agent"
