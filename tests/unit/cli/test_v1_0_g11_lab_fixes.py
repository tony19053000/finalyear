"""V1.0 G11 lab fixes in the CLI (round 11, v0.17.3).

* D452 - the one shared NODE-by-name resolver (``--exclude-node``,
  ``run --node``) ignores revoked nodes: a laptop that re-joined keeps its
  hostname and its old revoked row must not make the name ambiguous; an exact
  node id still works.
* D453 - a failed job's last attempt failure (``node_lost: node_revoked``)
  shows in the normal ``compute jobs JOB_ID`` view and on ``run -f``'s end line.
* D454 - ``compute data rm`` prints what it freed; ``compute data gc
  [--dry-run]`` sends the sweep and prints count and bytes.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import typer
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import job_commands, render
from meshforge.cli import shared as cli_shared
from meshforge.cli.app import app
from meshforge.cli.exits import CliExit
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

OLD = "node_01J00000000000000000000001"  # revoked, same hostname
NEW = "node_01J00000000000000000000002"  # the re-joined laptop
L1 = "node_01J00000000000000000000003"
JOB = "job_01J0000000000000000000CCCC"


class Nodes:
    def __init__(self) -> None:
        self.calls = 0

    def nodes(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "nodes": [
                {
                    "node_id": OLD,
                    "hostname": "Mohit",
                    "display_name": "L3",
                    "state": "revoked",
                },
                {
                    "node_id": NEW,
                    "hostname": "Mohit",
                    "display_name": "L3",
                    "state": "offline",
                },
                {"node_id": L1, "hostname": "JARVISM3", "state": "online"},
                {
                    "node_id": "node_01J00000000000000000000004",
                    "hostname": "gone",
                    "state": "revoked",
                },
            ]
        }


# ------------------------------------------------------------------ D452


def test_a_name_ignores_revoked_nodes_of_the_same_name() -> None:
    client = Nodes()
    assert job_commands._resolve_excluded(client, ["mohit"]) == [NEW]
    assert job_commands._resolve_excluded(client, ["l3", "JARVISM3"]) == sorted(
        [NEW, L1]
    )
    assert cli_shared._resolve_node_refs(client, ["Mohit"], "--node") == [NEW]


def test_the_exact_id_of_a_revoked_node_still_works_without_a_request() -> None:
    client = Nodes()
    assert job_commands._resolve_excluded(client, [OLD]) == [OLD]
    assert client.calls == 0


def test_a_name_only_a_revoked_node_has_is_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(typer.Exit) as info:
        cli_shared._resolve_node_refs(Nodes(), ["gone"], "--node")
    assert info.value.exit_code == ExitCode.USAGE_ERROR
    assert "--node 'gone': no node has that id" in capsys.readouterr().err


def _project(root: Path) -> Path:
    root.mkdir()
    (root / "compute.yaml").write_text("name: p\nentrypoint: python main.py\n")
    (root / "main.py").write_text("print(1)\n")
    return root


class RunClient(Nodes):
    controller_url = "https://127.0.0.1:1"
    auth_header = None
    ssl_context = None

    def __init__(self) -> None:
        super().__init__()
        self.submitted: Mapping[str, Any] | None = None

    def submit_job(self, body: Mapping[str, Any], *, idempotency_key: str) -> Any:
        self.submitted = body
        return {"job_id": JOB, "state": "queued", "created": True}

    def __enter__(self) -> RunClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _run(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, RunClient, str]:
    from meshforge.cli import transfer

    client = RunClient()
    monkeypatch.setattr(job_commands, "_own_node", lambda *_a, **_k: None)
    monkeypatch.setattr(
        transfer,
        "upload_tree",
        lambda *_a: SimpleNamespace(
            uploaded_blobs=0, uploaded_bytes=0, skipped_blobs=1, resumed_parts=0
        ),
    )
    monkeypatch.setattr(sys, "argv", ["compute", "run", *argv])
    monkeypatch.setattr(
        cli_shared,
        "_settings",
        lambda: SimpleNamespace(data_dir=tmp_path, cli=SimpleNamespace(timeout_s=5.0)),
    )
    monkeypatch.setattr(cli_shared, "_client", lambda _settings: client)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    return code, client, capsys.readouterr().err


def test_run_node_accepts_a_name_and_pins_the_live_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "proj")
    code, client, err = _run(
        [str(project), "--node", "mohit"], tmp_path, monkeypatch, capsys
    )
    assert code == CliExit.OK, err
    assert client.submitted is not None
    assert client.submitted["placement"]["node_id"] == NEW
    assert client.submitted["placement"]["local"] is False


def test_run_node_by_id_needs_no_node_list_and_pin_plus_exclude_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "proj")
    code, client, _ = _run([str(project), "--node", L1], tmp_path, monkeypatch, capsys)
    assert code == CliExit.OK and client.calls == 0
    assert client.submitted is not None
    assert client.submitted["placement"]["node_id"] == L1
    code, client, err = _run(
        [str(project), "--node", "Mohit", "--exclude-node", NEW],
        tmp_path,
        monkeypatch,
        capsys,
    )
    assert code == CliExit.USAGE_ERROR, err
    assert client.submitted is None


def test_run_node_with_an_unknown_name_is_a_usage_error_before_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "proj")
    code, client, err = _run(
        [str(project), "--node", "gone"], tmp_path, monkeypatch, capsys
    )
    assert code == CliExit.USAGE_ERROR, err
    assert client.submitted is None
    assert "no node has that id, name or hostname" in err


# ------------------------------------------------------------------ D453

REVOKED_JOB: dict[str, Any] = {
    "job_id": JOB,
    "name": "mc",
    "state": "failed",
    "failure_type": "node_lost",
    "failure_detail": "placed_node_offline",
    "attempts": [
        {
            "attempt_id": "att_1",
            "node_id": OLD,
            "fencing_token": 1,
            "state": "lost",
            "role": "run",
            "failure_type": "node_lost",
            "detail": "node_revoked",
            "missing_outputs": [],
        }
    ],
}


def test_the_last_attempts_failure_is_the_jobs_cause() -> None:
    assert render.job_failure_cause(REVOKED_JOB) == "node_lost: node_revoked"


def test_no_cause_row_when_it_says_nothing_new_or_the_job_did_not_fail() -> None:
    same = {**REVOKED_JOB, "failure_detail": "node_revoked"}
    assert render.job_failure_cause(same) == ""
    ok = {**REVOKED_JOB, "state": "succeeded", "failure_type": None}
    assert render.job_failure_cause(ok) == ""  # a retried success: attempt rows
    assert render.job_failure_cause({"failure_type": "x", "attempts": "bad"}) == ""


def test_the_newest_attempt_wins_and_server_text_is_scrubbed() -> None:
    first = {**REVOKED_JOB["attempts"][0], "fencing_token": 1, "detail": "old"}
    last = {
        **REVOKED_JOB["attempts"][0],
        "fencing_token": 2,
        "detail": "node_revoked\x1b[2J",
    }
    payload = {**REVOKED_JOB, "attempts": [last, first, "junk"]}
    cause = render.job_failure_cause(payload)
    assert cause.startswith("node_lost: node_revoked") and "\x1b" not in cause


def test_jobs_show_prints_the_cause_in_the_normal_view(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/jobs/{JOB}"
        return httpx.Response(200, json=REVOKED_JOB)

    controller(handler)
    result = runner.invoke(app, ["jobs", JOB])
    assert result.exit_code == 0, result.output
    cause_rows = [ln for ln in result.output.splitlines() if "failure_cause" in ln]
    assert len(cause_rows) == 1 and "node_lost: node_revoked" in cause_rows[0]


def test_run_follow_end_line_names_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from meshforge.cli import follow

    monkeypatch.setattr(
        follow,
        "follow_logs",
        lambda *_a, **_k: {"state": "failed", "failure_type": "node_lost"},
    )

    class Client:
        controller_url = "https://127.0.0.1:1"
        auth_header = None
        ssl_context = None

        def job(self, job_id: str) -> dict[str, Any]:
            return REVOKED_JOB

    with pytest.raises(typer.Exit) as info:
        job_commands._follow_to_exit(Client(), JOB, 5.0)
    assert info.value.exit_code != CliExit.OK
    err = capsys.readouterr().err
    assert "failed (node_lost)" in err
    assert "last attempt: node_lost: node_revoked" in err


# ------------------------------------------------------------------ D454


def test_data_rm_prints_what_it_freed(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE" and request.url.path == "/v1/data/ds/3"
        return httpx.Response(
            200,
            json={
                "ref": "ds@3",
                "removed": True,
                "swept": True,
                "freed_blobs": 12,
                "freed_bytes": 5 * 1024**3,
                "kept_recent_blobs": 2,
            },
        )

    controller(handler)
    result = runner.invoke(app, ["data", "rm", "ds@3"])
    assert result.exit_code == 0, result.output
    assert "removed ds@3" in result.output
    assert "freed 12 blobs" in result.output
    assert "2 recently used blobs kept" in result.output


def test_data_rm_from_an_older_controller_prints_no_freed_line(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(200, json={"ref": "ds@3", "removed": True}))
    result = runner.invoke(app, ["data", "rm", "ds@3"])
    assert result.exit_code == 0 and "freed" not in result.output


@pytest.mark.parametrize(("argv", "query"), [([], "false"), (["--dry-run"], "true")])
def test_data_gc_sends_the_sweep_and_prints_count_and_bytes(
    runner: CliRunner, controller: TransportFactory, argv: list[str], query: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "dry_run": query == "true",
                "removed_blobs": 7,
                "removed_bytes": 2048,
                "kept_recent_blobs": 1,
                "kept_recent_bytes": 10,
                "failed_blobs": 0,
                "duration_s": 0.1,
            },
        )

    controller(handler)
    result = runner.invoke(app, ["data", "gc", *argv])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1 and seen[0].method == "POST"
    assert seen[0].url.path == "/v1/data/gc"
    assert seen[0].url.params["dry_run"] == query
    verb = "would free" if query == "true" else "freed"
    assert f"{verb} 7 blobs / 2.0 KiB" in result.output or f"{verb} 7 blobs" in (
        result.output
    )
    assert "kept 1 unreferenced blobs" in result.output


def test_data_gc_refused_for_a_non_admin_exits_non_zero(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            403, json={"error": {"code": "forbidden", "message": "admin only"}}
        )
    )
    result = runner.invoke(app, ["data", "gc"])
    assert result.exit_code != 0
    assert "admin only" in result.output
