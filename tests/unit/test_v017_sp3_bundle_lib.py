"""Unit tests for the pure parts of the D384 bundle builder (V0.17 G0, SP3).

``scripts/bundle_lib.py``: OS layouts, launcher text, the relocation rewrites
(``#!`` header, wheel ``RECORD``, ``/install`` placeholder), the build-path leak
scan and the manifest (scan, diff, verify). The real build + relocation run is
``tests/component/lab/test_v017_sp3_bundle.py`` (slow).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load(name: str) -> ModuleType:
    """Import a script by path: ``scripts/`` is not a package (build_bundle
    imports its sibling ``bundle_lib``, so scripts/ goes on sys.path)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


lib = _load("bundle_lib")
build_bundle = _load("build_bundle")

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks/modes")
LINUX = lib.LINUX_X86_64
WINDOWS = lib.layout_for("Windows", "AMD64", experimental_windows=True)
AGENT = lib.EntryPoint("meshforge-agent", "meshforge.agent.main", "main")


# ---------------------------------------------------------------- layouts
def test_linux_layout_and_name() -> None:
    assert lib.layout_for("Linux", "x86_64", experimental_windows=False) is LINUX
    assert lib.bundle_name("0.1.0", LINUX, "x86_64") == "meshforge-0.1.0-linux-x86_64"
    assert LINUX.python_platform == "x86_64-manylinux_2_28"


def test_windows_layout_needs_the_experimental_flag() -> None:
    with pytest.raises(lib.BundleError) as caught:
        lib.layout_for("Windows", "AMD64", experimental_windows=False)
    assert caught.value.reason == "unsupported"
    assert WINDOWS.python_exe == "python/python.exe"
    assert WINDOWS.uv_name == "uv.exe"
    assert not WINDOWS.posix


@pytest.mark.parametrize(
    ("system", "machine"), [("Darwin", "arm64"), ("Linux", "aarch64")]
)
def test_unsupported_targets_are_refused(system: str, machine: str) -> None:
    with pytest.raises(lib.BundleError):
        lib.layout_for(system, machine, experimental_windows=True)


@pytest.mark.parametrize(
    ("raw", "norm"), [("AMD64", "x86_64"), ("x64", "x86_64"), ("arm64", "aarch64")]
)
def test_normalise_arch(raw: str, norm: str) -> None:
    assert lib.normalise_arch(raw) == norm


# -------------------------------------------------------------- launchers
def test_parse_entry_points_reads_console_scripts_only() -> None:
    text = (
        "[console_scripts]\n"
        "compute = meshforge.cli.app:main\n"
        "meshforge-agent = meshforge.agent.main:main\n"
        "\n[gui_scripts]\nignored = a.b:c\n"
    )
    found = lib.parse_entry_points(text)
    assert set(found) == {"compute", "meshforge-agent"}
    assert found["compute"] == lib.EntryPoint("compute", "meshforge.cli.app", "main")


def test_parse_entry_points_refuses_extras_syntax() -> None:
    with pytest.raises(lib.BundleError):
        lib.parse_entry_points("[console_scripts]\nx = a.b:c [extra]\n")


def test_posix_launcher_is_relative_and_isolated() -> None:
    text = lib.launcher_posix(AGENT, LINUX)
    assert text.startswith("#!/bin/sh\n")
    assert 'realpath -- "$0"' in text
    assert '"$here/../python/bin/python3.12" -I -c' in text
    assert 'sys.argv[0] = "meshforge-agent"' in text
    assert "from meshforge.agent.main import main" in text
    # the -c program sits in '...': it must not contain a single quote itself
    program = text.split(" -c '", 1)[1].rsplit('\' "$@"', 1)[0]
    assert "'" not in program
    # nothing absolute but /bin/sh
    assert "/home" not in text and "/tmp" not in text


def test_windows_launcher_uses_its_own_folder() -> None:
    text = lib.launcher_windows(AGENT, WINDOWS)
    assert '"%~dp0..\\python\\python.exe" -I -c "' in text
    assert "sys.argv[0] = 'meshforge-agent'" in text
    program = text.split(' -c "', 1)[1].rsplit('" %*', 1)[0]
    assert '"' not in program
    assert text.endswith("exit /b %ERRORLEVEL%\r\n")


@pytest.mark.parametrize(
    "entry",
    [
        lib.EntryPoint("x; rm -rf /", "a.b", "main"),
        lib.EntryPoint("ok", "a.b'; import os", "main"),
        lib.EntryPoint("ok", "a.b", "main()"),
    ],
)
def test_launcher_refuses_unsafe_entry_points(entry: object) -> None:
    with pytest.raises(lib.BundleError) as caught:
        lib.launcher_posix(entry, LINUX)
    assert caught.value.reason == "bad_entry_point"


