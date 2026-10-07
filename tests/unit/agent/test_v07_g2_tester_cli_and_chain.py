"""V0.7 G2 tester: deviation 1 (reading the unverified TLS chain on Python 3.12 via
a private attribute, public on 3.13) and the agent CLI surface for ``join`` /
``identity`` (stdin handling, exit codes, nothing echoed).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.agent import enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.main import app
from meshforge.common import join_code, pki
from meshforge.common.errors import ExitCode
from meshforge.common.ids import new_fabric_id

CODE = join_code.encode(
    join_code.JoinCode(
        host="127.0.0.1", port=1, token=bytes(range(16)), ca_pin="ab" * 16
    )
)


# --------------------------------------------------- deviation 1: chain reading


def test_chain_reader_supports_both_the_313_public_and_312_private_shapes() -> None:
    ca = pki.create_ca(new_fabric_id())
    der = ca.cert.public_bytes(
        __import__(
            "cryptography.hazmat.primitives.serialization", fromlist=["x"]
        ).Encoding.DER
    )
    # 3.13: SSLSocket.get_unverified_chain() -> list[bytes]
    public = types.SimpleNamespace(get_unverified_chain=lambda: [der, der])
    assert enrollment._chain_der(public) == [der, der]  # type: ignore[arg-type]
    # 3.12: only the private _sslobj has it
    private = types.SimpleNamespace(
        _sslobj=types.SimpleNamespace(get_unverified_chain=lambda: [der])
    )
    assert enrollment._chain_der(private) == [der]  # type: ignore[arg-type]
    # no chain at all
    assert (
        enrollment._chain_der(types.SimpleNamespace(get_unverified_chain=lambda: None))
        == []
    )  # type: ignore[arg-type]


def test_chain_reader_fails_with_a_typed_error_when_python_has_neither() -> None:
    with pytest.raises(JoinError) as info:
        enrollment._chain_der(types.SimpleNamespace())  # type: ignore[arg-type]
    assert info.value.reason is JoinReason.TLS_ERROR


def test_a_hostile_chain_is_capped_and_garbage_entries_are_skipped() -> None:
    fake = types.SimpleNamespace(get_unverified_chain=lambda: [b"\x00junk"] * 5000)
    assert len(enrollment._chain_der(fake)) <= 8  # type: ignore[arg-type]


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="the pinned interpreter")
def test_the_pinned_interpreter_still_has_the_private_hook() -> None:
    """A Python 3.12.x patch release that removed ``_sslobj.get_unverified_chain``
    would silently break every join: fail loudly here instead."""
    import _ssl

    assert hasattr(_ssl._SSLSocket, "get_unverified_chain")


# ------------------------------------------------------------------- agent CLI


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(
        f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8"
    )
    return path


def _run(config: Path, *args: str, stdin: str | None = None):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(app, [*args, "--config", str(config)], input=stdin)


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "\n",
        "   \n",
        "mf1-\n",
        "not a code\n",
        CODE[:-6] + "\n",
        "x" * 100_000 + "\n",
    ],
)
def test_join_from_stdin_with_bad_input_exits_2_and_echoes_nothing(
    config: Path, stdin: str
) -> None:
    result = _run(config, "join", "-", stdin=stdin)
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output
    shown = result.output
    if len(stdin.strip()) > 8:
        assert stdin.strip()[:30] not in shown
    assert "Traceback" not in shown


def test_join_with_the_code_on_argv_never_echoes_it_even_on_failure(
    config: Path,
) -> None:
    result = _run(config, "join", CODE)  # port 1: nothing listens
    assert result.exit_code != 0
    assert CODE not in result.output and CODE.split("-", 2)[1] not in result.output
    assert bytes(range(16)).hex() not in result.output


def test_two_line_paste_reads_only_the_first_line_and_says_the_code_is_short(
    config: Path,
) -> None:
    half = len(CODE) // 2
    result = _run(config, "join", "-", stdin=CODE[:half] + "\n" + CODE[half:] + "\n")
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert CODE[:half] not in result.output


def test_identity_show_and_reset_json_shape_and_exit_codes(config: Path) -> None:
    shown = _run(config, "identity", "show", "--json")
    assert shown.exit_code == 0, shown.output
    facts = json.loads(shown.stdout)
    assert facts["enrolled"] is False
    assert not any(key in facts for key in ("private_key", "token", "code"))
    refused = _run(config, "identity", "reset")
    assert refused.exit_code == ExitCode.USAGE_ERROR  # --yes missing
    assert _run(config, "identity", "reset", "--yes").exit_code == 0
