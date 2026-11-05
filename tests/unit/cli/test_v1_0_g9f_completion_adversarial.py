"""V1.0 G9-F (D439) - independent adversarial tests (tester).

Derived from the G9-F brief, not from the implementation: shell detection over
odd ``cmd /c`` command lines and deep / cyclic-looking trees, the explicit
``--install-completion <shell>`` argv handling, exit-4 messages, Typer
monkeypatch robustness and the safe PowerShell installer.
"""

from __future__ import annotations

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
    MAX_DEPTH,
    Proc,
    detect_shell,
    is_transient_cmd,
    pick_windows_shell,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(completion._TEST_SWITCH, raising=False)
    token = completion._explicit_shell.set(None)
    yield
    completion._explicit_shell.reset(token)


def cmd(line: str | None) -> Proc:
    return Proc("cmd", line)


PY = Proc("python", None)
PS = Proc("powershell", None)
PWSH = Proc("pwsh", None)
LAUNCH = r'C:\WINDOWS\system32\cmd.exe /c ""C:\Program Files\x\compute.cmd" --show-completion"'


# --------------------------------------------------------- is_transient_cmd
@pytest.mark.parametrize(
    "line",
    [
        r"cmd /q /c x",
        r"CMD.EXE /E:ON /V:ON /C foo",
        'cmd.exe /C"x"',
        "cmd \t/c x",
        r"\\?\C:\Windows\System32\cmd.exe /c x",
        r'"C:\Win dows\cmd.exe" /s /r "x"',
        "cmd /c",
        "cmd /Cecho",
    ],
)
def test_transient_variants(line: str) -> None:
    assert is_transient_cmd(line)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "cmd",
        "cmd /",
        "cmd /x",
        "cmd -c foo",
        "cmd /k /c x",
        "cmd /s/k/c",
        '"C:\\Windows\\cmd.exe" /K "x /c y"',
        '"cmd.exe',  # unterminated quote
        'cmd.exe "/c" foo',  # a quoted switch is not a switch for our reader
        "cmd.exe foo /c",  # /c after a non-switch belongs to the command
        "cmd /d",
        "\x00",
    ],
)
def test_interactive_variants(line: str) -> None:
    assert not is_transient_cmd(line)


def test_huge_command_line_is_fast() -> None:
    big = "cmd " + "/d " * 50_000 + "/c x"
    assert is_transient_cmd(big)
    assert not is_transient_cmd("cmd " + "x" * 1_000_000 + " /c")


def test_unicode_paths_do_not_confuse_the_parser() -> None:
    assert is_transient_cmd('"C:\\Users\\\u00fcser\\\u6587\u4ef6\\cmd.exe" /c "x"')
    assert not is_transient_cmd('"C:\\Users\\\u00fcser\\cmd.exe" /k')


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G9F-T1: an unquoted forward-slash program path is split at its "
    "first '/', so its path segments are read as switches ('cmd.exe' -> /c)",
)
def test_forward_slash_program_path_interactive_cmd_is_not_transient() -> None:
    assert not is_transient_cmd("C:/Windows/System32/cmd.exe")


# ---------------------------------------------------------- pick_windows_shell
def test_chain_of_launchers_then_powershell() -> None:
    chain = [PY, cmd(LAUNCH), cmd("cmd /c x"), cmd("cmd /d /c y"), PS]
    assert pick_windows_shell(chain) == "powershell"


def test_empty_chain_and_non_shell_chain() -> None:
    assert pick_windows_shell([]) is None
    assert (
        pick_windows_shell([PY, Proc("explorer", None), Proc("conhost", None)]) is None
    )


def test_only_launchers_no_shell_above_is_none() -> None:
    assert pick_windows_shell([PY, cmd(LAUNCH), Proc("explorer", None)]) is None


def test_readable_empty_cmdline_is_interactive() -> None:
    assert pick_windows_shell([PY, cmd(""), PS]) == "cmd"


def test_unreadable_cmd_two_in_a_row_second_is_the_shell() -> None:
    chain = [PY, cmd(None), cmd(None), PS]
    assert pick_windows_shell(chain) == "cmd"


def test_unreadable_cmd_above_pwsh_is_skipped() -> None:
    assert pick_windows_shell([PY, cmd(None), PWSH]) == "pwsh"


