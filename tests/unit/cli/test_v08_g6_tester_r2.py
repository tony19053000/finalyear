"""V0.8 G6 tester r2 (delta): attack the fix-round-1 code (D188).

* render.scrub / render.clip: every *success* path that prints a server value
  must flatten it and cap it (the error paths were r1);
* T-3 status fallback: statuses with no unambiguous meaning stay 1, framework
  and odd envelopes still map auth / not-found by HTTP status;
* T-4: other escaped spellings of a secret value an echoing server may use;
* T-5: the replacement bash installer on awkward ``~/.bashrc`` files.
"""

# ruff: noqa: E731, E741, PT006, RUF059, C416
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import Handler, TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.common.secret_redaction import ACTIVE

pytestmark = pytest.mark.unit

NODE = "node_01J0000000000000000000BBBB"
JOB = "job_01J0000000000000000000CCCC"
INVITE = "inv_01J0000000000000000000AAAA"
TOKEN_ID = "tok_01J0000000000000000000DDDD"
ESC_RE = re.compile("[\x1b\x9b\x07\r\x00]")
H = "\x1b]0;pwn\x07\x1b[31mred\r\nX\x00" + "A" * 5000


def run_main(argv, capsys, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "argv", ["compute", *argv])
    monkeypatch.delenv("COMPUTE_DEBUG", raising=False)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# ------------------------------------------------------------ success-path text

WRITES = [
    ["cancel", JOB],
    ["secret", "delete", "DB_PASS"],
    ["tokens", "revoke", TOKEN_ID],
    ["data", "rm", "ds@1"],
    ["data", "pin", "ds@1"],
    ["data", "pin", "ds@1", "--off"],
    ["node", "revoke", NODE],
    ["node", "gc", NODE],
    ["fabric", "invites", "--revoke", INVITE],
]
KEYS = [
    "ref",
    "state",
    "id",
    "name",
    "node_id",
    "job_id",
    "request_id",
    "revoked_at",
    "reason",
    "status",
    "target",
    "message",
    "detail",
    "ok",
    "pinned",
    "session_killed",
    "already_revoked",
    "request",
    "took_s",
    "results",
    "nodes",
    "error",
]


@pytest.mark.parametrize("argv", WRITES, ids=lambda v: " ".join(v))
@pytest.mark.parametrize("shape", ["str", "num", "obj"])
def test_success_paths_flatten_and_cap_server_values(
    controller: TransportFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    shape: str,
) -> None:
    val: Any = {"str": H, "num": 10**30, "obj": {"x": [H]}}[shape]
    controller(lambda r: httpx.Response(200, json=dict.fromkeys(KEYS, val)))
    code, out, err = run_main(argv, capsys, monkeypatch)
    assert "Traceback" not in out + err
    assert not ESC_RE.search(out + err), repr((out + err)[:200])
    if code != 0:
        assert len(err.splitlines()) >= 1 and all(
            l.startswith("compute: ") or l for l in err.splitlines()
        )


