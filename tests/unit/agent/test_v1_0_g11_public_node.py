"""V1.0 G11 (D457): the system agent's world-readable public node record.

The writer (``agent/public_node.py``): public facts only, atomic, ``0644`` even
under the service's ``UMask=0077``, written only by the installed service, a
missing folder logged (never fatal); and the Windows installer's ACL for the
folder (Users read, nothing more).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from meshforge.agent import public_node
from meshforge.agent import winservice as ws
from meshforge.agent.identity import KEY_FILE_NAME, NodeIdentity, load_or_create
from meshforge.common.local_node import (
    PUBLIC_NODE_FILE_NAME,
    LocalNodeSource,
    read_public_node_id,
    resolve_local_node,
)

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes")


@pytest.fixture
def identity(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "private" / "identity")


def _public(tmp_path: Path) -> Path:
    d = tmp_path / "public"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    return d


def test_record_holds_public_facts_only(identity: NodeIdentity) -> None:
    record = json.loads(public_node.public_record(identity, version="9.9.9"))
    assert set(record) == {
        "format",
        "node_id",
        "key_fingerprint_sha256",
        "agent_version",
        "written_at",
    }
    assert record["format"] == 1
    assert record["node_id"] == str(identity.node_id)
    assert record["key_fingerprint_sha256"] == identity.fingerprint
    assert record["agent_version"] == "9.9.9"


def test_record_never_contains_key_material_or_private_paths(
    identity: NodeIdentity,
) -> None:
    raw = public_node.public_record(identity).decode()
    key_pem = (identity.path / KEY_FILE_NAME).read_text()
    body = "".join(
        line for line in key_pem.splitlines() if not line.startswith("-----")
    )
    assert "PRIVATE" not in raw
    assert body[:40] not in raw
    assert str(identity.path) not in raw
    assert "session_epoch" not in raw


@posix_only
def test_publish_is_0644_in_a_0755_dir_even_under_umask_0077(
    identity: NodeIdentity, tmp_path: Path
) -> None:
    public = _public(tmp_path)
    old = os.umask(0o077)
    try:
        path = public_node.publish(identity, public)
    finally:
        os.umask(old)
    assert path == public / PUBLIC_NODE_FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert stat.S_IMODE(public.stat().st_mode) == 0o755
    # only the record: no temporary file left behind
    assert sorted(p.name for p in public.iterdir()) == [PUBLIC_NODE_FILE_NAME]
    # and the CLI side reads it back
    assert read_public_node_id(public) == identity.node_id


def test_publish_replaces_atomically_and_cleans_up_on_failure(
    identity: NodeIdentity, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = _public(tmp_path)
    path = public_node.publish(identity, public)
    before = path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(public_node.os, "replace", boom)
    with pytest.raises(OSError):
        public_node.publish(identity, public)
    assert path.read_bytes() == before  # the old record, whole
    assert sorted(p.name for p in public.iterdir()) == [PUBLIC_NODE_FILE_NAME]


def test_user_mode_agent_publishes_nothing(
    identity: NodeIdentity, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = _public(tmp_path)
    monkeypatch.delenv("MESHFORGE_INSTALL_MODE", raising=False)
    monkeypatch.setattr(public_node, "system_public_dir", lambda: public)
    assert public_node.publish_if_system(identity) is None
    assert list(public.iterdir()) == []


def test_system_agent_publishes_into_the_system_public_dir(
    identity: NodeIdentity, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = _public(tmp_path)
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "system")
    monkeypatch.setattr(public_node, "system_public_dir", lambda: public)
    assert public_node.publish_if_system(identity) == public / PUBLIC_NODE_FILE_NAME
    # the CLI user (no access to the private dir) resolves the same node
    found = resolve_local_node(tmp_path / "cli-user-data", public_dir=public)
    assert found.node_id == identity.node_id
    assert found.source is LocalNodeSource.SYSTEM


def test_missing_public_dir_is_logged_never_fatal(
    identity: NodeIdentity, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "system")
    monkeypatch.setattr(
        public_node, "system_public_dir", lambda: tmp_path / "not-there"
    )
    assert public_node.publish_if_system(identity) is None
    assert not (tmp_path / "not-there").exists()  # never created by the agent


def test_runtime_publishes_at_start_and_after_rotation() -> None:
    """Wiring guard: serve() publishes once the identity is loaded, and the
    renewer republishes after a key rotation (new fingerprint)."""
    import inspect

    from meshforge.agent import runtime

    serve = inspect.getsource(runtime.serve)
    assert "publish_if_system, identity" in serve
    assert serve.index("publish_if_system") < serve.index("load_http_tls")
    renewer = inspect.getsource(runtime.start_renewer)
    assert "publish_if_system(identity)" in renewer
    assert "on_renewed.append(republish)" in renewer


# ------------------------------------------------------------ Windows ACL
def test_windows_public_folder_lets_users_read_and_only_the_service_write() -> None:
    layout = ws.machine_layout(r"C:\ProgramData")
    assert layout.public_dir == layout.root / "public"
    assert layout.public_dir in layout.folders
    svc = ws.service_sid()
    plan = {acl.path: acl for acl in ws.acl_plan(layout, svc)}
    grants = {g.sid: g.access for g in plan[layout.public_dir].grants}
    assert grants == {
        ws.SID_SYSTEM: ws.Access.FULL,
        ws.SID_ADMINISTRATORS: ws.Access.FULL,
        svc: ws.Access.MODIFY,
        ws.SID_USERS: ws.Access.READ,
    }
    assert plan[layout.public_dir].owner_sid is None
    # Users reach no other folder - the identity least of all
    for path, acl in plan.items():
        if path != layout.public_dir:
            assert ws.SID_USERS not in {g.sid for g in acl.grants}


def test_windows_layout_matches_the_cli_lookup() -> None:
    from pathlib import PureWindowsPath

    from meshforge.common.local_node import system_public_dir

    layout = ws.machine_layout(r"D:\PD")
    assert system_public_dir("win32", {"PROGRAMDATA": r"D:\PD"}) == PureWindowsPath(
        layout.public_dir
    )