def test_unreadable_cmd_with_nothing_above_is_cmd() -> None:
    assert pick_windows_shell([PY, cmd(None)]) == "cmd"


def test_unreadable_cmd_above_cmd_k_is_cmd() -> None:
    assert pick_windows_shell([PY, cmd(None), cmd("cmd /k")]) == "cmd"


def test_depth_limit_is_exact() -> None:
    filler = [PY] * MAX_DEPTH
    assert pick_windows_shell([*filler, PS]) == "powershell"  # index 10 counts
    assert pick_windows_shell([*filler, PY, PS]) is None  # index 11 does not


def test_very_deep_tree_terminates() -> None:
    assert pick_windows_shell([PY] * 100_000) is None


def test_git_bash_through_cmd_launcher_is_bash() -> None:
    assert pick_windows_shell([PY, cmd(LAUNCH), Proc("bash", None)]) == "bash"


def test_nearest_non_cmd_shell_wins() -> None:
    assert pick_windows_shell([PY, Proc("pwsh", None), PS]) == "pwsh"


def test_uppercase_names_are_not_shells() -> None:
    # Proc.name is documented lower-case; the reader lower-cases. Pin the contract.
    assert pick_windows_shell([Proc("PowerShell", None)]) is None


# ------------------------------------------------------------- detect_shell
@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd", "cygwin"])
def test_non_windows_never_walks_the_tree(platform: str) -> None:
    def boom() -> list[Proc]:
        raise AssertionError("must not read a process tree off Windows")

    assert (
        detect_shell(platform=platform, ancestors=boom, fallback=lambda: "zsh") == "zsh"
    )
    assert detect_shell(platform=platform, ancestors=boom) is None
    assert (
        detect_shell(platform=platform, ancestors=boom, fallback=lambda: None) is None
    )


def test_windows_oserror_reading_tree_is_undetected() -> None:
    def fail() -> list[Proc]:
        raise OSError(5, "denied")

    assert (
        detect_shell(platform="win32", ancestors=fail, fallback=lambda: "bash") is None
    )


def test_windows_does_not_consult_shellingham() -> None:
    def fb() -> str | None:
        raise AssertionError("shellingham must not be used on Windows")

    assert (
        detect_shell(
            platform="win32", ancestors=lambda: [PY, cmd(LAUNCH), PS], fallback=fb
        )
        == "powershell"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="stand-ins are non-Windows")
def test_standins_are_harmless() -> None:
    assert shell_detect._windows_ancestors() == []
    assert shell_detect._command_line(0) is None


# --------------------------------------------------- take_explicit_shell
def test_does_not_mutate_input_and_returns_copy() -> None:
    argv = ["--install-completion", "pwsh", "x"]
    out = completion.take_explicit_shell(argv)
    assert argv == ["--install-completion", "pwsh", "x"]
    assert out == ["--install-completion", "x"]
    assert out is not argv


def test_real_arguments_after_the_shell_survive() -> None:
    out = completion.take_explicit_shell(
        ["--show-completion", "bash", "nodes", "--json"]
    )
    assert out == ["--show-completion", "nodes", "--json"]


def test_only_the_adjacent_name_is_taken() -> None:
    out = completion.take_explicit_shell(["--show-completion", "nodes", "bash"])
    assert out == ["--show-completion", "nodes", "bash"]
    assert completion._explicit_shell.get() is None


@pytest.mark.parametrize(
    "name",
    [
        "powershell;calc",
        "powershell.exe",
        "pwsh ",
        " pwsh",
        "",
        "po\u0308wershell",
        "-c",
        "$(id)",
        "../bash",
    ],
)
def test_hostile_or_odd_names_are_not_taken(name: str) -> None:
    argv = ["--install-completion", name]
    assert completion.take_explicit_shell(argv) == argv
    assert completion._explicit_shell.get() is None


def test_option_then_option_then_shell() -> None:
    out = completion.take_explicit_shell(
        ["--install-completion", "--show-completion", "cmd"]
    )
    assert out == ["--install-completion", "--show-completion"]
    assert completion.current_shell() == "cmd"


def test_option_repeated_only_first_named_is_removed() -> None:
    out = completion.take_explicit_shell(
        ["--show-completion", "bash", "--show-completion", "zsh"]
    )
    assert out == ["--show-completion", "--show-completion", "zsh"]
    assert completion.current_shell() == "bash"


