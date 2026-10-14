"""V0.7 G6 tester round 4: try to break T-18/T-20/T-21 (D164) with hostile input."""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.fsutil import remove_tree, remove_tree_quietly
from meshforge.agent.envs.hash import normalize_requirements
from meshforge.agent.execution.secret_files import (
    SecretFilesError,
    SecretsBaseRefusedError,
    SecretVault,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

S256 = "ab" * 32
URL = "https://files.example/p-1-py3-none-any.whl"


# ---- T-18: attempts to get a wheel URL through without a strong pin -------------
@pytest.mark.parametrize(
    "text",
    [
        f"p @ {URL}?x=1",  # query string after .whl is irrelevant: still a wheel
        f"p @ {URL} --hash=sha256:{S256}x",
        f"p @ {URL} --hash=SHA256:{S256}",
        f"p @ {URL} --hash=sha256:{S256}\n",  # fine alone, see accepted below
    ][:3],
)
def test_t18_wheel_url_variants_without_a_valid_pin_are_refused(text: str) -> None:
    with pytest.raises(EnvError):
        normalize_requirements(text.encode(), wheels_only=True)


def test_t18_pinned_wheel_url_is_accepted_and_unpinned_still_fine_off_wheels_only() -> (
    None
):
    ok = f"p @ {URL} --hash=sha256:{S256}\n".encode()
    assert normalize_requirements(ok, wheels_only=True)
    normalize_requirements(f"p @ {URL}\n".encode(), wheels_only=False)


def test_t18_two_urls_one_hash_second_line_refused() -> None:
    text = f"p @ {URL} --hash=sha256:{S256}\nq @ {URL}\n".encode()
    with pytest.raises(EnvError):
        normalize_requirements(text, wheels_only=True)


# ---- T-21: the base --------------------------------------------------------------
def _vault(tmp_path: Path) -> SecretVault:
    return SecretVault(tmp_path / "root", "t")


def _victim(tmp_path: Path) -> Path:
    v = tmp_path / "victim"
    (v / "att" / "d").mkdir(parents=True)
    (v / "att" / "f").write_bytes(b"keep")
    (v / "att" / "d" / "g").write_bytes(b"keep2")
    return v


def _intact(v: Path) -> None:
    assert (v / "att" / "f").read_bytes() == b"keep"
    assert (v / "att" / "d" / "g").read_bytes() == b"keep2"


def _plant(vault: SecretVault, kind: str, tmp_path: Path) -> None:
    vault.root.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        os.symlink(_victim(tmp_path), vault.base)
    elif kind == "dangling":
        os.symlink(tmp_path / "nowhere", vault.base)
    elif kind == "symlink_to_file":
        f = tmp_path / "f"
        f.write_bytes(b"keep")
        os.symlink(f, vault.base)
    elif kind == "mode755":
        vault.base.mkdir(mode=0o755)
        os.chmod(vault.base, 0o755)  # noqa: S103 - a hostile base on purpose
    elif kind == "mode000":
        vault.base.mkdir()
        os.chmod(vault.base, 0)
    elif kind == "mode777":
        vault.base.mkdir()
        os.chmod(vault.base, 0o777)  # noqa: S103 - a hostile base on purpose
    elif kind == "file":
        vault.base.write_bytes(b"x")
    elif kind == "fifo":
        os.mkfifo(vault.base)


KINDS = [
    "symlink",
    "dangling",
    "symlink_to_file",
    "mode755",
    "mode000",
    "mode777",
    "file",
    "fifo",
]


@pytest.mark.parametrize("kind", KINDS)
def test_t21_materialise_sweep_wipe_refuse_every_hostile_base(
    kind: str, tmp_path: Path
) -> None:
    vault = _vault(tmp_path)
    _plant(vault, kind, tmp_path)
    with pytest.raises(SecretFilesError):
        vault.materialise("att1", {"K": b"v"}, read_only=False)
    assert vault.sweep() == 0
    vault.wipe(vault.folder("att"))  # never raises
    if kind == "symlink":
        _intact(tmp_path / "victim")
        assert not list((tmp_path / "victim").glob("att1"))
    if kind == "symlink_to_file":
        assert (tmp_path / "f").read_bytes() == b"keep"
    with contextlib.suppress(OSError):
        if vault.base.is_symlink():
            vault.base.unlink()
        else:
            os.chmod(vault.base, 0o700)


def test_t21_refusal_is_the_typed_error_not_a_bare_oserror(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _plant(vault, "symlink", tmp_path)
    with pytest.raises(SecretsBaseRefusedError) as info:
        vault.materialise("att1", {"K": b"v"}, read_only=False)
    assert str(tmp_path / "victim") not in str(info.value)
    vault.base.unlink()


def test_t21_symlinked_root_parent_is_not_the_base_check(tmp_path: Path) -> None:
    """The ROOT may legitimately be a symlink (/dev/shm bind); only the base counts."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    vault = SecretVault(link, "t")
    folder = vault.materialise("att1", {"K": b"v"}, read_only=True)
    assert (folder / "K").read_bytes() == b"v"
    assert vault.sweep() == 1
    assert list(vault.base.iterdir()) == []


def test_t21_good_base_wipes_symlink_entries_without_following(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.materialise("a", {"K": b"v"}, read_only=False)
    victim = _victim(tmp_path)
    os.symlink(victim, vault.base / "evil")
    os.symlink(victim / "att" / "f", vault.base / "evilfile")
    os.mkfifo(vault.base / "fifo")
    assert vault.sweep() == 4
    _intact(victim)
    assert list(vault.base.iterdir()) == []


def test_t21_hostile_entry_inside_attempt_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = vault.materialise("a", {"K": b"v"}, read_only=True)
    victim = _victim(tmp_path)
    os.chmod(folder, 0o700)
    os.symlink(victim, folder / "link")
    vault.wipe(folder)
    _intact(victim)
    assert not folder.exists()


def test_t21_base_replaced_by_symlink_between_calls(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.materialise("a", {"K": b"v"}, read_only=False)
    victim = _victim(tmp_path)
    remove_tree(vault.base)
    os.symlink(victim, vault.base)
    assert vault.sweep() == 0
    vault.wipe(vault.folder("att"))
    _intact(victim)
    vault.base.unlink()


# ---- T-20 ------------------------------------------------------------------------
def _tree_000(root: Path) -> Path:
    t = root / "t"
    (t / "a" / "b" / "c").mkdir(parents=True)
    (t / "a" / "b" / "c" / "f").write_bytes(b"x")
    (t / "a" / "f").write_bytes(b"x")
    for p in (t / "a" / "b" / "c", t / "a" / "b", t / "a"):
        os.chmod(p, 0)
    return t


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_t20_nested_mode_000_tree_is_removed(tmp_path: Path) -> None:
    t = _tree_000(tmp_path)
    remove_tree(t)
    assert not t.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_t20_mode_000_directly_under_a_read_only_parent(tmp_path: Path) -> None:
    t = tmp_path / "t"
    (t / "d").mkdir(parents=True)
    (t / "d" / "f").write_bytes(b"x")
    os.chmod(t / "d", 0)
    os.chmod(t, 0o500)
    remove_tree(t)
    assert not t.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_t20_symlink_to_unreadable_dir_inside_tree_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"k")
    t = tmp_path / "t"
    t.mkdir()
    os.symlink(outside, t / "l")
    os.chmod(t, 0o500)
    remove_tree(t)
    assert (outside / "keep").read_bytes() == b"k"
    assert not t.exists()


def test_t20_quietly_never_raises_and_never_typeerror(
    tmp_path: Path, monkeypatch
) -> None:
    import shutil

    def boom(*a, **k):
        raise PermissionError(1, "no")

    monkeypatch.setattr(shutil, "rmtree", boom)
    d = tmp_path / "d"
    d.mkdir()
    assert remove_tree_quietly(d) is False


def test_t20_remove_tree_raises_only_oserror_on_unremovable(
    tmp_path: Path, monkeypatch
) -> None:
    d = tmp_path / "d"
    (d / "x").mkdir(parents=True)
    real_rmdir = os.rmdir

    def deny(p, *a, **k):
        if str(p) == "x" or str(p).endswith("/x"):
            raise PermissionError(13, "denied")
        return real_rmdir(p, *a, **k)

    monkeypatch.setattr(os, "rmdir", deny)
    with pytest.raises(OSError):
        remove_tree(d)
    monkeypatch.undo()
    assert stat.S_ISDIR(os.lstat(d).st_mode)


# ---- T-19: every interleaving of "write returns" and "wait gives up" -------------
def test_t19_secret_write_never_leaves_a_folder_in_any_interleaving(
    tmp_path: Path,
) -> None:
    import random
    import threading
    import time

    from meshforge.agent.execution.secret_files import SecretWrite

    vault = SecretVault(tmp_path / "root", "t")
    rnd = random.Random(19)  # noqa: S311 - a reproducible schedule, not crypto
    for i in range(300):
        write = SecretWrite(vault, f"att{i}")
        delay_run, delay_finish = rnd.random() * 0.002, rnd.random() * 0.002

        def work(write: SecretWrite = write, d: float = delay_run) -> None:
            time.sleep(d)
            with contextlib.suppress(SecretFilesError):
                write.run({"K": b"v"}, read_only=bool(rnd.getrandbits(1)))

        t = threading.Thread(target=work)
        t.start()
        time.sleep(delay_finish)
        write.finish()
        t.join(10)
        assert not t.is_alive()
        assert not vault.folder(f"att{i}").exists(), f"round {i}"
    assert vault.sweep() == 0