# --------------------------------------------------------- shebang rewrite
PY = b"/build/out/.bundle-x/meshforge/python/bin/python3.12"
BODY = b"import sys\nfrom pkg import main\nsys.exit(main())\n"


def test_rewrite_plain_shebang() -> None:
    new = lib.rewrite_shebang(b"#!" + PY + b"\n" + BODY, PY, "python3.12")
    assert new == lib.trampoline("python3.12") + BODY
    assert PY not in new


def test_rewrite_sh_form_used_for_paths_with_spaces() -> None:
    spaced = b"/build/my out/python/bin/python3.12"
    data = b"#!/bin/sh\n'''exec' '" + spaced + b"' \"$0\" \"$@\"\n' '''\n" + BODY
    new = lib.rewrite_shebang(data, spaced, "python3.12")
    assert new == lib.trampoline("python3.12") + BODY


@pytest.mark.parametrize(
    "data",
    [
        b"\x7fELF\x02\x01",  # a binary (uv)
        b"#!/usr/bin/env python3\n" + BODY,  # not ours
        b"#!/other/python3.12\n" + BODY,  # another interpreter
        lib.trampoline("python3.12") + BODY,  # already relocatable
    ],
)
def test_rewrite_leaves_other_files_alone(data: bytes) -> None:
    assert lib.rewrite_shebang(data, PY, "python3.12") is None


def test_trampoline_is_valid_python_and_names_the_interpreter() -> None:
    header = lib.trampoline("python3.12").decode()
    compile(header + BODY.decode(), "x", "exec")  # sh trick = a Python string
    assert '/python3.12" "$0" "$@"' in header


def _record_hash(data: bytes) -> str:
    raw = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + raw.decode()


def test_update_record_rehashes_and_drops(tmp_path: Path) -> None:
    site = tmp_path / "lib" / "python3.12" / "site-packages"
    script = tmp_path / "bin" / "compute"
    direct = site / "pkg-1.0.dist-info" / "direct_url.json"
    record = (
        "pkg/__init__.py,sha256=abc,10\n"
        "../../../bin/compute,sha256=old,99\n"
        "pkg-1.0.dist-info/direct_url.json,sha256=def,5\n"
        "pkg-1.0.dist-info/RECORD,,\n"
    )
    new_script = b"#!/bin/sh\nnew\n"
    out = lib.update_record(record, site, {script: new_script, direct: None})
    assert out.splitlines() == [
        "pkg/__init__.py,sha256=abc,10",
        f"../../../bin/compute,{_record_hash(new_script)},{len(new_script)}",
        "pkg-1.0.dist-info/RECORD,,",
    ]
    assert lib.record_hash(new_script) == _record_hash(new_script)


# ---------------------------------------------------------------- leaks
def test_leak_needles_drop_generic_paths_and_sort_longest_first() -> None:
    needles = lib.leak_needles(["/home", "/tmp/", "/srv/build", "/srv/build/out"])
    assert b"/home" not in needles and b"/tmp" not in needles
    assert needles.index(b"/srv/build/out") < needles.index(b"/srv/build")


@posix_only
def test_find_leaks_in_text_binary_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "clean.py").write_text("x = 1\n")
    (root / "lib" / "text.cfg").write_text("prefix=/srv/build/out/python\n")
    (root / "lib" / "blob.so").write_bytes(b"\x00\x01/srv/build/out\x00")
    (root / "link").symlink_to("/srv/build/out/python/bin/uv")
    (root / "rel").symlink_to("lib/clean.py")
    leaks = lib.find_leaks(root, lib.leak_needles(["/srv/build/out"]))
    assert [leak.path for leak in leaks] == ["lib/blob.so", "lib/text.cfg", "link"]
    assert lib.find_leaks(root, lib.leak_needles(["/nowhere/at/all"])) == []


def test_replace_in_text_files_skips_binaries(tmp_path: Path) -> None:
    (tmp_path / "sysconfig.py").write_bytes(b"'prefix': '/cache/py/cpython'\n")
    (tmp_path / "python.bin").write_bytes(b"\x00/cache/py/cpython\x00")
    changed = lib.replace_in_text_files(tmp_path, b"/cache/py/cpython", b"/install")
    assert changed == ["sysconfig.py"]
    assert (tmp_path / "sysconfig.py").read_bytes() == b"'prefix': '/install'\n"
    assert b"/cache/py/cpython" in (tmp_path / "python.bin").read_bytes()