def test_equals_form_is_left_to_typer() -> None:
    argv = ["--install-completion=powershell"]
    assert completion.take_explicit_shell(argv) == argv


def test_empty_and_single() -> None:
    assert completion.take_explicit_shell([]) == []
    assert completion.take_explicit_shell(["--show-completion"]) == [
        "--show-completion"
    ]


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G9F-T2: after '--' (arguments for a user command) a shell name "
    "following '--show-completion' is still removed from argv",
)
def test_after_double_dash_nothing_is_taken() -> None:
    argv = ["run", "--", "tool", "--show-completion", "bash"]
    assert completion.take_explicit_shell(argv) == argv


def test_current_shell_prefers_the_named_over_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "detect_shell", lambda **_k: "cmd")
    completion.take_explicit_shell(["--install-completion", "PWSH"])
    assert completion.current_shell() == "pwsh"


# ----------------------------------------------- CLI behaviour, in process
def _invoke(*args: str) -> tuple[int, str]:
    r = CliRunner().invoke(cli_app.app, list(args), prog_name="compute", env={})
    return r.exit_code, r.output + (r.stderr or "")


def test_show_completion_powershell_named_through_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "detect_shell", lambda **_k: "cmd")
    monkeypatch.setattr(sys, "argv", ["compute", "--show-completion", "powershell"])
    captured: list[list[str]] = []
    monkeypatch.setattr(
        cli_app, "app", lambda args, prog_name: captured.append(list(args))
    )
    cli_app.main()
    assert captured == [["--show-completion"]]
    assert completion.current_shell() == "powershell"


def test_unknown_named_shell_exit_4_not_ignored() -> None:
    """`--show-completion nu` (a real shell without completion) is exit 4."""
    token = completion._explicit_shell.set("nu")
    try:
        code, out = _invoke("--show-completion")
    finally:
        completion._explicit_shell.reset(token)
    assert code == 4
    assert "nu has no tab completion support" in out


def test_exit4_messages_are_single_line_and_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(completion, "detect_shell", lambda **_k: "evil\x1b[31m\nshell")
    code, out = _invoke("--show-completion")
    assert code == 4
    assert "\x1b" not in out
    assert len(set(out.strip().splitlines())) == 1  # runner echoes stderr twice
    assert "Traceback" not in out


@pytest.mark.parametrize("opt", ["--install-completion", "--show-completion"])
@pytest.mark.parametrize("shell", [None, "cmd", "nu", "xonsh"])
def test_every_unsupported_shell_exits_4(
    opt: str, shell: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(completion, "detect_shell", lambda **_k: shell)
    code, out = _invoke(opt)
    assert code == 4, out
    assert out.strip().startswith("compute:")


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G9F-T3: a mistyped shell name (--show-completion powerhsell) is "
    "silently ignored and the detected shell is used instead",
)
def test_mistyped_shell_name_is_an_error() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.cli", "--show-completion", "powerhsell"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode != 0


# -------------------------------------------------- Typer monkeypatch robustness
def test_use_safe_installers_is_idempotent() -> None:
    completion.use_safe_installers()
    completion.use_safe_installers()
    assert typer_completion.install_powershell is completion.install_powershell
    assert typer_completion.install_bash is completion.install_bash
    assert typer_completion_options.install is completion.install
    assert typer_completion_options._get_shell_name is completion.current_shell


def test_originals_were_captured_before_patching() -> None:
    assert completion._typer_install is not completion.install
    assert completion._typer_get_completion_script is not completion._completion_script
    assert completion._typer_get_shell_name is not completion.current_shell


def test_install_with_unsupported_explicit_shell_never_calls_typer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_k: object) -> tuple[str, Path]:
        raise AssertionError("typer install must not run")

    monkeypatch.setattr(completion, "_typer_install", boom)
    with pytest.raises(SystemExit) as e:
        completion.install(shell="cmd", prog_name="compute")  # noqa: S604
    assert e.value.code == 4


