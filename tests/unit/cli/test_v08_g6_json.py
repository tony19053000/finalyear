"""V0.8 G6 (D178) deliverable 3: ``--json`` on every read command, a stable and
documented schema with ``schema_version``, snapshot tests.

The table in docs/CLI.md section 3 is the contract. These tests hold it to:

* **the code** - every command with ``--json`` is in the table and every row's
  command has ``--json``; running it asks the documented route and prints
  exactly ``{payload + schema_version}`` on one sorted line (snapshot);
* **the API** - every guaranteed field exists in that route's response schema
  in the controller's OpenAPI document, so a server-side rename fails here and
  not in a user's script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer.main
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli.render import JSON_SCHEMA_VERSION

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CLI_MD = REPO / "docs" / "CLI.md"
NODE = "node_01J0000000000000000000BBBB"
JOB = "job_01J0000000000000000000CCCC"
INVITE = "inv_01J0000000000000000000AAAA"
CAMPAIGN = "camp_01J0000000000000000000DDDD"  # V0.9 G3
SPEC = str(REPO / "examples" / "bench" / "cpu-baseline.yaml")
_PLACEHOLDERS = {
    "NODE_ID": NODE,
    "JOB_ID": JOB,
    "NAME": "ds",
    "ID": INVITE,
    "CAMPAIGN_ID": CAMPAIGN,
    "SPEC": SPEC,
}
_PATH_PARAMS = {
    "{node_id}": NODE,
    "{job_id}": JOB,
    "{name}": "ds",
    "{invite_id}": INVITE,
    "{campaign_id}": CAMPAIGN,
}
_ROW = re.compile(r"^\| `compute ([^`]+)` \| ([^|]+) \| (.+) \|$")


def _json_table() -> list[tuple[str, str, list[str]]]:
    """(command words, source, guaranteed fields) per row of CLI.md section 3."""
    text = CLI_MD.read_text(encoding="utf-8")
    section = text.split("## 3. `--json`", 1)[1].split("\n## 4.", 1)[0]
    rows: list[tuple[str, str, list[str]]] = []
    for line in section.splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        command, source, fields = match.groups()
        rows.append(
            (
                command.strip(),
                source.strip().strip("`"),
                re.findall(r"`([^`]+)`", fields),
            )
        )
    return rows


ROWS = _json_table()


def _argv(command: str) -> list[str]:
    return [_PLACEHOLDERS.get(word, word) for word in command.split()] + ["--json"]


def test_the_doc_table_was_parsed() -> None:
    assert len(ROWS) >= 16, ROWS
    assert all(fields for _c, _s, fields in ROWS)


# ------------------------------------------------------------- doc <-> code


def _commands_with_json() -> set[str]:
    """Every leaf command path of the real Typer tree that has ``--json``."""
    found: set[str] = set()

    def walk(command: Any, path: list[str]) -> None:
        if hasattr(command, "commands"):
            for name, sub in command.commands.items():
                walk(sub, [*path, name])
            return
        if any("--json" in getattr(p, "opts", ()) for p in command.params):
            found.add(" ".join(path))

    walk(typer.main.get_command(cli_app.app), [])
    return found


def test_every_json_command_is_documented_and_every_row_has_json() -> None:
    documented = {
        " ".join(
            w
            for w in c.split()
            # V0.14 G4: `planner eval` needs its --campaign option to run
            if not w.isupper() and w not in ("--revoke", "--campaign")
        )
        for c, _s, _f in ROWS
    }
    assert _commands_with_json() == documented


READ_COMMANDS = {
    "status",
    "nodes",
    "jobs",
    "explain",
    "network",
    "whoami",
    "tokens list",
    "secret list",
    "data ls",
    "data show",
    "fabric invites",
}
"""Commands that only read. ``logs`` is the workload's byte stream and has no
``--json`` by design (docs/CLI.md section 1)."""


def test_every_read_command_has_json() -> None:
    assert _commands_with_json() >= READ_COMMANDS


# ------------------------------------------------------------- doc <-> API


def _openapi() -> dict[str, Any]:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    from meshforge.controller.http import openapi_document

    return openapi_document()


def _resolve(doc: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` and the non-null branch of ``anyOf`` to an object/array."""
    for _ in range(10):
        if "$ref" in schema:
            schema = doc["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
            continue
        branches = schema.get("anyOf") or schema.get("oneOf")
        if branches:
            real = [b for b in branches if b.get("type") != "null"]
            schema = real[0]
            continue
        return schema
    raise AssertionError(f"unresolvable schema {schema}")


def _has_field(doc: dict[str, Any], schema: dict[str, Any], path: str) -> bool:
    current = _resolve(doc, schema)
    for part in re.split(r"\.", path):
        is_list = part.endswith("[]")
        name = part.removesuffix("[]")
        props = current.get("properties")
        if not isinstance(props, dict) or name not in props:
            return False
        current = _resolve(doc, props[name])
        if is_list:
            if current.get("type") != "array":
                return False
            current = _resolve(doc, current["items"])
    return True


@pytest.mark.parametrize(
    ("command", "source", "fields"),
    [row for row in ROWS if row[1] != "built by the CLI"],
    ids=[row[0] for row in ROWS if row[1] != "built by the CLI"],
)
def test_documented_fields_exist_in_the_api_response_schema(
    command: str, source: str, fields: list[str]
) -> None:
    doc = _openapi()
    method, path = source.split(" ", 1)
    operation = doc["paths"][path][method.lower()]
    ok = next(code for code in ("200", "201") if code in operation["responses"])
    schema = operation["responses"][ok]["content"]["application/json"]["schema"]
    missing = [f for f in fields if not _has_field(doc, schema, f)]
    assert missing == [], f"{command}: not in the API's {path} schema: {missing}"


# ------------------------------------------------------------- snapshots


def _set(target: dict[str, Any], path: str) -> None:
    """Put a deterministic value at ``path`` (``a[].b``, ``a.b``, ``a``)."""
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        items = target.setdefault(head[:-2], [{}])
        if rest:
            _set(items[0], rest)
        return
    if rest:
        _set(target.setdefault(head, {}), rest)
        return
    target.setdefault(head, f"v-{head}")


def _payload(fields: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"z_unknown_future_field": [3, {"b": 1, "a": None}]}
    # nested paths first: a bare `nodes` must not turn `nodes` into a string
    for field in sorted(fields, key=lambda f: ("." not in f and "[]" not in f, f)):
        _set(payload, field)
    return payload


@pytest.mark.parametrize(
    ("command", "source", "fields"),
    [row for row in ROWS if row[1] != "built by the CLI"],
    ids=[row[0] for row in ROWS if row[1] != "built by the CLI"],
)
def test_json_is_the_answer_plus_schema_version_on_one_sorted_line(
    runner: CliRunner,
    controller: TransportFactory,
    command: str,
    source: str,
    fields: list[str],
) -> None:
    payload = _payload(fields)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=payload)

    controller(handler)
    result = runner.invoke(cli_app.app, _argv(command))
    assert result.exit_code == 0, result.output
    method, path = source.split(" ", 1)
    for placeholder, value in _PATH_PARAMS.items():
        path = path.replace(placeholder, value)
    assert seen == [(method, path)]
    expected = json.dumps(
        {**payload, "schema_version": JSON_SCHEMA_VERSION},
        separators=(",", ":"),
        sort_keys=True,
    )
    assert result.stdout == expected + "\n"
    assert result.stderr == ""


