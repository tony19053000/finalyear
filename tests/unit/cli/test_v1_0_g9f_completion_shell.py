"""V1.0 G9-F (D439): ``compute --install-completion`` finds PowerShell through
the installed ``compute.cmd`` launcher (lab L2 C8.10-g).

* :func:`pick_windows_shell` over fake Windows process trees: a ``cmd.exe /c``
  (the launcher, or ``cmd /c`` typed by the user) is walked past; an interactive
  cmd is the user's shell;
* the shell can be named after the option (``--install-completion powershell``);
* a shell without completion (cmd.exe, unknown) is one line + exit 4;
* on Windows (CI ``unit (windows-latest)``): a real ``.cmd`` launcher run from
  PowerShell prints the PowerShell script; run from an interactive cmd it gives
  the cmd.exe line.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer import _completion_shared as typer_completion
from typer import completion as typer_completion_options
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import completion, shell_detect
from meshforge.cli.shell_detect import (
    Proc,
    detect_shell,
    is_transient_cmd,
    pick_windows_shell,
)

pytestmark = pytest.mark.unit

_TIMEOUT_S = 120
PS_LAUNCH = (
    r'C:\WINDOWS\system32\cmd.exe /c ""C:\Program Files\MeshForge\bin\compute.cmd"'
    r' --install-completion"'
)
"""How Windows starts a ``.cmd`` that PowerShell runs (lab L2 C8.10-g)."""
PYTHON = Proc("python", None)


def ps(name: str = "powershell") -> Proc:
    return Proc(name, None)


def cmd(cmdline: str | None) -> Proc:
    return Proc("cmd", cmdline)


@pytest.fixture(autouse=True)
def _no_explicit_shell() -> Iterator[None]:
    token = completion._explicit_shell.set(None)
    yield
    completion._explicit_shell.reset(token)


# ----------------------------------------------------------- is_transient_cmd
@pytest.mark.parametrize(
    "cmdline",
    [
        PS_LAUNCH,
        r"cmd /c compute --install-completion",
        r"cmd.exe /C compute",
        r'"C:\WINDOWS\system32\cmd.exe" /d /s /c "compute"',
        r"cmd /d/c compute",
        r'"C:\WINDOWS\system32\cmd.exe"/c compute',
        r"cmd/c compute",
        r"cmd /r compute",
        r"cmd /e:on /v:on /c compute",
    ],
)
def test_cmd_started_with_c_is_transient(cmdline: str) -> None:
    assert is_transient_cmd(cmdline)


@pytest.mark.parametrize(
    "cmdline",
    [
        r'"C:\WINDOWS\system32\cmd.exe"',
        r"C:\WINDOWS\system32\cmd.exe",
        "cmd",
        "",
        r"cmd /k compute",
        r"cmd /K echo hi /c",
        r'cmd "C:\dir /c\x.cmd"',
        r'"C:\a /c b\cmd.exe"',
        r"cmd /d /q",
    ],
)
def test_interactive_cmd_is_not_transient(cmdline: str) -> None:
    assert not is_transient_cmd(cmdline)


# --------------------------------------------------------- pick_windows_shell
def test_powershell_through_the_launcher_is_powershell() -> None:
    """The lab L2 C8.10-g tree: powershell -> cmd /c compute.cmd -> python."""
    assert pick_windows_shell([cmd(PS_LAUNCH), ps(), Proc("explorer", None)]) == (
        "powershell"
    )


def test_pwsh_through_the_launcher_is_pwsh() -> None:
    tree = [cmd(PS_LAUNCH), ps("pwsh"), Proc("windowsterminal", None)]
    assert pick_windows_shell(tree) == "pwsh"


def test_interactive_cmd_is_cmd() -> None:
    """compute.cmd typed in cmd.exe runs inside that cmd: python's parent."""
    tree = [cmd(r'"C:\WINDOWS\system32\cmd.exe" '), Proc("explorer", None)]
    assert pick_windows_shell(tree) == "cmd"


