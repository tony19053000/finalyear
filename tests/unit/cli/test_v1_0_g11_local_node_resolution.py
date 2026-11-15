"""V1.0 G11 (D457): which node is this laptop, for ``compute run --local``.

Lab bug: on a ``.deb`` install the system agent's identity is in
``/var/lib/meshforge`` (``0750 meshforge``), so the CLI user found "no agent
identity" and ``--local`` exited 4. Resolution is now: the CLI's own data dir
identity, then the system agent's world-readable public record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from meshforge.cli import job_commands
from meshforge.common import local_node as ln
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
not_root = pytest.mark.skipif(
    os.name == "posix" and os.geteuid() == 0, reason="root reads 0700 dirs"
)


def _user_identity(data_dir: Path, node: str) -> Path:
    d = data_dir / "identity"
    d.mkdir(parents=True)
    (d / "node.json").write_text(
        json.dumps({"format": 1, "node_id": node, "session_epoch": 1})
    )
    return data_dir


def _public(tmp_path: Path, payload: Any, *, mode: int = 0o644) -> Path:
    d = tmp_path / "public"
    d.mkdir()
    d.chmod(0o755)
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    path = d / ln.PUBLIC_NODE_FILE_NAME
    path.write_bytes(data)
    path.chmod(mode)
    return d


def _record(node: str) -> dict[str, Any]:
    return {
        "format": 1,
        "node_id": node,
        "key_fingerprint_sha256": "ab" * 32,
        "agent_version": "0.17.3",
        "written_at": "2026-11-12T05:00:00+00:00",
    }


# ------------------------------------------------------------ order
def test_user_identity_wins_over_the_system_record(tmp_path: Path) -> None:
    mine, system = str(new_node_id()), str(new_node_id())
    data = _user_identity(tmp_path / "data", mine)
    found = ln.resolve_local_node(data, public_dir=_public(tmp_path, _record(system)))
    assert (str(found.node_id), found.source) == (mine, ln.LocalNodeSource.USER)


def test_no_user_identity_falls_back_to_the_system_record(tmp_path: Path) -> None:
    system = str(new_node_id())
    found = ln.resolve_local_node(
        tmp_path / "data", public_dir=_public(tmp_path, _record(system))
    )
    assert (str(found.node_id), found.source) == (system, ln.LocalNodeSource.SYSTEM)


@posix_only
@not_root
def test_unreadable_private_dir_is_skipped_not_fatal(tmp_path: Path) -> None:
    """The packaged case: the CLI in system mode (or a data_dir pointed at
    /var/lib/meshforge) cannot open the owner-only identity - it must use the
    public record, and never loosen or poke at the private dir."""
    data = _user_identity(tmp_path / "data", str(new_node_id()))
    (data / "identity").chmod(0o000)
    try:
        system = str(new_node_id())
        found = ln.resolve_local_node(
            data, public_dir=_public(tmp_path, _record(system))
        )
    finally:
        (data / "identity").chmod(0o700)
    assert (str(found.node_id), found.source) == (system, ln.LocalNodeSource.SYSTEM)


def test_only_node_json_is_ever_opened_in_the_private_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = _public(tmp_path, _record(str(new_node_id())))
    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *a: Any, **k: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    monkeypatch.setattr(os, "listdir", lambda *_a: pytest.fail("listed a directory"))
    ln.resolve_local_node(tmp_path / "data", public_dir=public)
    assert opened == ["node.json"]  # never node.key, never a listing


def test_nothing_anywhere_is_not_found_with_both_remedies(tmp_path: Path) -> None:
    with pytest.raises(ln.LocalNodeError) as info:
        ln.resolve_local_node(tmp_path / "data", public_dir=tmp_path / "nope")
    assert info.value.failure_type is FailureType.NOT_FOUND
    assert "meshforge-agent run" in info.value.message
    assert "agent service" in info.value.message


def test_no_public_dir_platform_still_reports_not_found(tmp_path: Path) -> None:
    with pytest.raises(ln.LocalNodeError) as info:
        ln.resolve_local_node(tmp_path / "data", public_dir=None)
    assert info.value.failure_type is FailureType.NOT_FOUND


# ------------------------------------------------------ garbled record
@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"",
        [1, 2],
        {"format": 2, "node_id": "x"},
        {"format": 1, "node_id": "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"},
        {"format": 1},
        b"{" + b" " * (64 * 1024) + b"}",
    ],
)
def test_garbled_public_record_is_a_clear_config_error(
    tmp_path: Path, payload: Any
) -> None:
    with pytest.raises(ln.LocalNodeError) as info:
        ln.resolve_local_node(tmp_path / "data", public_dir=_public(tmp_path, payload))
    assert info.value.failure_type is FailureType.CONFIG_ERROR
    assert "public node record" in info.value.message


def test_garbled_user_identity_falls_back_to_a_good_public_record(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    (data / "identity").mkdir(parents=True)
    (data / "identity" / "node.json").write_text("{broken")
    system = str(new_node_id())
    found = ln.resolve_local_node(data, public_dir=_public(tmp_path, _record(system)))
    assert found.source is ln.LocalNodeSource.SYSTEM


def test_garbled_user_identity_without_record_reports_the_garbled_file(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    (data / "identity").mkdir(parents=True)
    (data / "identity" / "node.json").write_text("{broken")
    with pytest.raises(ln.LocalNodeError) as info:
        ln.resolve_local_node(data, public_dir=tmp_path / "nope")
    assert info.value.failure_type is FailureType.CONFIG_ERROR
    assert "not JSON" in info.value.message


@posix_only
@pytest.mark.parametrize("mode", [0o666, 0o664])
def test_shared_writable_record_is_refused(tmp_path: Path, mode: int) -> None:
    public = _public(tmp_path, _record(str(new_node_id())), mode=mode)
    with pytest.raises(ln.LocalNodeError, match="writable by other users"):
        ln.read_public_node_id(public)


@posix_only
def test_shared_writable_folder_is_refused(tmp_path: Path) -> None:
    public = _public(tmp_path, _record(str(new_node_id())))
    public.chmod(0o777)
    with pytest.raises(ln.LocalNodeError, match="folder"):
        ln.read_public_node_id(public)


@posix_only
def test_symlinked_record_is_refused(tmp_path: Path) -> None:
    public = _public(tmp_path, _record(str(new_node_id())))
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_record(str(new_node_id()))))
    (public / ln.PUBLIC_NODE_FILE_NAME).unlink()
    (public / ln.PUBLIC_NODE_FILE_NAME).symlink_to(target)
    with pytest.raises(ln.LocalNodeError) as info:
        ln.read_public_node_id(public)
    assert info.value.failure_type is FailureType.CONFIG_ERROR


# ------------------------------------------------------------ paths
def test_system_public_dir_per_platform() -> None:
    assert ln.system_public_dir("linux", {}) == PurePosixPath(
        "/var/lib/meshforge-public"
    )
    assert ln.system_public_dir("win32", {"PROGRAMDATA": r"E:\Data"}) == (
        PureWindowsPath(r"E:\Data\MeshForge\public")
    )
    assert ln.system_public_dir("win32", {}) == PureWindowsPath(
        r"C:\ProgramData\MeshForge\public"
    )
    assert ln.system_public_dir("win32", {"PROGRAMDATA": "relative"}) == (
        PureWindowsPath(r"C:\ProgramData\MeshForge\public")
    )
    assert ln.system_public_dir("darwin", {}) is None


def test_linux_public_dir_is_outside_the_private_state_dir() -> None:
    from meshforge.common.config import SYSTEM_LINUX_DATA_DIR

    public = ln.system_public_dir("linux", {})
    assert public is not None
    assert SYSTEM_LINUX_DATA_DIR not in public.parents  # /var/lib/meshforge stays 0750


def test_packaging_creates_the_public_dir_with_the_right_modes() -> None:
    root = Path(__file__).resolve().parents[3] / "packaging" / "linux"
    postinst = (root / "scripts" / "postinst").read_text()
    assert (
        "install -d -m 0755 -o meshforge -g meshforge /var/lib/meshforge-public"
        in postinst
    )
    assert "chmod go-w /var/lib/meshforge-public" in postinst
    # the private state dir is never loosened
    assert "install -d -m 0750 -o meshforge -g meshforge /var/lib/meshforge\n" in (
        postinst
    )
    unit = (root / "meshforge-agent.service").read_text()
    assert "-/var/lib/meshforge-public" in unit
    assert "/var/lib/meshforge-public" in (root / "scripts" / "postrm").read_text()


# ------------------------------------------------------------ the CLI
def test_cli_local_resolves_the_system_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = str(new_node_id())
    public = _public(tmp_path, _record(system))
    monkeypatch.setattr(ln, "system_public_dir", lambda: public)
    found = job_commands._own_node(tmp_path / "data", required=True)
    assert found is not None and str(found.node_id) == system


def test_cli_local_without_any_agent_exits_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import typer

    monkeypatch.setattr(ln, "system_public_dir", lambda: tmp_path / "nope")
    with pytest.raises((SystemExit, typer.Exit)) as info:
        job_commands._own_node(tmp_path / "data", required=True)
    code = getattr(info.value, "exit_code", getattr(info.value, "code", None))
    assert code == 4
    assert job_commands._own_node(tmp_path / "data", required=False) is None


def test_cli_never_seeds_its_own_cache_for_the_system_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_commands, "_seed", lambda *_a: pytest.fail("seeded the wrong cache")
    )
    node = ln.LocalNode(new_node_id(), ln.LocalNodeSource.SYSTEM)
    assert job_commands._seed_for(node, object(), object()) is False


def test_cli_seeds_for_its_own_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_commands, "_seed", lambda *_a: True)
    node = ln.LocalNode(new_node_id(), ln.LocalNodeSource.USER)
    assert job_commands._seed_for(node, object(), object()) is True
