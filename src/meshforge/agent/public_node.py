"""The system agent's world-readable public node record (V1.0 G11, D457).

The packaged agent's identity (``node.key`` + ``node.json``) lives in the
machine-wide data dir that only the service account may read. ``compute run
--local`` - typed by the laptop's owner as themselves - still needs to know which
node *this* laptop is. So at every start (and after a key rotation) the system
agent writes ``node-id.json`` into :func:`~meshforge.common.local_node.system_public_dir`
with **only public facts**: the ``node_id``, the SHA-256 of the public key, the
agent version and when it was written (format: ``common/local_node.py``). Never
the key, the certificate, the session epoch, or a path into the private state.

Written atomically: a temporary file in the same folder, made ``0644``
(``fchmod``, so the service's ``UMask=0077`` cannot narrow it), fsynced, then
``os.replace``d - a reader sees the old record or the new one, never half of one.
On Windows the file inherits the folder's ACL (the installer lets Users read
``%ProgramData%\\MeshForge\\public``, ``agent/winservice.acl_plan``).

Best effort: a missing or read-only public folder is logged once as a warning
and the agent runs on (the only loss is ``compute run --local`` on this laptop,
and the warning says how to fix it). User-mode agents (a developer's
``meshforge-agent run``) publish nothing - their own user reads their identity.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from meshforge import __version__
from meshforge.agent.identity import NodeIdentity
from meshforge.common.config import InstallMode, install_mode
from meshforge.common.local_node import (
    PUBLIC_NODE_FORMAT,
    public_node_json,
    system_public_dir,
)
from meshforge.common.logging import get_logger

__all__ = ["PUBLIC_FILE_MODE", "public_record", "publish", "publish_if_system"]

log = get_logger(__name__)

PUBLIC_FILE_MODE: Final = 0o644


def public_record(identity: NodeIdentity, *, version: str = __version__) -> bytes:
    """The record's bytes: public facts only (module docstring)."""
    record = {
        "format": PUBLIC_NODE_FORMAT,
        "node_id": str(identity.node_id),
        "key_fingerprint_sha256": identity.fingerprint,
        "agent_version": version,
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


def publish(identity: NodeIdentity, public_dir: Path) -> Path:
    """Atomically (re)write ``<public_dir>/node-id.json``; raises ``OSError``.

    The folder must exist (the package makes it with the right owner and mode;
    the agent never creates it, so it cannot make one with the wrong ones).
    """
    path = public_node_json(public_dir)
    data = public_record(identity)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, PUBLIC_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            if os.name == "posix":
                os.fchmod(fh.fileno(), PUBLIC_FILE_MODE)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(public_dir)
    return path


def publish_if_system(identity: NodeIdentity) -> Path | None:
    """:func:`publish` into the system public dir when this is the installed
    service (``MESHFORGE_INSTALL_MODE=system``); an ``OSError`` is logged, not
    raised."""
    if install_mode() is not InstallMode.SYSTEM:
        return None
    where = system_public_dir()
    if where is None:
        return None
    public_dir = Path(where)
    try:
        path = publish(identity, public_dir)
    except OSError as exc:
        log.warning(
            "agent.public_node.not_written",
            component="agent",
            node_id=str(identity.node_id),
            path=str(public_dir),
            reason=exc.strerror or type(exc).__name__,
            remedy=(
                "`compute run --local` cannot find this laptop's node until the "
                "folder exists and the service may write it - reinstall or "
                "upgrade the MeshForge agent package"
            ),
        )
        return None
    log.info(
        "agent.public_node.written",
        component="agent",
        node_id=str(identity.node_id),
        path=str(path),
    )
    return path


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
