"""V0.8 G6 (D178) deliverable 2: one renderer, NO_COLOR, non-TTY, no tracebacks.

* Statically: no module of ``meshforge.cli`` but ``render.py`` prints - no
  ``typer.echo``/``click.echo``/``print``/``Console(...)``/``sys.std*.write``
  (the workload's log bytes in ``compute logs`` are the one allowed raw copy,
  via ``sys.stdout.buffer``).
* ``NO_COLOR`` turns colour off even on a forced terminal; a pipe gets no escape
  sequence at all; help text is plain ASCII with ``[cli]`` shown literally.
* An unexpected exception is one line naming its type (never its text) and
  exit 1; ``COMPUTE_DEBUG=1`` re-raises it.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from rich.text import Text
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import render
from meshforge.cli import shared as cli_shared
from meshforge.cli.exits import CliExit

pytestmark = pytest.mark.unit

CLI_DIR = Path(cli_app.__file__).parent
_ESC = "\x1b"
_COLOUR_SGR = re.compile(r"\x1b\[[0-9;]*(?:3[0-9]|4[0-9]|9[0-7]|10[0-7])[0-9;]*m")
NODES = {
    "nodes": [
        {
            "node_id": "node_01JQZ00000000000000000000A",
            "hostname": "lab-a",
            "state": "online",
            "connected": True,
        }
    ],
    "count": 1,
}


_PRINTERS = frozenset(
    {
        "print",
        "typer.echo",
        "typer.secho",
        "click.echo",
        "Console",
        "sys.stdout.write",
        "sys.stderr.write",
    }
)


def _printing_calls(path: Path) -> list[str]:
    """Every call in ``path`` that writes to the terminal outside render.py."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        text = ast.unparse(node.func)
        if text in _PRINTERS:
            found.append(f"{path.name}:{node.lineno} {text}")
    return found


def test_no_cli_module_but_render_prints() -> None:
    offenders: list[str] = []
    for path in sorted(CLI_DIR.glob("*.py")):
        if path.name == "render.py":
            continue
        offenders.extend(_printing_calls(path))
    assert offenders == [], "print through meshforge.cli.render: " + ", ".join(
        offenders
    )


def test_the_static_check_would_catch_a_raw_print(tmp_path: Path) -> None:
    sample = tmp_path / "x.py"
    sample.write_text(
        "import typer, sys\n"
        "from rich.console import Console\n"
        "typer.echo('a')\nprint('b')\nConsole().print('c')\nsys.stderr.write('d')\n",
        encoding="utf-8",
    )
    assert len(_printing_calls(sample)) == 4


def test_render_console_honours_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console itself: colour on a (forced) colour terminal, none with
    NO_COLOR - while bold, which is not colour, may stay (no-color.org)."""
    monkeypatch.setenv("FORCE_COLOR", "1")  # what a real colour TTY would be
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    coloured = render.console()
    with coloured.capture() as cap:
        coloured.print(Text("x", style="red"))
    assert _COLOUR_SGR.search(cap.get()), "test cannot see colour"

    monkeypatch.setenv("NO_COLOR", "1")
    plain = render.console()
    assert plain.no_color is True
    with plain.capture() as cap:
        plain.print(Text("x", style="red bold"))
    assert not _COLOUR_SGR.search(cap.get()) and "x" in cap.get()


def test_an_empty_no_color_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    assert render.no_color() is False


def test_a_pipe_gets_no_escape_sequence_at_all(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(200, json=NODES))
    result = runner.invoke(cli_app.app, ["nodes"])
    assert result.exit_code == 0, result.output
    assert _ESC not in result.output
    assert "lab-a" in result.output


@pytest.mark.parametrize(
    "argv", [["--help"], ["tokens", "--help"], ["nodes"], ["secret", "--help"]]
)
def test_no_color_with_forced_colour_still_prints_no_colour(
    runner: CliRunner,
    controller: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("NO_COLOR", "1")
    controller(lambda _r: httpx.Response(200, json=NODES))
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == 0, result.output
    assert not _COLOUR_SGR.search(result.output), repr(result.output[:300])


def test_help_is_plain_ascii_and_shows_square_brackets(runner: CliRunner) -> None:
    """Typer's Rich help ate ``[cli]`` as a markup tag before V0.8 G6."""
    result = runner.invoke(cli_app.app, ["--help"])
    assert result.exit_code == 0
    assert "[cli] token" in result.output and "[cli] ca_file" in result.output
    assert result.output.isascii()
    assert "--install-completion" in result.output


def test_line_flattens_control_characters(capsys: pytest.CaptureFixture[str]) -> None:
    render.line("a\x1b[2Jb\rc\nd")
    render.note("x\x07y")
    out, err = capsys.readouterr()
    assert out == "a [2Jb c d\n"
    assert err == "compute: x y\n"


def test_fail_scrubs_token_shaped_text(capsys: pytest.CaptureFixture[str]) -> None:
    token = "mfat_" + "A" * 43
    render.fail(f"refused: Bearer {token} and {token} code mf1-ABCD-EFGH-JKMN-PQRS-TV")
    err = capsys.readouterr().err
    assert token not in err and "A" * 20 not in err
    assert "mf1-ABCD-EFGH" not in err
    assert err.startswith("compute: refused:") and err.count("\n") == 1


def test_emit_json_is_one_sorted_line_with_the_schema_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render.emit_json({"b": 1, "a": {"y": 2, "x": "\x1b"}})
    out = capsys.readouterr().out
    assert out == '{"a":{"x":"\\u001b","y":2},"b":1,"schema_version":1}\n'
    assert json.loads(out)["schema_version"] == render.JSON_SCHEMA_VERSION


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["compute", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_app.main()
    code = exited.value.code
    return code if isinstance(code, int) else 0


def test_an_unexpected_exception_is_one_line_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "mfat_" + "S" * 43

    def broken(_settings: object) -> object:
        raise RuntimeError(f"bug holding {secret}")

    monkeypatch.setattr(cli_shared, "_client", broken)
    code = _run_main(monkeypatch, ["nodes"])
    out, err = capsys.readouterr()
    assert code == CliExit.FAILURE
    assert err.startswith("compute: internal error (RuntimeError)")
    assert "COMPUTE_DEBUG=1" in err and err.count("\n") == 1
    assert secret not in out + err and "Traceback" not in out + err


def test_compute_debug_shows_the_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_settings: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_shared, "_client", broken)
    monkeypatch.setenv("COMPUTE_DEBUG", "1")
    monkeypatch.setattr(sys, "argv", ["compute", "nodes"])
    with pytest.raises(RuntimeError, match="boom"):
        cli_app.main()


def test_a_usage_error_through_main_is_still_click_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_main(monkeypatch, ["nodes", "--no-such-flag"]) == CliExit.USAGE_ERROR
    assert "internal error" not in capsys.readouterr().err