# -------------------------------------------------------------- manifest
def _tree(root: Path) -> None:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tool").write_text("#!/bin/sh\n", newline="\n")
    (root / "bin" / "tool").chmod(0o755)
    (root / "data.txt").write_text("hello\n", newline="\n")
    (root / "empty").mkdir()


@posix_only
def test_scan_tree_entries(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / "uv").symlink_to("bin/tool")
    (tmp_path / lib.MANIFEST_NAME).write_text("{}")  # never lists itself
    entries = lib.scan_tree(tmp_path)
    assert [e["path"] for e in entries] == ["bin/tool", "data.txt", "empty", "uv"]
    by_path = {e["path"]: e for e in entries}
    assert by_path["bin/tool"]["mode"] == "0755"
    assert by_path["data.txt"] == {
        "path": "data.txt",
        "type": "file",
        "size": 6,
        "sha256": hashlib.sha256(b"hello\n").hexdigest(),
        "mode": "0644",
    }
    assert by_path["empty"] == {"path": "empty", "type": "dir"}
    assert by_path["uv"] == {"path": "uv", "type": "symlink", "target": "bin/tool"}


def test_manifest_json_is_canonical(tmp_path: Path) -> None:
    _tree(tmp_path)
    one = lib.Manifest(meta={"b": 1, "a": 2}, files=lib.scan_tree(tmp_path))
    two = lib.Manifest(meta={"a": 2, "b": 1}, files=lib.scan_tree(tmp_path))
    assert one.to_json() == two.to_json()
    doc = json.loads(one.to_json())
    assert doc["format"] == lib.MANIFEST_FORMAT
    assert doc["total_bytes"] == len("#!/bin/sh\n") + len("hello\n")


def test_diff_entries_names_every_difference(tmp_path: Path) -> None:
    _tree(tmp_path)
    a = lib.scan_tree(tmp_path)
    assert lib.diff_entries(a, a) == []
    (tmp_path / "data.txt").write_text("changed\n")
    (tmp_path / "bin" / "tool").unlink()
    (tmp_path / "new.txt").write_text("x")
    b = lib.scan_tree(tmp_path)
    assert lib.diff_entries(a, b) == [
        "only in second: bin",  # now an empty directory: listed as such
        "only in first: bin/tool",
        "differs: data.txt (sha256)",
        "only in second: new.txt",
    ]
    assert lib.diff_meta({"v": 1}, {"v": 2}) == ["meta v: 1 != 2"]


@posix_only
def test_verify_bundle(tmp_path: Path) -> None:
    _tree(tmp_path)
    (tmp_path / lib.MANIFEST_NAME).write_text(
        lib.manifest_text(lib.scan_tree(tmp_path))
    )
    assert lib.verify_bundle(tmp_path) == []
    (tmp_path / "data.txt").write_text("tampered\n")
    assert lib.verify_bundle(tmp_path) == ["differs: data.txt (sha256)"]
    (tmp_path / "data.txt").write_text("hello\n")
    (tmp_path / "data.txt").chmod(0o755)
    assert lib.verify_bundle(tmp_path) == ["differs: data.txt (mode)"]


def test_verify_without_manifest_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(lib.BundleError) as caught:
        lib.verify_bundle(tmp_path)
    assert caught.value.reason == "no_manifest"


# ------------------------------------------------- build_bundle helpers
def test_locked_version_reads_uv_lock() -> None:
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert build_bundle.locked_version(lock, "uv") is not None
    assert build_bundle.locked_version(lock, "no-such-package") is None


def test_clean_env_drops_user_installer_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    env = build_bundle.clean_env({"UV_CACHE_DIR": "/c"})
    assert "UV_INDEX_URL" not in env and "PIP_INDEX_URL" not in env
    assert "PYTHONPATH" not in env
    assert env["UV_CACHE_DIR"] == "/c"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_every_role_has_launchers_and_extras() -> None:
    assert set(build_bundle.ROLE_EXTRAS) == set(build_bundle.ROLE_COMMANDS)
    for commands in build_bundle.ROLE_COMMANDS.values():
        assert "compute" in commands


def test_cli_diff_exit_codes(tmp_path: Path) -> None:
    _tree(tmp_path / "t")
    a = tmp_path / "a.sha256"
    b = tmp_path / "b.sha256"
    text = lib.manifest_text(lib.scan_tree(tmp_path / "t"))
    a.write_text(text)
    b.write_text(text)
    assert build_bundle.main(["--diff", str(a), str(b)]) == 0
    b.write_text(text.replace("  data.txt", "  other.txt"))
    assert build_bundle.main(["--diff", str(a), str(b)]) == 1
