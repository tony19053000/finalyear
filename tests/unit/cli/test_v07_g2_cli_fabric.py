"""V0.7 G2: ``compute fabric invite|invites|join`` (real client, faked transport).

* ``invite`` posts ttl/replace, prints the code alone on stdout, facts on stderr;
  bad ``--ttl`` / ``--replace`` refused before any request;
* ``invites`` lists (no codes) and ``--revoke`` posts to the id's revoke path;
  a malformed id never reaches a URL;
* a 401 from the controller is the fabric-token message (D151);
* ``join`` with no agent installed says so (``CONFIG_ERROR``); with one, it runs
  ``meshforge-agent join -`` and hands the code over **stdin**, never argv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import fabric_commands
from meshforge.cli.app import app
from meshforge.common.errors import ExitCode

CODE = "mf1-aaaaa-bbbbb-ccccc-ddddd"
INVITE = "inv_01J0000000000000000000AAAA"
NODE = "node_01J0000000000000000000BBBB"


def test_invite_posts_and_prints_the_code_alone(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/fabric/invites"
        assert request.method == "POST"
        seen.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "code": CODE,
                "invite_id": INVITE,
                "address": "10.0.0.1:47101",
                "ca_pin": "ab" * 16,
                "expires_at": "2026-10-08T12:15:00+00:00",
                "replace_node_id": NODE,
            },
        )

    controller(handler)
    result = runner.invoke(app, ["fabric", "invite", "--ttl", "2h", "--replace", NODE])
    assert result.exit_code == 0, result.output
    assert result.stdout == CODE + "\n"
    assert INVITE in result.stderr and NODE in result.stderr
    assert CODE not in result.stderr
    assert seen == [{"ttl_s": 7200, "replace_node_id": NODE}]
    result = runner.invoke(app, ["fabric", "invite", "--json"])
    assert json.loads(result.stdout)["code"] == CODE
    assert seen[-1] == {}


@pytest.mark.parametrize(
    "args", [["--ttl", "soon"], ["--replace", "bob"], ["--replace", "inv_x"]]
)
def test_invite_refuses_bad_options_before_any_request(
    runner: CliRunner, controller: TransportFactory, args: list[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    controller(handler)
    result = runner.invoke(app, ["fabric", "invite", *args])
    assert result.exit_code == ExitCode.USAGE_ERROR


def test_invite_without_the_fabric_token_says_which_token(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda request: httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "no"}}
        )
    )
    result = runner.invoke(app, ["fabric", "invite"])
    assert result.exit_code != 0
    assert "fabric token" in result.stderr


def test_invites_list_and_revoke(
    runner: CliRunner, controller: TransportFactory
) -> None:
    paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "invites": [
                        {
                            "id": INVITE,
                            "state": "used",
                            "expires_at": "2026-10-08T12:15:00+00:00",
                            "used_by_node": NODE,
                            "replace_node_id": None,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"id": INVITE, "state": "revoked"})

    controller(handler)
    listed = runner.invoke(app, ["fabric", "invites"])
    assert listed.exit_code == 0 and INVITE in listed.stdout and NODE in listed.stdout
    revoked = runner.invoke(app, ["fabric", "invites", "--revoke", INVITE])
    assert revoked.exit_code == 0 and f"revoked {INVITE}" in revoked.stdout
    assert paths == [
        ("GET", "/v1/fabric/invites"),
        ("POST", f"/v1/fabric/invites/{INVITE}/revoke"),
    ]
    bad = runner.invoke(app, ["fabric", "invites", "--revoke", "../../jobs"])
    assert bad.exit_code == ExitCode.USAGE_ERROR and len(paths) == 2


def test_join_without_an_agent_is_a_clear_config_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fabric_commands, "agent_command", lambda: None)
    result = runner.invoke(app, ["fabric", "join", CODE])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "no MeshForge agent is installed" in result.stderr


def test_join_hands_the_code_to_the_agent_on_stdin(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    fake = tmp_path / "fake_agent.py"
    fake.write_text(
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, "
        f"open({str(record)!r}, 'w'))\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        fabric_commands, "agent_command", lambda: [sys.executable, str(fake)]
    )
    result = runner.invoke(app, ["fabric", "join", CODE, "--force"])
    assert result.exit_code == 7  # the agent's own exit code
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"] == ["join", "-", "--force"]
    assert CODE not in " ".join(seen["argv"])
    assert seen["stdin"].strip() == CODE