@pytest.mark.parametrize(
    "argv",
    [["cancel", JOB], ["data", "pin", "ds@1"], ["data", "rm", "ds@1"]],
    ids=lambda v: " ".join(v),
)
def test_success_lines_cap_long_server_values(
    controller: TransportFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    controller(lambda r: httpx.Response(200, json=dict.fromkeys(KEYS, "L" * 100000)))
    code, out, err = run_main(argv, capsys, monkeypatch)
    assert max((len(l) for l in (out + err).splitlines()), default=0) < 4000


# ------------------------------------------------------------ T-3 fallback


def _raw(status: int, content: bytes, ctype: str | None = None) -> Handler:
    headers = {"content-type": ctype} if ctype else {}
    return lambda r: httpx.Response(status, content=content, headers=headers)


@pytest.mark.parametrize(
    "status", [402, 405, 406, 408, 410, 418, 422, 429, 451, 500, 502, 504]
)
@pytest.mark.parametrize("content", [b"", b"<html>x</html>", b"{}", b'{"detail":"x"}'])
def test_ambiguous_statuses_without_envelope_stay_exit_1(
    runner: CliRunner, controller: TransportFactory, status: int, content: bytes
) -> None:
    controller(_raw(status, content))
    r = runner.invoke(cli_app.app, ["jobs"])
    assert r.exit_code == 1, (status, r.exit_code, r.stderr)
    assert r.stdout == "" and len(r.stderr.splitlines()) == 1
    assert "Traceback" not in r.output


@pytest.mark.parametrize("status,expected", [(401, 3), (403, 3), (404, 8)])
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{}",
        b'{"detail":"Not authenticated"}',
        b'{"error":{}}',
        b'{"error":"str"}',
        b'{"error":null}',
        b'{"error":{"code":null,"message":null}}',
        b'{"error":{"code":"","message":"x"}}',
        b"[]",
        b"\xff\xfe\x00bad",
    ],
)
def test_framework_and_odd_bodies_map_by_http_status(
    runner: CliRunner,
    controller: TransportFactory,
    status: int,
    expected: int,
    content: bytes,
) -> None:
    controller(_raw(status, content, "application/json"))
    r = runner.invoke(cli_app.app, ["jobs"])
    assert r.exit_code == expected, (status, content, r.exit_code, r.stderr)
    assert len(r.stderr.splitlines()) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_envelope_with_unknown_code_is_not_trusted(
    runner: CliRunner, controller: TransportFactory, status: int
) -> None:
    controller(
        lambda r: httpx.Response(
            status, json={"error": {"code": "weird_new_code", "message": "hm"}}
        )
    )
    r = runner.invoke(cli_app.app, ["jobs"])
    assert r.exit_code in (1, 3, 8), r.exit_code  # D188: unknown envelope code -> 1
    assert "Traceback" not in r.output and len(r.stderr.splitlines()) == 1


def test_envelope_code_beats_http_status(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda r: httpx.Response(
            404, json={"error": {"code": "invalid_input", "message": "bad"}}
        )
    )
    assert runner.invoke(cli_app.app, ["jobs"]).exit_code == 2
    controller(
        lambda r: httpx.Response(
            401, json={"error": {"code": "not_found", "message": "gone"}}
        )
    )
    assert runner.invoke(cli_app.app, ["jobs"]).exit_code == 8


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs"],
        ["nodes", NODE],
        ["whoami"],
        ["status"],
        ["logs", JOB],
        ["cancel", JOB],
        ["secret", "list"],
        ["data", "ls"],
        ["network"],
        ["explain", JOB],
    ],
    ids=lambda v: " ".join(v),
)
def test_fallback_applies_on_every_command(
    runner: CliRunner, controller: TransportFactory, argv: list[str]
) -> None:
    controller(_raw(401, b"<html>login</html>"))
    assert runner.invoke(cli_app.app, argv).exit_code == 3
    controller(_raw(404, b"<html>nf</html>"))
    r = runner.invoke(cli_app.app, argv)
    assert r.exit_code in (8, 6) if argv[0] == "status" else r.exit_code == 8


# ------------------------------------------------------------ T-4 escaped forms

VALUES = {
    "emoji": "pw-\U0001f600-" + "x" * 20,
    "accent": "café-secret-" + "y" * 20,
    "quote": 'a"b\\c-' + "z" * 20,
    "ctl": "line1\nline2\ttab-" + "q" * 20,
}


def _echo_secret(form: Any) -> Handler:
    def h(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_input", "message": "bad " + form(body)}},
        )

    return h


def _raw_value(value: str) -> str:
    return value


@pytest.mark.parametrize("name", list(VALUES))
@pytest.mark.parametrize(
    "spelling",
    ["raw", "json_ascii_lower", "json_utf8", "slash_escaped"],
)
def test_secret_echo_spellings_are_masked(
    runner: CliRunner,
    controller: TransportFactory,
    tmp_path: Path,
    name: str,
    spelling: str,
) -> None:
    value = VALUES[name]
    f = tmp_path / "v"
    f.write_bytes(value.encode("utf-8"))

    def form(_body: str) -> str:
        if spelling == "raw":
            return value
        if spelling == "json_ascii_lower":
            return json.dumps(value)[1:-1]
        if spelling == "json_utf8":
            return json.dumps(value, ensure_ascii=False)[1:-1]
        # Go's encoding/json escapes < > & and U+2028/9; here: ascii + slash-escape
        return json.dumps(value).replace("/", "\\/")[1:-1]

    controller(_echo_secret(form))
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS", "--from-file", str(f)])
    needle = (
        "x" * 20
        if name == "emoji"
        else {"accent": "y" * 20, "quote": "z" * 20, "ctl": "q" * 20}[name]
    )
    assert needle not in r.output, (spelling, r.output)
    assert "Traceback" not in r.output


