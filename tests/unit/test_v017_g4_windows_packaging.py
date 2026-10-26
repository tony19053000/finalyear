"""V0.17 G4: the Windows bundle + installer build, and the installer script vs code.

Pure parts only (no Windows needed): the Windows bundle layout and launcher,
the console-script trampolines the bundle drops, the Windows forms of the leak
needles, the installer builder's names/defines/refusals, the Inno Setup pin,
and that ``packaging/windows/meshforge-agent.iss`` names exactly what the
Python side implements (AppId, service, shortcuts, helper commands). The
script really compiling is ``tests/component/packaging/test_v017_g4_iss_compiles.py``
(Inno Setup under Wine); running it is the ``package-windows`` CI job.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
ISS = (REPO / "packaging" / "windows" / "meshforge-agent.iss").read_text(
    encoding="utf-8"
)


def _load(name: str, path: Path) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bundle_lib = _load("bundle_lib", SCRIPTS / "bundle_lib.py")
build_bundle = _load("build_bundle", SCRIPTS / "build_bundle.py")
bwi = _load("build_windows_installer", SCRIPTS / "build_windows_installer.py")
vu = _load("verify_uninstall_g4", REPO / "lab" / "verify_uninstall.py")

WINDOWS = bundle_lib.layout_for("Windows", "AMD64", experimental_windows=True)


# ------------------------------------------------------------------ bundle
def test_windows_layout_is_the_python_build_standalone_one() -> None:
    assert WINDOWS.os_name == "windows"
    assert WINDOWS.python_exe == "python/python.exe"
    assert WINDOWS.site_packages == "python/Lib/site-packages"
    assert WINDOWS.launcher_suffix == ".cmd"
    assert WINDOWS.python_platform == "x86_64-pc-windows-msvc"
    assert not WINDOWS.posix


def test_windows_launcher_runs_the_bundle_python_isolated() -> None:
    entry = bundle_lib.EntryPoint("meshforge-agent", "meshforge.agent.main", "main")
    text = bundle_lib.launcher_windows(entry, WINDOWS)
    lines = text.split("\r\n")
    assert lines[0] == "@echo off"
    assert lines[2].startswith('"%~dp0..\\python\\python.exe" -I -c "')
    assert lines[2].endswith('" %*')
    assert "sys.argv[0] = 'meshforge-agent'" in lines[2]
    assert lines[3] == "exit /b %ERRORLEVEL%"
    # cmd's quoting: no double quote inside the -c program
    program = lines[2].split(' -c "', 1)[1].rsplit('" %*', 1)[0]
    assert '"' not in program


def test_script_names_reads_console_and_gui_scripts_only() -> None:
    text = (
        "[console_scripts]\nfoo = a:b\n  bar=c:d\n# x = y\n"
        "[gui_scripts]\nwin = e:f\n[other]\nnot_me = g:h\n"
    )
    assert bundle_lib.script_names(text) == {"foo", "bar", "win"}


def test_trampolines_and_build_shebang_scripts_are_dropped(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    scripts = root / WINDOWS.scripts_dir
    scripts.mkdir(parents=True)
    site = WINDOWS.site_packages_path(root)
    (site / "foo-1.0.dist-info").mkdir(parents=True)
    (site / "foo-1.0.dist-info" / "entry_points.txt").write_text(
        "[console_scripts]\nfoo = foo:main\n[gui_scripts]\nfoow = foo:gui\n"
    )
    python_abs = str(root / WINDOWS.python_exe)
    files = {
        "foo.exe": b"MZ trampoline",
        "FOO-script.py": b"import foo",
        "foow.pyw": b"",
        "tool.py": f"#!{python_abs.upper()}\nprint(1)\n".encode(),
        "uv.exe": b"MZ real binary",
        "notes.txt": b"#!/usr/bin/env python\n",
    }
    for name, data in files.items():
        (scripts / name).write_bytes(data)
    (scripts / "subdir").mkdir()
    dropped = {p.name for p in build_bundle.windows_scripts_to_drop(root, WINDOWS)}
    assert dropped == {"foo.exe", "FOO-script.py", "foow.pyw", "tool.py"}
    assert build_bundle.windows_scripts_to_drop(tmp_path / "none", WINDOWS) == []


def test_leak_needles_cover_every_windows_spelling_of_a_path() -> None:
    needles = set(bundle_lib.leak_needles([r"C:\Users\runner\work\x"]))
    for form in (
        r"C:\Users\runner\work\x",
        r"c:\Users\runner\work\x",
        "C:/Users/runner/work/x",
        "c:/Users/runner/work/x",
    ):
        assert form.encode() in needles
    # a POSIX path stays one needle (plus its realpath)
    assert b"/srv/build/meshforge" in set(
        bundle_lib.leak_needles(["/srv/build/meshforge"])
    )


def test_strip_windows_never_removes_what_the_agent_needs() -> None:
    patterns = [p for p, _why in build_bundle.STRIP_WINDOWS]
    for needed in ("Lib/site-packages", "DLLs/_ssl.pyd", "python.exe", "Lib/venv"):
        assert not any(Path(needed).match(p) or needed == p for p in patterns)
    assert all(why for _p, why in build_bundle.STRIP_WINDOWS)


def test_the_bundle_import_check_covers_every_pywin32_module_used() -> None:
    used: set[str] = set()
    for path in (REPO / "src" / "meshforge").rglob("*.py"):
        for match in re.finditer(
            r"^\s*import (win32\w+|pywintypes|servicemanager)|^from (win32\w+)",
            path.read_text(encoding="utf-8"),
            re.M,
        ):
            used.add(next(g for g in match.groups() if g))
    used.discard("win32com")  # the check imports win32com.shell explicitly
    check = build_bundle.WINDOWS_IMPORT_CHECK
    missing = {m for m in used if m not in check and m != "pywintypes"}
    assert not missing, f"add to WINDOWS_IMPORT_CHECK: {sorted(missing)}"


# ------------------------------------------------------------- installer
@pytest.mark.parametrize(
    ("version", "revision", "numeric"),
    [
        ("0.17.0", 1, "0.17.0.1"),
        ("0.17.0.dev3", 2, "0.17.0.2"),
        ("1", 1, "1.0.0.1"),
        ("0.17.0rc1+local", 7, "0.17.0.7"),
    ],
)
def test_numeric_version_fits_a_windows_file_version(
    version: str, revision: int, numeric: str
) -> None:
    assert bwi.numeric_version(version, revision) == numeric


def test_numeric_version_refusals() -> None:
    with pytest.raises(bwi.BundleError, match="no release number"):
        bwi.numeric_version("dev", 1)
    with pytest.raises(bwi.BundleError, match="does not fit"):
        bwi.numeric_version("70000.0.0", 1)


def test_installer_names_and_defines() -> None:
    names = bwi.installer_names("0.17.0", 2)
    assert names.version_dir == "0.17.0-2"
    assert names.output_name == "MeshForge-Agent-Setup-0.17.0-2"
    defines = bwi.iscc_defines(Path("C:/b"), "0.17.0", names, Path("C:/out"))
    keys = [d.split("=", 1)[0] for d in defines]
    assert keys == [
        "/DBundleDir",
        "/DAppVersion",
        "/DVersionDir",
        "/DNumericVersion",
        "/DOutputDir",
        "/DOutputName",
    ]
    # every define the script requires is passed
    for required in re.findall(r"#ifndef (\w+)\s+#error", ISS):
        assert f"/D{required}" in keys
    with pytest.raises(bwi.BundleError, match="revision"):
        bwi.installer_names("0.17.0", 0)
    with pytest.raises(bwi.BundleError, match="unsafe version"):
        bwi.installer_names('0.17"; x', 1)
    with pytest.raises(bwi.BundleError, match="double quote"):
        bwi.iscc_defines(Path('C:/a"b'), "0.17.0", names, Path("C:/out"))


def test_bundle_facts_require_a_windows_agent_bundle(tmp_path: Path) -> None:
    with pytest.raises(bwi.BundleError, match="not a built bundle"):
        bwi.bundle_facts(tmp_path)
    info = tmp_path / "BUILDINFO.json"
    info.write_text(json.dumps({"role": "agent", "os": "linux", "arch": "x86_64"}))
    with pytest.raises(bwi.BundleError, match="not a windows x86_64 agent"):
        bwi.bundle_facts(tmp_path)
    info.write_text(json.dumps({"role": "agent", "os": "windows", "arch": "x86_64"}))
    with pytest.raises(bwi.BundleError, match="no version"):
        bwi.bundle_facts(tmp_path)
    info.write_text(
        json.dumps(
            {"role": "agent", "os": "windows", "arch": "x86_64", "version": "0.1"}
        )
    )
    assert bwi.bundle_facts(tmp_path)["version"] == "0.1"


@pytest.mark.skipif(sys.platform == "win32", reason="the Windows refusal")
def test_the_installer_is_only_built_on_windows(tmp_path: Path) -> None:
    with pytest.raises(bwi.BundleError, match="built on Windows"):
        bwi.build(
            tmp_path, tmp_path, revision=1, cache=tmp_path, iscc=None, size_budget=True
        )
    assert bwi.main(["--bundle", str(tmp_path), "--out", str(tmp_path)]) == 1


def test_inno_setup_is_pinned_by_url_and_sha256() -> None:
    pins = bundle_lib.load_pins(REPO / "packaging" / "pins.toml")
    download = bundle_lib.pinned(pins, "innosetup", "windows-x86_64")
    assert re.fullmatch(r"[0-9a-f]{64}", download.sha256)
    assert download.url.startswith("https://github.com/jrsoftware/issrc/releases/")
    assert download.version.replace(".", "_") in download.url
    assert "latest" not in download.url


# ---------------------------------------------- the .iss names our code's facts
def _iss_define(name: str) -> str:
    match = re.search(rf'^#define {name} "(.*)"$', ISS, re.M)
    assert match is not None, name
    return match.group(1)


def test_iss_service_and_app_id_match_the_code_and_the_verifier() -> None:
    from meshforge.common.windows_service import SERVICE_NAME

    assert _iss_define("ServiceName") == SERVICE_NAME
    app_id = _iss_define("AppId")
    assert app_id == "{" + vu.WINDOWS_APP_ID  # "{{" escapes "{" in Inno Setup
    assert re.fullmatch(r"\{[0-9A-F-]{36}\}", vu.WINDOWS_APP_ID)
    assert vu.WINDOWS_SERVICE == SERVICE_NAME


def test_iss_is_per_machine_x64_admin_and_windows_10_22h2_or_newer() -> None:
    for line in (
        "PrivilegesRequired=admin",
        "ArchitecturesAllowed=x64os",
        "ArchitecturesInstallIn64BitMode=x64os",
        "MinVersion=10.0.19045",
        r"DefaultDirName={autopf}\MeshForge",
        "CloseApplications=no",
        "LicenseFile=license.txt",
    ):
        assert re.search(rf"^{re.escape(line)}$", ISS, re.M), line
    assert (REPO / "packaging" / "windows" / "license.txt").is_file()


def test_iss_shortcuts_run_the_console_launcher_and_the_docker_action() -> None:
    from meshforge.agent.console.checks_windows import DOCKER_ACCESS_MENU

    icons = [
        line for line in ISS.splitlines() if line.startswith('Name: "{autoprograms}')
    ]
    assert len(icons) == 2
    assert all(r"python\pythonw.exe" in line for line in icons)
    assert all("-I -m meshforge.agent.console.launcher" in line for line in icons)
    docker = next(line for line in icons if "--docker-access" in line)
    menu_name = DOCKER_ACCESS_MENU.rsplit(" -> ", 1)[1]
    assert f'\\{menu_name}"' in docker
    # the console's docker-access view names the same entry
    app = (REPO / "src/meshforge/agent/console/app.py").read_text(encoding="utf-8")
    assert f'menu_action="{menu_name}"' in app


def test_iss_helper_commands_exist_in_the_winservice_cli() -> None:
    from meshforge.agent import winservice as ws

    parser = ws._parser()
    setup = parser.parse_args(
        ["setup", "--result-file", "r.json", "--message-file", "m.txt"]
    )
    assert (setup.command, setup.result_file, setup.message_file) == (
        "setup",
        "r.json",
        "m.txt",
    )
    teardown = parser.parse_args(
        ["teardown", "--result-file", "u.json", "--remove-data"]
    )
    assert teardown.remove_data is True
    for words in (
        "-I -m meshforge.agent.winservice setup --result-file",
        "-I -m meshforge.agent.winservice teardown --result-file",
        "' --remove-data'",
        "/REMOVEDATA",
    ):
        assert words in ISS, words
    assert {"setup", "teardown"} <= ws.ADMIN_COMMANDS


def test_iss_open_meshforge_runs_only_after_a_healthy_setup_as_the_user() -> None:
    run = next(
        line
        for line in ISS.splitlines()
        if line.startswith("Filename:") and "postinstall" in line
    )
    assert "Check: ServiceHealthy" in run
    assert "runasoriginaluser" in run
    assert "skipifsilent" in run
    assert "Result := SetupRan and (SetupResultCode = 0);" in ISS
    # silent installs report the verdict: 100 + the helper's exit code
    assert "Result := 100 + SetupResultCode;" in ISS


def test_uninstall_deletes_only_a_folder_named_meshforge() -> None:
    assert re.search(r'^Type: filesandordirs; Name: "\{app\}"$', ISS, re.M)
    assert (
        "CompareText(ExtractFileName(RemoveBackslashUnlessRoot(Dir)), 'MeshForge')"
        in ISS
    )


# ------------------------------------------------------- verify_uninstall (win)
def _facts(*items: tuple[str, str]) -> list[tuple[str, str]]:
    return [("elevated", "True"), *items, ("done", "ok")]


def test_windows_clean_uninstall_keeps_data_by_default() -> None:
    verdict = vu.evaluate_windows(
        _facts(
            ("data", r"C:\ProgramData\MeshForge"),
            (
                "user_identity",
                r"C:\Users\a\AppData\Local\MeshForge\identity.migrated-1",
            ),
        ),
        data_removed=False,
    )
    assert verdict.clean
    assert len(verdict.kept) == 2


@pytest.mark.parametrize(
    "kind",
    [
        "service",
        "process",
        "task",
        "firewall",
        "program",
        "uninstall_entry",
        "path",
        "docker_member",
    ],
)
def test_windows_leftovers_are_never_clean(kind: str) -> None:
    verdict = vu.evaluate_windows(_facts((kind, "x")), data_removed=False)
    assert not verdict.clean


def test_windows_remove_all_data_must_remove_it() -> None:
    verdict = vu.evaluate_windows(
        _facts(("data", r"C:\ProgramData\MeshForge")), data_removed=True
    )
    assert not verdict.clean


def test_windows_probe_unfinished_or_not_elevated_is_never_clean() -> None:
    assert not vu.evaluate_windows([("elevated", "True")], data_removed=False).clean
    verdict = vu.evaluate_windows(
        [("elevated", "False"), ("done", "ok")], data_removed=False
    )
    assert not verdict.clean
    assert "not elevated" in verdict.leftovers[0]


def test_windows_probe_script_has_no_placeholder_left() -> None:
    from meshforge.common.windows_service import service_sid

    script = vu.windows_probe_script()
    assert "__" not in script.replace("__SERVICE__", "")
    assert vu.WINDOWS_APP_ID + "_is1" in script
    assert service_sid() in script
    assert vu.windows_service_sid() == service_sid()
    assert "verify_uninstall" in script.splitlines()[0]  # excludes itself


# ------------------------------------- test support: an external PostgreSQL
def test_external_postgres_is_read_from_the_environment_only_when_asked() -> None:
    from mf_postgres import external_pg_server

    assert external_pg_server({}) is None
    assert external_pg_server({"MESHFORGE_TEST_PG": "managed"}) is None
    server = external_pg_server(
        {
            "MESHFORGE_TEST_PG": "external",
            "MESHFORGE_TEST_PG_PORT": "5433",
            "MESHFORGE_TEST_PG_PASSWORD": "root",
        }
    )
    assert server is not None
    assert (server.host, server.port, server.user, server.admin_db) == (
        "127.0.0.1",
        5433,
        "postgres",
        "postgres",
    )
    assert server.password == "root"
