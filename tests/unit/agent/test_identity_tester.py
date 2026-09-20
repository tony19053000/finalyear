"""Independent tester round for roadmap 0.3.2 (ADR-0013 node identity).

Adversarial tests derived from ADR-0013 and the implementer's own handoff
claims, written without reading ``tests/unit/agent/test_identity.py`` first.
Real files under ``tmp_path``, a real second OS process (``multiprocessing``,
spawn), a real SIGKILL. Never edits ``src/``.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from meshforge.agent.identity import IdentityError, load_or_create, next_epoch

pytestmark = pytest.mark.unit

IS_WINDOWS = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX-only behaviour")
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture
def id_dir(tmp_path: Path) -> Path:
    return tmp_path / "identity"


# --------------------------------------------------------------------- concurrency


def _mp_load(directory: str, out_path: str, barrier: Any) -> None:
    barrier.wait()
    try:
        identity = load_or_create(Path(directory))
        result: dict[str, Any] = {"ok": True, "node_id": str(identity.node_id)}
    except Exception as exc:  # noqa: BLE001 - report to parent, don't crash worker
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    Path(out_path).write_text(json.dumps(result))


def test_concurrent_first_create_never_forks_the_identity(
    id_dir: Path, tmp_path: Path
) -> None:
    """Two processes call ``load_or_create`` on the same *empty* directory at once.

    ADR-0013 §6 / implementer's claim: "exactly one identity must result and both
    must return the same node_id, or one must fail loudly (never two different
    node_ids on disk, never a torn file)". Reproduction: run this test; it forces
    the two workers to start together with a ``multiprocessing.Barrier`` (the
    directory does not exist beforehand so both see the "neither file exists"
    branch). Expected: after both finish, node.json's fingerprint matches
    node.key's fingerprint (i.e. a subsequent load succeeds) and, if both workers
    reported success, they report the *same* node_id.

    Observed on this implementation: ``_create`` has no mutual exclusion (no file
    lock, no O_CREAT|O_EXCL on the *final* path - only on the temp file) so both
    processes can independently generate distinct keys and node_ids and
    interleave their writes; the result can be node.key from one process's keypair
    and node.json from the other's, which then fails fingerprint verification on
    every future load - a permanently bricked identity, not "one must fail
    loudly" (both processes report success; the corruption only surfaces later).
    """
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    out_a, out_b = tmp_path / "out_a.json", tmp_path / "out_b.json"
    procs = [
        ctx.Process(target=_mp_load, args=(str(id_dir), str(out_a), barrier)),
        ctx.Process(target=_mp_load, args=(str(id_dir), str(out_b), barrier)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, "worker process crashed"

    result_a = json.loads(out_a.read_text())
    result_b = json.loads(out_b.read_text())

    # Both workers reporting success with different node_ids on disk, or a
    # fingerprint mismatch surfacing on the next honest load, are both forbidden
    # outcomes per the spec quoted above.
    if result_a["ok"] and result_b["ok"]:
        assert result_a["node_id"] == result_b["node_id"], (
            "two different node_ids were minted for the same directory: "
            f"{result_a} vs {result_b}"
        )
        # The on-disk pair must actually be self-consistent (this is the real
        # bug: both workers can report success with two DIFFERENT node_ids, or
        # report the same node_id while the files on disk are torn/mismatched).
        reloaded = load_or_create(id_dir)  # must not raise IdentityError
        assert str(reloaded.node_id) == result_a["node_id"]


def _mp_next_epoch(directory: str, out_path: str, barrier: Any) -> None:
    barrier.wait()
    identity = load_or_create(Path(directory))
    updated = next_epoch(identity)
    Path(out_path).write_text(json.dumps({"epoch": updated.session_epoch}))


def test_concurrent_next_epoch_never_loses_an_increment(
    id_dir: Path, tmp_path: Path
) -> None:
    """Two processes call ``next_epoch`` on an existing identity at the same time.

    Implementer's claim: "next_epoch from two processes -> both epochs distinct
    and monotone on disk". Reproduction: pre-create the identity, then start two
    workers on a barrier so both read ``node.json`` (epoch 0) before either
    writes. Expected: the two resulting epochs are 1 and 2 (nothing lost).
    Observed: ``next_epoch`` reads-computes-writes with no lock, so both workers
    can read epoch 0 and each write epoch 1 - the increment from the loser is
    silently lost and the final on-disk epoch is 1, not 2. A lost epoch means a
    reused ``session_epoch`` is possible across two agent processes, which is
    exactly what ADR-0002's fencing depends on being unique per stream.
    """
    load_or_create(id_dir)  # epoch 0 exists before the race starts
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    out_a, out_b = tmp_path / "epoch_a.json", tmp_path / "epoch_b.json"
    procs = [
        ctx.Process(target=_mp_next_epoch, args=(str(id_dir), str(out_a), barrier)),
        ctx.Process(target=_mp_next_epoch, args=(str(id_dir), str(out_b), barrier)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    epoch_a = json.loads(out_a.read_text())["epoch"]
    epoch_b = json.loads(out_b.read_text())["epoch"]
    on_disk = json.loads((id_dir / "node.json").read_text())["session_epoch"]

    assert epoch_a != epoch_b, (
        f"both workers got epoch {epoch_a}: an increment was lost"
    )
    assert on_disk == max(epoch_a, epoch_b)


# -------------------------------------------------------------------- epoch bounds


def test_session_epoch_beyond_uint64_range_is_accepted_but_unsendable(
    id_dir: Path,
) -> None:
    """``Hello.session_epoch`` is proto3 ``uint64`` (max 2**64 - 1, ADR-0002 §1).

    Reproduction: hand-write ``node.json`` with ``session_epoch = 2**64`` (one
    past the uint64 max) and load it. Expected (one of): ``load_or_create``
    refuses it with ``IdentityError`` (consistent with "bad node_id" and
    "epoch as float or negative" being refused per the task brief), OR the
    value is silently clamped/rejected before it could ever reach a
    ``Hello`` message. Observed: ``_load`` only checks
    ``isinstance(epoch, int) and not bool and epoch >= 0`` - no upper bound - so
    an out-of-range epoch is accepted into a live ``NodeIdentity``, and
    ``next_epoch`` would carry it forward (2**64 + 1, ...), silently producing a
    value that cannot be encoded into the ``Hello.session_epoch`` proto field
    without raising deep in gRPC serialization far from this module.
    """
    identity = load_or_create(id_dir)
    path = id_dir / "node.json"
    meta = json.loads(path.read_text())
    meta["session_epoch"] = 2**64  # one past uint64 max
    path.write_text(json.dumps(meta))

    reloaded = load_or_create(id_dir)  # implementer expects this NOT to raise today
    assert reloaded.session_epoch == 2**64
    # Demonstrate the real-world consequence: this value cannot become a proto
    # uint64 field, which is the whole reason the epoch exists (ADR-0002 §1).
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    with pytest.raises(ValueError):
        agent_pb2.Hello(session_epoch=reloaded.session_epoch)
    assert identity.node_id == reloaded.node_id


# ----------------------------------------------------------------- adversarial PEM


def test_rsa_private_key_pem_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (id_dir / "node.key").write_bytes(pem)
    with pytest.raises(IdentityError, match="not an ECDSA P-256"):
        load_or_create(id_dir)


def test_p256_public_key_pem_is_refused_not_crash(id_dir: Path) -> None:
    load_or_create(id_dir)
    pub_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    (id_dir / "node.key").write_bytes(pub_pem)
    with pytest.raises(IdentityError):
        load_or_create(id_dir)


def test_pem_with_crlf_line_endings_still_loads(id_dir: Path) -> None:
    """Windows editors may save the key with CRLF; the key must still load."""
    identity = load_or_create(id_dir)
    key_path = id_dir / "node.key"
    crlf = key_path.read_bytes().replace(b"\n", b"\r\n")
    key_path.write_bytes(crlf)
    reloaded = load_or_create(id_dir)
    assert reloaded.node_id == identity.node_id
    assert reloaded.fingerprint == identity.fingerprint


def test_pem_with_utf8_bom_still_loads_or_fails_cleanly(id_dir: Path) -> None:
    """A UTF-8 BOM before the PEM header (common from Windows editors/tools).

    Not a defect: verified as correct behaviour. The underlying ``cryptography``
    PEM parser tolerates a leading BOM (it scans for the ``-----BEGIN`` marker
    rather than requiring it at byte 0), so the key loads with the same
    fingerprint as before. Documented here so a future change that makes this
    raise is a deliberate decision, not a silent regression - either outcome
    (clean load or a clean ``IdentityError``) is acceptable, a raw exception is
    not.
    """
    identity = load_or_create(id_dir)
    key_path = id_dir / "node.key"
    bom_pem = b"\xef\xbb\xbf" + key_path.read_bytes()
    key_path.write_bytes(bom_pem)
    try:
        reloaded = load_or_create(id_dir)
    except IdentityError:
        pass
    else:
        assert reloaded.node_id == identity.node_id


# --------------------------------------------------------------- filesystem shapes


def test_identity_dir_is_a_symlink_to_real_dir(tmp_path: Path) -> None:
    real = tmp_path / "real_identity"
    real.mkdir()
    link = tmp_path / "identity_link"
    if IS_WINDOWS:
        pytest.skip("symlink creation needs elevated privileges on Windows")
    link.symlink_to(real, target_is_directory=True)
    identity = load_or_create(link)
    assert (real / "node.key").exists()
    reloaded = load_or_create(link)
    assert reloaded.node_id == identity.node_id


def test_identity_dir_path_is_a_regular_file(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "identity"
    not_a_dir.write_text("i am a file, not a directory")
    with pytest.raises(IdentityError):
        load_or_create(not_a_dir)


@posix_only
@pytest.mark.skipif(IS_ROOT, reason="root bypasses permission checks")
def test_read_only_parent_dir_gives_identity_error_not_crash(tmp_path: Path) -> None:
    parent = tmp_path / "ro_parent"
    parent.mkdir(mode=0o500)
    target = parent / "identity"
    try:
        with pytest.raises(IdentityError):
            load_or_create(target)
    finally:
        parent.chmod(0o700)  # so pytest can clean up tmp_path


# -------------------------------------------------------------- killed writer


def _write_worker(directory: str, ready_path: str) -> None:
    # Reach into the atomic writer to hang after opening the temp file so the
    # parent can SIGKILL us mid-write, leaving a real orphaned temp file.
    from meshforge.common import atomic as atomic_mod

    real_open = os.open

    def slow_open(path: Any, flags: Any, mode: Any = 0o777) -> int:
        fd = real_open(path, flags, mode)
        Path(ready_path).write_text("ready")
        time.sleep(30)  # parent SIGKILLs us long before this returns
        return fd

    atomic_mod.os.open = slow_open  # type: ignore[attr-defined]
    from meshforge.agent.identity import load_or_create as _loc

    _loc(Path(directory))


@posix_only
def test_sigkill_during_write_leaves_no_load_breaking_state(
    id_dir: Path, tmp_path: Path
) -> None:
    """A writer SIGKILLed mid-write must leave an identity dir a later
    ``load_or_create`` can still use cleanly (temp-file leftovers are inert)."""
    id_dir.mkdir()
    ctx = multiprocessing.get_context("fork")
    ready = tmp_path / "ready"
    proc = ctx.Process(target=_write_worker, args=(str(id_dir), str(ready)))
    proc.start()
    deadline = time.time() + 10
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists(), "worker never reached the slow write"
    time.sleep(0.1)
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5)

    leftover_tmp = [p for p in id_dir.iterdir() if ".tmp" in p.name]
    assert leftover_tmp, "test setup did not actually leave a temp file"

    # A fresh load must succeed and mint a clean identity, ignoring debris.
    identity = load_or_create(id_dir)
    assert identity.node_id
    second = load_or_create(id_dir)
    assert second.node_id == identity.node_id