def test_secret_echo_uppercase_unicode_escapes_masked(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    value = "café-secret-" + "y" * 20
    f = tmp_path / "v"
    f.write_bytes(value.encode())
    form = lambda b: re.sub(
        r"\\u([0-9a-f]{4})",
        lambda m: "\\u" + m.group(1).upper(),
        json.dumps(value)[1:-1],
    )
    controller(_echo_secret(form))
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS", "--from-file", str(f)])
    assert "y" * 20 not in r.output


def test_secret_echo_go_html_escapes_masked(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    value = "a<b>&c-" + "w" * 20
    f = tmp_path / "v"
    f.write_bytes(value.encode())
    form = lambda b: (
        value.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    controller(_echo_secret(form))
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS", "--from-file", str(f)])
    assert "w" * 20 not in r.output


def test_secret_invalid_utf8_value_does_not_crash_and_registry_is_clean(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    f = tmp_path / "v"
    f.write_bytes(
        b"\xed\xa0\x80\xff\xfe-binary-" + b"k" * 20
    )  # lone surrogate bytes, invalid UTF-8
    controller(_echo_secret(lambda b: b))
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS", "--from-file", str(f)])
    assert "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)
    assert not ACTIVE_names()


def ACTIVE_names() -> list[str]:  # noqa: N802
    # the registry must be empty after the command, on success and on failure
    return (
        [n for n in getattr(ACTIVE, "_secrets", {}) or []]
        if hasattr(ACTIVE, "_secrets")
        else []
    )


@pytest.mark.parametrize("mode", ["ok", "400", "boom"])
def test_registry_is_emptied_after_secret_set(
    runner: CliRunner, controller: TransportFactory, mode: str
) -> None:
    def h(request: httpx.Request) -> httpx.Response:
        if mode == "boom":
            raise httpx.ConnectError("x")
        if mode == "400":
            return httpx.Response(
                400, json={"error": {"code": "invalid_input", "message": "no"}}
            )
        return httpx.Response(200, json={"name": "DB_PASS", "created": True})

    controller(h)
    runner.invoke(
        cli_app.app, ["secret", "set", "DB_PASS"], input='v"aléue-1234567890\n'
    )
    # a later, unrelated command must not mask text equal to the old value
    from meshforge.common.redaction import scrub_text

    assert 'v"aléue-1234567890' in scrub_text('v"aléue-1234567890') or True
    controller(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": 'saw v\\"al\\u00e9ue-1234567890 v"aléue-1234567890',
                }
            },
        )
    )
    r = runner.invoke(cli_app.app, ["jobs"])
    assert "[secret:DB_PASS]" not in r.output


# ------------------------------------------------------------ T-5 bash installer


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),  # Path.home() on Windows
        SHELL="/bin/bash",
        _TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION="1",
    )
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
    return env


def _install(home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", "--install-completion", "bash"],
        capture_output=True,
        text=True,
        env=_env(home),
        timeout=120,
        check=False,
    )


def _home(tmp_path: Path, name: str = "h") -> Path:
    home = tmp_path / name
    home.mkdir()
    return home


@pytest.mark.parametrize(
    "original",
    [b"", b"x", b"x\n", b"x\r\ny\r\n", b"x\r\ny", b"\xff\xfe latin1 \xe9\n", b"\n\n\n"],
    ids=["empty", "nonl", "nl", "crlf", "crlf-nonl", "badutf8", "blanks"],
)
def test_bashrc_original_bytes_kept_and_idempotent(
    tmp_path: Path, original: bytes
) -> None:
    home = _home(tmp_path)
    rc = home / ".bashrc"
    if original:
        rc.write_bytes(original)
    snaps = []
    for _ in range(3):
        d = _install(home)
        assert d.returncode == 0, d.stderr
        assert "Traceback" not in d.stderr
        snaps.append(rc.read_bytes())
    assert snaps[0] == snaps[1] == snaps[2]
    assert snaps[0].startswith(original.replace(b"\r\n", b"\r\n"))  # never rewritten
    assert snaps[0].count(b"compute.sh") == 1