def test_interactive_cmd_inside_powershell_is_cmd() -> None:
    """The user typed `cmd` in PowerShell, then `compute`: they are in cmd."""
    tree = [cmd("cmd"), ps(), Proc("explorer", None)]
    assert pick_windows_shell(tree) == "cmd"


def test_cmd_k_is_interactive() -> None:
    assert pick_windows_shell([cmd("cmd /k compute"), ps()]) == "cmd"


def test_launcher_inside_cmd_c_inside_powershell_is_powershell() -> None:
    """powershell -> cmd /c some.bat -> cmd /c compute.cmd -> python."""
    tree = [cmd(PS_LAUNCH), cmd(r"cmd /c C:\tools\run.bat"), ps()]
    assert pick_windows_shell(tree) == "powershell"


def test_launcher_inside_interactive_cmd_is_cmd() -> None:
    """interactive cmd -> cmd /c compute.cmd -> python: the user is in cmd."""
    tree = [cmd(PS_LAUNCH), cmd("cmd"), ps()]
    assert pick_windows_shell(tree) == "cmd"


def test_python_and_trampolines_are_walked_past() -> None:
    """A venv's python.exe redirector or a uv `compute.exe` trampoline sits
    between python and the shell (development install)."""
    tree = [PYTHON, Proc("compute", None), cmd(PS_LAUNCH), ps("pwsh")]
    assert pick_windows_shell(tree) == "pwsh"
    assert pick_windows_shell([PYTHON, Proc("uv", None), ps()]) == "powershell"


def test_git_bash_on_windows_is_bash() -> None:
    assert pick_windows_shell([Proc("bash", None), Proc("mintty", None)]) == "bash"


def test_no_shell_is_none() -> None:
    assert pick_windows_shell([]) is None
    assert pick_windows_shell([cmd(PS_LAUNCH), Proc("explorer", None)]) is None
    assert pick_windows_shell([Proc("services", None), Proc("wininit", None)]) is None


def test_only_the_first_eleven_ancestors_count() -> None:
    deep = [PYTHON] * (shell_detect.MAX_DEPTH + 1) + [ps()]
    assert pick_windows_shell(deep) is None
    assert pick_windows_shell([PYTHON] * shell_detect.MAX_DEPTH + [ps()]) == (
        "powershell"
    )


def test_unreadable_launcher_under_powershell_is_skipped() -> None:
    """Command line not readable: a cmd with PowerShell as the next shell is
    taken for the launcher - once."""
    assert pick_windows_shell([cmd(None), ps()]) == "powershell"
    assert pick_windows_shell([cmd(None), Proc("conhost", None), ps()]) == (
        "powershell"
    )
    assert pick_windows_shell([cmd(None), cmd(None), ps()]) == "cmd"


def test_unreadable_cmd_without_powershell_above_is_cmd() -> None:
    assert pick_windows_shell([cmd(None), Proc("explorer", None)]) == "cmd"
    assert pick_windows_shell([cmd(None), cmd("cmd")]) == "cmd"


# --------------------------------------------------------------- detect_shell
def test_detect_shell_on_windows_uses_the_tree() -> None:
    assert (
        detect_shell(platform="win32", ancestors=lambda: [cmd(PS_LAUNCH), ps()])
        == "powershell"
    )


def test_detect_shell_on_windows_snapshot_failure_is_none() -> None:
    def broken() -> list[Proc]:
        raise OSError("access denied")

    assert detect_shell(platform="win32", ancestors=broken) is None


def test_detect_shell_elsewhere_keeps_shellingham() -> None:
    assert detect_shell(platform="linux", fallback=lambda: "zsh") == "zsh"
    assert detect_shell(platform="darwin") is None


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX path")
def test_the_real_tree_reader_is_windows_only() -> None:
    assert shell_detect._windows_ancestors() == []
    assert shell_detect._command_line(os.getpid()) is None


