"""Task 0.3.2: persistent node identity (ADR-0013).

Real files in ``tmp_path``, real corrupt bytes, a real failing ``os.replace``;
nothing depends on this machine's hostname, user name or hardware.
"""

from __future__ import annotations

import json
import os
import pickle
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meshforge.agent import identity as ident
from meshforge.agent.identity import (
    IdentityError,
    NodeIdentity,
    load_or_create,
    next_epoch,
    reset,
)
from meshforge.common.ids import validate_node_id
from meshforge.common.redaction import redact, scrub_text

pytestmark = pytest.mark.unit

IS_WINDOWS = sys.platform.startswith("win")
posix_only = pytest.mark.skipif(IS_WINDOWS, reason="POSIX permission bits")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows icacls ACLs")


@pytest.fixture
def id_dir(tmp_path: Path) -> Path:
    return tmp_path / "identity"


def _files(id_dir: Path) -> dict[str, bytes]:
    """Identity files by name. The permanent, empty ``identity.lock`` is not one."""
    return {
        p.name: p.read_bytes()
        for p in sorted(id_dir.iterdir())
        if p.name != ident.LOCK_FILE_NAME
    }


def _other_key_pem() -> bytes:
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


# ------------------------------------------------------------------- create / load


def test_create_then_load_is_stable_and_idempotent(id_dir: Path) -> None:
    first = load_or_create(id_dir)
    files_after_create = _files(id_dir)
    second = load_or_create(id_dir)

    assert set(files_after_create) == {"node.json", "node.key"}
    assert _files(id_dir) == files_after_create  # second load wrote nothing
    assert second.node_id == first.node_id
    assert second.fingerprint == first.fingerprint
    assert second.session_epoch == first.session_epoch == 0
    assert second.path == id_dir
    assert validate_node_id(first.node_id) == first.node_id
    assert len(first.fingerprint) == 64 and int(first.fingerprint, 16) >= 0
    assert isinstance(first.private_key, ec.EllipticCurvePrivateKey)
    assert isinstance(first.private_key.curve, ec.SECP256R1)
    # Same key on disk as in memory.
    assert (
        second.private_key.private_numbers().private_value
        == first.private_key.private_numbers().private_value
    )


def test_json_layout_matches_adr_0013(id_dir: Path) -> None:
    identity = load_or_create(id_dir)
    meta = json.loads((id_dir / "node.json").read_text(encoding="utf-8"))
    assert meta == {
        "format": 1,
        "node_id": identity.node_id,
        "session_epoch": 0,
        "created_at": meta["created_at"],
        "key_fingerprint_sha256": identity.fingerprint,
    }
    assert meta["created_at"].endswith("+00:00")  # UTC, wall clock only here
    pem = (id_dir / "node.key").read_bytes()
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")  # PKCS#8, unencrypted


def test_two_directories_get_distinct_identities(tmp_path: Path) -> None:
    a = load_or_create(tmp_path / "a")
    b = load_or_create(tmp_path / "b")
    assert a.node_id != b.node_id
    assert a.fingerprint != b.fingerprint


# ------------------------------------------------------------------------- epoch


def test_next_epoch_increments_and_persists_before_returning(id_dir: Path) -> None:
    identity = load_or_create(id_dir)
    for expected in (1, 2, 3):
        identity = next_epoch(identity)
        assert identity.session_epoch == expected
        on_disk = json.loads((id_dir / "node.json").read_text(encoding="utf-8"))
        assert on_disk["session_epoch"] == expected
    reloaded = load_or_create(id_dir)
    assert reloaded.session_epoch == 3
    assert reloaded.node_id == identity.node_id
    assert not [p for p in id_dir.iterdir() if p.suffix == ".tmp"]


def test_next_epoch_never_reuses_an_epoch_already_on_disk(id_dir: Path) -> None:
    stale = load_or_create(id_dir)  # epoch 0 in memory
    other = next_epoch(next_epoch(load_or_create(id_dir)))  # disk now at 2
    assert other.session_epoch == 2
    bumped = next_epoch(stale)
    assert bumped.session_epoch == 3  # not 1