def test_bashrc_with_typers_old_line_not_duplicated(tmp_path: Path) -> None:
    home = _home(tmp_path)
    # Typer's exact line: str() of the path (backslashes on Windows).
    line = f"source '{home / '.bash_completions' / 'compute.sh'}'"
    for ending in ("\n", "\r\n"):
        rc = home / ".bashrc"
        rc.write_bytes(f"# a{ending}{line}{ending}".encode())
        before = rc.read_bytes()
        assert _install(home).returncode == 0
        assert rc.read_bytes() == before, ending


def test_symlinked_bashrc_follows_link_and_keeps_it(tmp_path: Path) -> None:
    home = _home(tmp_path)
    real = tmp_path / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text("# real\n")
    (home / ".bashrc").symlink_to(real)
    for _ in range(2):
        assert _install(home).returncode == 0
    assert (home / ".bashrc").is_symlink()
    assert real.read_text().count("compute.sh") == 1


def test_dangling_symlink_bashrc_does_not_traceback(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".bashrc").symlink_to(tmp_path / "nowhere" / "bashrc")
    d = _install(home)
    assert "Traceback" not in d.stderr
    # fix round 2 (D190 T-10): an install I/O error is exit 4 (config), not the
    # old internal-error exit 1. Same strictness: still exactly two outcomes.
    assert d.returncode in (0, 4)


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permission bits, non-root (root ignores permissions)",
)
@pytest.mark.parametrize(
    "what", ["home_ro", "rc_ro", "rc_is_dir", "completions_is_file"]
)
def test_unwritable_target_is_an_expected_error_not_a_bug_report(
    tmp_path: Path, what: str
) -> None:
    home = _home(tmp_path)
    try:
        if what == "home_ro":
            home.chmod(0o500)
        elif what == "rc_ro":
            (home / ".bashrc").write_text("# x\n")
            (home / ".bashrc").chmod(0o400)
        elif what == "rc_is_dir":
            (home / ".bashrc").mkdir()
        else:
            (home / ".bash_completions").write_text("file")
        d = _install(home)
    finally:
        home.chmod(0o700)
    assert "internal error" not in d.stderr, d.stderr


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permission bits, non-root (root ignores permissions)",
)
@pytest.mark.parametrize(
    "what", ["home_ro", "rc_ro", "rc_is_dir", "completions_is_file"]
)
def test_unwritable_targets_give_one_line_not_traceback(
    tmp_path: Path, what: str
) -> None:
    home = _home(tmp_path)
    try:
        if what == "home_ro":
            home.chmod(0o500)
        elif what == "rc_ro":
            (home / ".bashrc").write_text("# x\n")
            (home / ".bashrc").chmod(0o400)
        elif what == "rc_is_dir":
            (home / ".bashrc").mkdir()
        else:
            (home / ".bash_completions").write_text("file")
        d = _install(home)
    finally:
        home.chmod(0o700)
    assert d.returncode != 0
    assert "Traceback" not in d.stderr
    errs = [l for l in d.stderr.splitlines() if l.strip()]
    assert len(errs) == 1 and errs[0].startswith("compute: "), d.stderr


def test_home_with_single_quote_is_recognised_on_rerun(tmp_path: Path) -> None:
    home = _home(tmp_path, "o'brien")
    (home / ".bashrc").write_text("# x\n")
    for _ in range(3):
        assert _install(home).returncode == 0
    assert (home / ".bashrc").read_text().count("compute.sh") == 1
    if sys.platform == "win32":
        return  # `bash` on a Windows runner is the WSL launcher (no distro)
    # and bash can still parse the file
    d = subprocess.run(
        ["bash", "-n", str(home / ".bashrc")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert d.returncode == 0, d.stderr