# ------------------------------------------------------ PowerShell installer
class _Run:
    """Fake subprocess.run recording every argv."""

    def __init__(self, stdout: bytes = b"", exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.exc = exc

    def __call__(
        self, argv: list[str], **_k: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr=b"")


def _install(
    profile: Path, monkeypatch: pytest.MonkeyPatch, shell: str = "powershell"
) -> _Run:
    fake = _Run(str(profile).encode() + b"\r\n")
    monkeypatch.setattr(completion.subprocess, "run", fake)
    completion.install_powershell(
        prog_name="compute", complete_var="_COMPUTE_COMPLETE", shell=shell
    )
    return fake


def test_install_twice_one_block_and_bytes_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "Documents" / "WindowsPowerShell" / "p.ps1"
    _install(profile, monkeypatch)
    first = profile.read_bytes()
    _install(profile, monkeypatch)
    _install(profile, monkeypatch)
    assert profile.read_bytes() == first
    assert profile.read_text(encoding="utf-8").count(completion.MARKER) == 1


def test_never_runs_set_executionpolicy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install(tmp_path / "p.ps1", monkeypatch)
    assert len(fake.calls) == 1
    assert all("executionpolicy" not in a.lower() for c in fake.calls for a in c)
    assert fake.calls[0][0] == "powershell"


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_profile_query_is_a_fixed_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str
) -> None:
    fake = _install(tmp_path / "p.ps1", monkeypatch, shell)
    assert fake.calls == [[shell, "-NoProfile", "-Command", "echo", "$profile"]]


def test_existing_profile_without_newline_is_kept_and_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "p.ps1"
    p.write_bytes(b"Set-Alias ll ls")
    _install(p, monkeypatch)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("Set-Alias ll ls\n" + completion.MARKER)


def test_existing_crlf_and_invalid_utf8_profile_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "p.ps1"
    original = b"# caf\xe9\r\nSet-Alias a b\r\n"
    p.write_bytes(original)
    _install(p, monkeypatch)
    assert p.read_bytes().startswith(original)
    before = p.read_bytes()
    _install(p, monkeypatch)
    assert p.read_bytes() == before


def test_marker_already_present_profile_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "p.ps1"
    p.write_bytes((completion.MARKER + "\nold\n").encode())
    _install(p, monkeypatch)
    assert p.read_text() == completion.MARKER + "\nold\n"


def test_unicode_profile_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "Dokumente \u00fc\u6587" / "p.ps1"
    _install(p, monkeypatch)
    assert completion.MARKER in p.read_text(encoding="utf-8")


def test_profile_is_a_directory_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "p.ps1"
    p.mkdir()
    with pytest.raises(SystemExit) as e:
        _install(p, monkeypatch)
    assert e.value.code == 4
    assert "cannot install powershell completion" in capsys.readouterr().err


def test_profile_parent_is_a_file_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "blocker").write_text("x")
    with pytest.raises(SystemExit) as e:
        _install(tmp_path / "blocker" / "p.ps1", monkeypatch)
    assert e.value.code == 4


@pytest.mark.parametrize(
    "fake",
    [
        _Run(exc=FileNotFoundError("powershell")),
        _Run(exc=subprocess.TimeoutExpired("powershell", 30)),
        _Run(exc=subprocess.CalledProcessError(1, "powershell")),
        _Run(exc=PermissionError("no")),
        _Run(stdout=b""),
        _Run(stdout=b"  \r\n"),
    ],
)
def test_profile_query_failures_exit_4(
    fake: _Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(completion.subprocess, "run", fake)
    with pytest.raises(SystemExit) as e:
        completion.install_powershell(  # noqa: S604
            prog_name="compute", complete_var="_C", shell="powershell"
        )
    assert e.value.code == 4
    err = capsys.readouterr().err
    assert "--show-completion" in err
    assert "Traceback" not in err


def test_cp1252_profile_path_is_decoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "caf\u00e9" / "p.ps1"
    monkeypatch.setattr(
        completion.subprocess, "run", _Run(str(target).encode("cp1252") + b"\r\n")
    )
    got = completion._profile_path("powershell")
    assert got == target


def test_note_does_not_execute_policy_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _install(tmp_path / "p.ps1", monkeypatch)
    cap = capsys.readouterr()
    assert "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" in cap.out + cap.err
    assert len(fake.calls) == 1


def test_installer_source_has_no_policy_execution() -> None:
    src = Path(completion.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in src
    # the only mention is the printed hint
    assert src.count("Set-ExecutionPolicy") == src.count(
        "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
    ) + src.count("Set-ExecutionPolicy Unrestricted")