# --------------------------------------------------------------------- atomicity


def test_crash_during_replace_leaves_identity_byte_identical(
    id_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = load_or_create(id_dir)
    before = _files(id_dir)

    def boom(src: Any, dst: Any) -> None:
        raise OSError(5, "simulated crash before rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(IdentityError) as info:
        next_epoch(identity)
    assert info.value.path == id_dir / "node.json"
    assert "simulated crash" in str(info.value)
    monkeypatch.undo()

    assert _files(id_dir) == before  # no temp file, contents untouched
    assert load_or_create(id_dir).session_epoch == 0


def test_crash_during_create_leaves_nothing_or_a_refused_partial(
    id_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real_replace = os.replace

    def fail_second(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # key written, node.json fails
            raise OSError(28, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(IdentityError, match="No space left"):
        load_or_create(id_dir)
    monkeypatch.undo()

    assert set(_files(id_dir)) == {"node.key"}  # a partial identity ...
    with pytest.raises(IdentityError, match="partial identity"):
        load_or_create(id_dir)  # ... which is refused, never silently completed


# --------------------------------------------------------------------- refusals


@pytest.mark.parametrize("remove", ["node.key", "node.json"])
def test_partial_identity_is_refused_for_either_half(id_dir: Path, remove: str) -> None:
    load_or_create(id_dir)
    (id_dir / remove).unlink()
    before = _files(id_dir)
    with pytest.raises(IdentityError) as info:
        load_or_create(id_dir)
    err = info.value
    assert err.problem.startswith("partial identity")
    assert err.path == id_dir / remove
    assert "identity reset" in err.remedy
    assert str(err.path) in str(err) and err.remedy in str(err)
    assert err.details == {"path": str(err.path), "remedy": err.remedy}
    assert _files(id_dir) == before  # nothing minted, nothing touched


def test_corrupt_pem_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    (id_dir / "node.key").write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot base64!\n")
    with pytest.raises(IdentityError, match="corrupt identity key") as info:
        load_or_create(id_dir)
    assert info.value.path == id_dir / "node.key"


def test_truncated_pem_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    pem = (id_dir / "node.key").read_bytes()
    (id_dir / "node.key").write_bytes(pem[: len(pem) // 2])
    with pytest.raises(IdentityError, match="identity key"):
        load_or_create(id_dir)


def test_wrong_key_type_is_refused(id_dir: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import ed25519

    load_or_create(id_dir)
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    (id_dir / "node.key").write_bytes(pem)
    with pytest.raises(IdentityError, match="not an ECDSA P-256"):
        load_or_create(id_dir)


def test_corrupt_json_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    (id_dir / "node.json").write_bytes(b'{"format": 1, "node_id": ')
    with pytest.raises(IdentityError, match="not JSON") as info:
        load_or_create(id_dir)
    assert info.value.path == id_dir / "node.json"


def test_json_that_is_not_an_object_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    (id_dir / "node.json").write_bytes(b"[1, 2]")
    with pytest.raises(IdentityError, match="not a JSON object"):
        load_or_create(id_dir)


def _rewrite_json(id_dir: Path, **changes: Any) -> None:
    path = id_dir / "node.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(changes)
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_unsupported_format_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    _rewrite_json(id_dir, format=2)
    with pytest.raises(IdentityError, match=r"unsupported identity format 2") as info:
        load_or_create(id_dir)
    assert "upgrade the agent" in info.value.remedy


@pytest.mark.parametrize(
    "bad_node_id", ["", "job_01J00000000000000000000000", "node_notaulid", 42, None]
)
def test_invalid_node_id_is_refused(id_dir: Path, bad_node_id: Any) -> None:
    load_or_create(id_dir)
    _rewrite_json(id_dir, node_id=bad_node_id)
    with pytest.raises(IdentityError, match="corrupt identity"):
        load_or_create(id_dir)


@pytest.mark.parametrize("bad_epoch", [-1, "3", 1.5, True, None])
def test_invalid_epoch_is_refused(id_dir: Path, bad_epoch: Any) -> None:
    load_or_create(id_dir)
    _rewrite_json(id_dir, session_epoch=bad_epoch)
    with pytest.raises(IdentityError, match="session_epoch"):
        load_or_create(id_dir)


@pytest.mark.security
def test_swapped_key_fails_fingerprint_check(id_dir: Path) -> None:
    identity = load_or_create(id_dir)
    (id_dir / "node.key").write_bytes(_other_key_pem())  # valid key, wrong one
    with pytest.raises(IdentityError, match=r"does not match node\.json") as info:
        load_or_create(id_dir)
    assert info.value.path == id_dir / "node.key"
    # The untouched node.json still names the original node.
    meta = json.loads((id_dir / "node.json").read_text(encoding="utf-8"))
    assert meta["node_id"] == identity.node_id


def test_identity_error_pickles(id_dir: Path) -> None:
    err = IdentityError("partial identity", id_dir / "node.key", "reset it")
    back = pickle.loads(pickle.dumps(err))
    assert isinstance(back, IdentityError)
    assert (back.problem, back.path, back.remedy) == (err.problem, err.path, err.remedy)
    assert str(back) == str(err)


# ------------------------------------------------------------------------- reset


def test_reset_deletes_both_files_and_is_idempotent(id_dir: Path) -> None:
    first = load_or_create(id_dir)
    reset(id_dir)
    assert _files(id_dir) == {}
    reset(id_dir)  # nothing to delete: fine
    reset(id_dir / "never-existed")  # missing directory: fine too
    second = load_or_create(id_dir)
    assert second.node_id != first.node_id


# ---------------------------------------------------------------------- secrecy


@pytest.mark.security
def test_no_key_material_in_repr_str_redact_or_logs(id_dir: Path) -> None:
    with structlog.testing.capture_logs() as captured:
        identity = load_or_create(id_dir)
        identity = next_epoch(identity)
        try:
            (id_dir / "node.json").unlink()
            load_or_create(id_dir)
        except IdentityError as exc:
            texts = [repr(exc), str(exc), json.dumps(exc.to_dict())]
        else:  # pragma: no cover
            pytest.fail("partial identity accepted")

    private_value = identity.private_key.private_numbers().private_value
    pem_body = (id_dir / "node.key").read_text(encoding="utf-8").splitlines()[1]
    texts += [repr(identity), str(identity), json.dumps(redact(identity), default=str)]
    texts += [json.dumps(event, default=str) for event in captured]
    assert captured, "identity module must log"
    for text in texts:
        assert "PRIVATE KEY" not in text
        assert pem_body not in text
        assert hex(private_value)[2:] not in text
        assert str(private_value) not in text
    assert redact(identity)["private_key"] == "[REDACTED]"
    events = {e["event"] for e in captured}
    assert {"identity.created", "identity.epoch_advanced"} <= events
    assert all(e.get("node_id") == identity.node_id for e in captured if "node_id" in e)


def test_scrub_text_removes_pem_private_key_blocks() -> None:
    pem = _other_key_pem().decode("ascii")
    body = pem.splitlines()[1]
    scrubbed = scrub_text(f"loaded key\n{pem}\nafter")
    assert body not in scrubbed
    assert scrubbed.startswith("loaded key\n-----BEGIN PRIVATE KEY----- [REDACTED] ")
    assert scrubbed.endswith("\nafter")
    # Unclosed block: redacted to the end of the text.
    assert body not in scrub_text(f"x {pem.split('-----END')[0]}")
    # Other PEM labels too; a block without "PRIVATE KEY" is left alone.
    ec_block = "-----BEGIN EC PRIVATE KEY-----\nAAA\n-----END EC PRIVATE KEY-----"
    assert scrub_text(ec_block) == "-----BEGIN EC PRIVATE KEY----- [REDACTED] "
    cert = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----"
    assert scrub_text(cert) == cert


# --------------------------------------------------------------------- protection


@pytest.mark.security
@posix_only
def test_posix_modes_are_restricted(id_dir: Path) -> None:
    load_or_create(id_dir)
    assert stat.S_IMODE(id_dir.stat().st_mode) == 0o700
    for name in ("node.key", "node.json"):
        assert stat.S_IMODE((id_dir / name).stat().st_mode) == 0o600, name


@pytest.mark.security
@posix_only
def test_posix_pre_existing_loose_dir_is_tightened(id_dir: Path) -> None:
    id_dir.mkdir(mode=0o755)
    load_or_create(id_dir)
    assert stat.S_IMODE(id_dir.stat().st_mode) == 0o700


@pytest.mark.security
@posix_only
def test_posix_loose_file_mode_is_refused(id_dir: Path) -> None:
    load_or_create(id_dir)
    (id_dir / "node.key").chmod(0o644)
    with pytest.raises(IdentityError, match="accessible to other users") as info:
        load_or_create(id_dir)
    assert "chmod 600" in info.value.remedy


@pytest.mark.security
@windows_only
def test_windows_acl_grants_only_the_current_user(id_dir: Path) -> None:
    load_or_create(id_dir)
    for path in (id_dir, id_dir / "node.key", id_dir / "node.json"):
        out = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, check=True
        ).stdout
        grants = [line for line in out.splitlines() if ":(" in line]
        assert len(grants) == 1, out
        assert "S-1-1-0" not in out and "Everyone" not in out, out
        assert "BUILTIN\\Users" not in out and "S-1-5-32-545" not in out, out


def test_protect_dispatches_by_platform(
    id_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On non-Windows the Windows path must be unreachable; exercise the failure
    # branch by simulating a non-zero icacls exit.
    id_dir.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ident, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(ident, "current_logon_sid", lambda: "S-1-5-5-0-7")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:1] == ["icacls"]
        # the one grant (+ removals); a folder's is inheritable (D399)
        assert "*S-1-5-21-1-2-3-1001:(OI)(CI)(F)" in argv
        return subprocess.CompletedProcess(
            argv, 5, stdout="", stderr="Access is denied."
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(IdentityError, match=r"icacls failed \(exit 5\)") as info:
        ident._protect(id_dir)
    assert info.value.path == id_dir


def test_sid_parsing_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="not,csv\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(IdentityError, match="SID"):
        ident._current_user_sid()

    def fake_run_ok(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        csv_out = '"User Name","SID"\n"host\\user","S-1-5-21-1-2-3-1001"\n'
        return subprocess.CompletedProcess(argv, 0, stdout=csv_out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    assert ident._current_user_sid() == "S-1-5-21-1-2-3-1001"


def test_node_identity_is_frozen(id_dir: Path) -> None:
    identity = load_or_create(id_dir)
    with pytest.raises(AttributeError):
        identity.session_epoch = 5  # type: ignore[misc]
    assert isinstance(identity, NodeIdentity)


def test_identity_logs_never_name_the_directory(tmp_path: Path) -> None:
    """Checklist item 7: no full paths of user dirs in logs. The identity dir lives
    under the user's home (it names the user); node_id + fingerprint identify it."""
    id_dir = tmp_path / "some-user" / "identity"
    with structlog.testing.capture_logs() as captured:
        created = load_or_create(id_dir)
        loaded = load_or_create(id_dir)
        ident.reset(id_dir)
    events = {e["event"]: e for e in captured}
    assert {"identity.created", "identity.loaded", "identity.reset"} <= set(events)
    for event in captured:
        text = json.dumps(event, default=str)
        assert str(tmp_path) not in text, event
        assert "some-user" not in text, event
    assert events["identity.created"]["node_id"] == created.node_id
    assert events["identity.created"]["key_fingerprint"] == created.fingerprint
    assert events["identity.loaded"]["key_fingerprint"] == loaded.fingerprint