# --------------------------------------------------------- explicit shell name
@pytest.mark.parametrize("option", completion.COMPLETION_OPTIONS)
@pytest.mark.parametrize("shell", ["powershell", "pwsh", "PowerShell", "cmd"])
def test_a_shell_named_after_the_option_is_used(
    option: str, shell: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(completion._TEST_SWITCH, raising=False)
    args = completion.take_explicit_shell(["--ca", "x.crt", option, shell])
    assert args == ["--ca", "x.crt", option]
    assert completion.current_shell() == shell.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["--install-completion"],
        ["--install-completion", "nodes"],
        ["nodes", "powershell"],
        ["run", "--", "powershell"],
        [],
    ],
)
def test_other_arguments_are_untouched(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(completion._TEST_SWITCH, raising=False)
    assert completion.take_explicit_shell(argv) == argv
    assert completion._explicit_shell.get() is None


def test_typers_test_switch_keeps_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(completion._TEST_SWITCH, "1")
    argv = ["--show-completion", "pwsh"]
    assert completion.take_explicit_shell(argv) == argv
    assert completion._explicit_shell.get() is None


def test_detected_when_not_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion, "detect_shell", lambda **_kw: "pwsh")
    assert completion.current_shell() == "pwsh"


# ------------------------------------------------------------- Typer wiring
def test_typer_looks_up_our_functions() -> None:
    """A Typer upgrade that stops looking these up by module global fails here
    instead of bringing back "Shell cmd is not supported."."""
    assert cli_app.app is not None
    assert typer_completion._get_shell_name is completion.current_shell
    assert typer_completion_options._get_shell_name is completion.current_shell
    assert typer_completion_options.install is completion.install
    assert typer_completion_options.get_completion_script is (
        completion._completion_script
    )
    source = Path(typer_completion_options.__file__).read_text(encoding="utf-8")
    assert "shell, path = install()" in source
    assert "detected_shell = _get_shell_name()" in source
    assert "script_content = get_completion_script(" in source


def _invoke(*args: str) -> tuple[int, str]:
    runner = CliRunner()
    result = runner.invoke(cli_app.app, list(args), prog_name="compute", env={})
    return result.exit_code, result.output + (result.stderr or "")