NODES_SNAPSHOT_IN = {
    "count": 1,
    "nodes": [
        {
            "node_id": NODE,
            "hostname": "lab-a\x1b[31m",
            "state": "online",
            "connected": True,
            "cpu_percent": 12.5,
            "gpus": [],
            "scores": None,
        }
    ],
}
NODES_SNAPSHOT_OUT = (
    '{"count":1,"nodes":[{"connected":true,"cpu_percent":12.5,"gpus":[],'
    '"hostname":"lab-a\\u001b[31m","node_id":"node_01J0000000000000000000BBBB",'
    '"scores":null,"state":"online"}],"schema_version":1}\n'
)


def test_nodes_json_snapshot(runner: CliRunner, controller: TransportFactory) -> None:
    controller(lambda _r: httpx.Response(200, json=NODES_SNAPSHOT_IN))
    result = runner.invoke(cli_app.app, ["nodes", "--json"])
    assert result.exit_code == 0
    assert result.stdout == NODES_SNAPSHOT_OUT


def test_status_json_snapshot_reachable(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 2})
        return httpx.Response(200, json={"nodes": [], "count": 0})

    controller(handler)
    result = runner.invoke(cli_app.app, ["status", "--json"])
    assert result.exit_code == 0
    assert result.stdout == (
        '{"controller_url":"https://127.0.0.1:47110","detail":null,'
        '"live_sessions":2,"nodes":{"count":0,"nodes":[]},"reachable":true,'
        '"schema_version":1}\n'
    )


def test_status_json_when_logged_out_is_reachable_with_exit_3(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 0})
        return httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "x"}}
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["status", "--json"])
    assert result.exit_code == 3
    body = json.loads(result.stdout)
    assert body["reachable"] is True and body["nodes"] is None
    assert "compute login" in body["detail"]
    assert body["live_sessions"] == 0


def test_status_json_documented_fields_match_the_code(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(200, json={"nodes": [], "count": 0}))
    result = runner.invoke(cli_app.app, ["status", "--json"])
    documented = next(f for c, s, f in ROWS if c == "status")
    assert set(json.loads(result.stdout)) == {*documented, "schema_version"}


def test_a_failing_json_command_prints_nothing_on_stdout(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            404, json={"error": {"code": "not_found", "message": "no such job"}}
        )
    )
    result = runner.invoke(cli_app.app, ["jobs", JOB, "--json"])
    assert result.exit_code == 8
    assert result.stdout == ""
    assert result.stderr.startswith("compute: ")
