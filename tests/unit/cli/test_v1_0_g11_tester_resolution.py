"""Tester pass, round G11: D452 (node-name resolver) and D457 (public node file).

Hostile and ambiguous inputs the implementer's own tests do not cover.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
import typer

from meshforge.cli import shared as cli_shared
from meshforge.common import local_node as ln
from meshforge.common.errors import FailureType
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes")

A = "node_01J00000000000000000000001"
B = "node_01J00000000000000000000002"
C = "node_01J00000000000000000000003"


class _Client:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes
        self.calls = 0

    def nodes(self) -> dict[str, Any]:
        self.calls += 1
        return {"nodes": self._nodes}


# ------------------------------------------------------------------ D452


def test_two_live_nodes_with_one_name_are_ambiguous_and_name_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client(
        [
            {"node_id": A, "hostname": "lab", "state": "online"},
            {"node_id": B, "hostname": "LAB", "state": "offline"},
        ]
    )
    with pytest.raises(typer.Exit) as info:
        cli_shared._resolve_node_refs(client, ["lab"], "--exclude-node")
    assert info.value.exit_code == ExitCode.USAGE_ERROR
    err = capsys.readouterr().err
    assert "matches 2 nodes" in err and A in err and B in err


def test_one_node_matching_by_name_and_hostname_is_not_ambiguous() -> None:
    client = _Client(
        [{"node_id": A, "hostname": "l3", "display_name": "L3", "state": "online"}]
    )
    assert cli_shared._resolve_node_refs(client, ["l3"], "--node") == [A]


def test_display_name_of_one_node_equal_to_hostname_of_another_is_ambiguous() -> None:
    client = _Client(
        [
            {"node_id": A, "hostname": "x", "display_name": "gpu", "state": "online"},
            {"node_id": B, "hostname": "gpu", "state": "online"},
        ]
    )
    with pytest.raises(typer.Exit):
        cli_shared._resolve_node_refs(client, ["gpu"], "--node")


def test_revoked_plus_two_live_is_still_ambiguous_but_revoked_plus_one_live_is_not() -> (
    None
):
    rows = [
        {"node_id": A, "hostname": "m", "state": "revoked"},
        {"node_id": B, "hostname": "m", "state": "online"},
    ]
    assert cli_shared._resolve_node_refs(_Client(rows), ["m"], "--node") == [B]
    rows.append({"node_id": C, "hostname": "m", "state": "online"})
    with pytest.raises(typer.Exit):
        cli_shared._resolve_node_refs(_Client(rows), ["m"], "--node")


def test_a_row_without_state_counts_as_live() -> None:
    client = _Client([{"node_id": A, "hostname": "m"}])
    assert cli_shared._resolve_node_refs(client, ["m"], "--node") == [A]


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_blank_names_are_a_usage_error_without_a_request(value: str) -> None:
    client = _Client([])
    with pytest.raises(typer.Exit) as info:
        cli_shared._resolve_node_refs(client, [value], "--exclude-node")
    assert info.value.exit_code == ExitCode.USAGE_ERROR
    assert client.calls == 0


def test_unicode_names_match_case_insensitively() -> None:
    client = _Client([{"node_id": A, "hostname": "STRASSE-Büro", "state": "online"}])
    assert cli_shared._resolve_node_refs(client, ["strasse-büro"], "--node") == [A]


def test_node_list_is_fetched_once_for_many_names() -> None:
    client = _Client(
        [
            {"node_id": A, "hostname": "a", "state": "online"},
            {"node_id": B, "hostname": "b", "state": "online"},
        ]
    )
    assert cli_shared._resolve_node_refs(client, ["a", "b", "A"], "--node") == [A, B, A]
    assert client.calls == 1


def test_a_malformed_node_list_is_a_clean_unknown_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client(
        [{"node_id": A, "hostname": None, "display_name": 5, "state": "online"}]
    )  # type: ignore[dict-item]
    client._nodes.append("junk")  # type: ignore[arg-type]
    with pytest.raises(typer.Exit) as info:
        cli_shared._resolve_node_refs(client, ["a"], "--node")
    assert info.value.exit_code == ExitCode.USAGE_ERROR


# ------------------------------------------------------------------ D457


def _public(tmp_path: Path, payload: bytes, *, mode: int = 0o644) -> Path:
    d = tmp_path / "public"
    d.mkdir()
    d.chmod(0o755)
    f = d / ln.PUBLIC_NODE_FILE_NAME
    f.write_bytes(payload)
    f.chmod(mode)
    return d


def _good(node: str | None = None) -> bytes:
    return json.dumps({"format": 1, "node_id": node or str(new_node_id())}).encode()


def test_oversized_public_record_is_refused(tmp_path: Path) -> None:
    pad = b" " * (ln.MAX_NODE_JSON_BYTES + 10)
    public = _public(tmp_path, _good() + pad)
    with pytest.raises(ln.LocalNodeError, match="too large"):
        ln.read_public_node_id(public)


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"null",
        b'{"format": 2, "node_id": "node_01J00000000000000000000001"}',
        b'{"format": 1}',
        b'{"format": 1, "node_id": "../../etc/passwd"}',
        b'{"format": 1, "node_id": 7}',
        b"\xff\xfe\x00bad",
        b"{" * 100_000,
    ],
)
def test_hostile_public_records_are_config_errors_never_a_node(
    tmp_path: Path, payload: bytes
) -> None:
    public = _public(tmp_path, payload)
    with pytest.raises(ln.LocalNodeError) as info:
        ln.read_public_node_id(public)
    assert info.value.failure_type is FailureType.CONFIG_ERROR


def test_a_bool_format_is_not_format_1(tmp_path: Path) -> None:
    """JSON ``true == 1`` in Python: a record with ``"format": true`` must not pass."""
    payload = json.dumps({"format": True, "node_id": str(new_node_id())}).encode()
    with pytest.raises(ln.LocalNodeError):
        ln.read_public_node_id(_public(tmp_path, payload))


@posix_only
def test_user_identity_wins_even_when_the_system_record_is_unsafe(
    tmp_path: Path,
) -> None:
    """Precedence: a valid user-mode identity never even opens the public record."""
    user_node = str(new_node_id())
    data = tmp_path / "data"
    (data / "identity").mkdir(parents=True)
    (data / "identity" / "node.json").write_text(
        json.dumps({"format": 1, "node_id": user_node})
    )
    public = _public(tmp_path, _good(), mode=0o666)  # would be refused
    got = ln.resolve_local_node(data, public_dir=public)
    assert got.node_id == user_node and got.source is ln.LocalNodeSource.USER


@posix_only
def test_unsafe_system_record_is_refused_when_nothing_else_exists(
    tmp_path: Path,
) -> None:
    public = _public(tmp_path, _good(), mode=0o666)
    with pytest.raises(ln.LocalNodeError) as info:
        ln.resolve_local_node(tmp_path / "empty", public_dir=public)
    assert info.value.failure_type is not FailureType.NOT_FOUND


@posix_only
def test_a_symlinked_public_folder_to_a_writable_target_is_refused(
    tmp_path: Path,
) -> None:
    real = _public(tmp_path, _good())
    real.chmod(0o777)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ln.LocalNodeError):
        ln.read_public_node_id(link)


@posix_only
def test_a_fifo_in_place_of_the_record_does_not_hang_the_cli(tmp_path: Path) -> None:
    """Defect probe: ``os.open(path, O_RDONLY)`` on a FIFO blocks until a writer
    appears; the `compute run --local` path must refuse a non-regular file
    without waiting (needs ``O_NONBLOCK`` or an lstat first)."""
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    fifo = public / ln.PUBLIC_NODE_FILE_NAME
    os.mkfifo(fifo, 0o644)
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(ln.read_public_node_id(public))
        except ln.LocalNodeError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    hung = worker.is_alive()
    if hung:  # release the blocked open() so the test process can finish
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        worker.join(timeout=3.0)
    assert not hung, "read_public_node_id blocked on a FIFO"
    assert isinstance(outcome[0], ln.LocalNodeError)