@pytest.fixture
def detected(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    monkeypatch.delenv(completion._TEST_SWITCH, raising=False)
    answer: list[str | None] = [None]
    monkeypatch.setattr(completion, "detect_shell", lambda **_kw: answer[0])
    return answer


def test_install_in_cmd_is_one_clear_line_exit_4(detected: list[str | None]) -> None:
    detected[0] = "cmd"
    code, out = _invoke("--install-completion")
    assert code == 4
    assert "not available in cmd.exe" in out
    assert "open PowerShell and run `compute --install-completion`" in out
    assert "Shell cmd is not supported" not in out
    assert "Traceback" not in out


def test_show_in_cmd_is_one_clear_line_exit_4(detected: list[str | None]) -> None:
    detected[0] = "cmd"
    code, out = _invoke("--show-completion")
    assert code == 4
    assert "not available in cmd.exe" in out


def test_undetected_shell_says_how_to_name_it(detected: list[str | None]) -> None:
    detected[0] = None
    code, out = _invoke("--install-completion")
    assert code == 4
    assert "compute --install-completion powershell" in out


def test_unsupported_shell_lists_the_supported(detected: list[str | None]) -> None:
    detected[0] = "nu"
    code, out = _invoke("--show-completion")
    assert code == 4
    assert "nu has no tab completion support" in out
    assert "bash, zsh, fish, powershell, pwsh" in out


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_show_in_powershell_prints_the_powershell_script(
    shell: str, detected: list[str | None]
) -> None:
    detected[0] = shell
    code, out = _invoke("--show-completion")
    assert code == 0, out
    assert "Register-ArgumentCompleter -Native -CommandName compute" in out
    assert '$Env:_COMPUTE_COMPLETE = "complete_powershell"' in out


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_install_in_powershell_goes_to_our_installer(
    shell: str, detected: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    detected[0] = shell
    calls: list[tuple[str, str, str]] = []

    def fake(*, prog_name: str, complete_var: str, shell: str) -> Path:
        calls.append((prog_name, complete_var, shell))
        return Path("profile.ps1")

    monkeypatch.setattr(typer_completion, "install_powershell", fake)
    code, out = _invoke("--install-completion")
    assert code == 0, out
    assert calls == [("compute", "_COMPUTE_COMPLETE", shell)]
    assert f"{shell} completion installed in profile.ps1" in out


def test_install_with_typers_test_switch_still_takes_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(completion._TEST_SWITCH, "1")
    with pytest.raises(SystemExit) as exited:
        completion.install(shell="cmd", prog_name="compute")  # noqa: S604
    assert exited.value.code == 4
    script = completion._completion_script(  # noqa: S604 - a shell name
        prog_name="compute", complete_var="_C", shell="bash"
    )
    assert "_C=complete_bash" in script


# --------------------------------------------- the console script, end to end
def _env(tmp_path: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("MESHFORGE_") and k != completion._TEST_SWITCH
    }
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)
    return env


def test_install_completion_cmd_named_exits_4(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.cli", "--install-completion", "cmd"],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 4, done.stderr
    assert "not available in cmd.exe" in done.stderr
    assert done.stderr.count("\n") == 1


@pytest.mark.skipif(sys.platform == "win32", reason="posix fake shell")
def test_install_completion_powershell_named_installs(tmp_path: Path) -> None:
    """The lab's second try, `compute --install-completion powershell`, from a
    POSIX shell: the named shell wins over detection."""
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    fake = tmp_path / "bin" / "powershell"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "{profile}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = _env(tmp_path)
    env["PATH"] = f"{fake.parent}{os.pathsep}{env['PATH']}"
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.cli", "--install-completion", "powershell"],
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "powershell completion installed" in done.stdout
    text = profile.read_text(encoding="utf-8")
    assert text.startswith(completion.MARKER)
    assert "Register-ArgumentCompleter -Native -CommandName compute" in text


# ------------------------------------------- Windows: a real .cmd launcher
def _launcher(tmp_path: Path) -> Path:
    """A ``compute.cmd`` shaped like the bundle's (bundle_lib.launcher_windows):
    cmd runs python with the arguments, python's parent is that cmd."""
    launcher = tmp_path / "bin" / "compute.cmd"
    launcher.parent.mkdir()
    launcher.write_bytes(
        (
            "@echo off\r\n"
            f'"{sys.executable}" -m meshforge.cli %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        ).encode()
    )
    return launcher


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_windows_launcher_from_powershell_shows_the_powershell_script(
    tmp_path: Path, shell: str
) -> None:  # pragma: no cover - Windows
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not installed")
    env = _env(tmp_path)
    if shell == "powershell":
        env.pop("PSModulePath", None)  # 5.1 started from pwsh (package_check.py)
    launcher = _launcher(tmp_path)
    done = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command",
         f"& '{launcher}' --show-completion"],
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT_S,
        check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Register-ArgumentCompleter -Native -CommandName compute" in done.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
def test_windows_launcher_from_interactive_cmd_says_use_powershell(
    tmp_path: Path,
) -> None:  # pragma: no cover - Windows
    launcher = _launcher(tmp_path)
    done = subprocess.run(
        f'cmd.exe /d /k ""{launcher}" --show-completion & exit"',
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert "not available in cmd.exe" in done.stderr, done.stdout + done.stderr
    assert "Register-ArgumentCompleter" not in done.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
def test_windows_real_tree_reader_sees_this_process_parent() -> (
    None
):  # pragma: no cover
    chain = shell_detect._windows_ancestors()
    assert chain, "no ancestors read"
    for proc in chain:
        if proc.name == "cmd":
            assert proc.cmdline is not None
