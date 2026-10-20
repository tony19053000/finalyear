"""V0.10 G4 tester r3: attacks on ChunkFolder.pack_out / remove (B1 fix).

Contents of ``out/`` a hostile chunk can plant: links to host files/dirs,
links on the way, hardlinks, fifos, devices, oversize data, a racing swap of an
inner folder while the agent packs, a folder swapped and swapped back.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from meshforge.agent.execution.chunk_folder import ChunkFolder
from meshforge.agent.execution.outputs import OutputError
from meshforge.common.bundle import unpack

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX links"),
]

SECRET = b"HOST-SECRET-" + b"k" * 200


def _setup(tmp_path: Path) -> tuple[ChunkFolder, Path]:
    secret = tmp_path / "hostdir"
    secret.mkdir()
    (secret / "key").write_bytes(SECRET)
    root = tmp_path / "work"
    root.mkdir()
    folder = ChunkFolder(root, "att_t", lines=False, docker=False)
    folder.create()
    return folder, secret


def _packed_has_secret(folder: ChunkFolder, tmp_path: Path) -> bool:
    dest = tmp_path / "o.bundle"
    folder.pack_out(dest, 1 << 20)
    target = tmp_path / "unpacked"
    unpack(dest, target)
    return any(SECRET in p.read_bytes() for p in target.rglob("*") if p.is_file())


def test_file_symlink_to_a_host_file_is_refused(tmp_path: Path) -> None:
    folder, secret = _setup(tmp_path)
    os.symlink(secret / "key", folder.out / "leak.txt")
    with pytest.raises(OutputError):
        folder.pack_out(tmp_path / "o.bundle", 1 << 20)
    assert not (tmp_path / "o.bundle").exists()


def test_symlinked_inner_folder_is_refused_or_skipped_never_followed(
    tmp_path: Path,
) -> None:
    folder, secret = _setup(tmp_path)
    os.symlink(secret, folder.out / "sub")
    (folder.out / "ok.txt").write_text("fine")
    with contextlib.suppress(OutputError):  # refusing is also acceptable
        assert not _packed_has_secret(folder, tmp_path)


def test_fifo_does_not_hang_the_agent_and_is_refused(tmp_path: Path) -> None:
    folder, _ = _setup(tmp_path)
    os.mkfifo(folder.out / "pipe")
    result: list[object] = []

    def run() -> None:
        try:
            folder.pack_out(tmp_path / "o.bundle", 1 << 20)
            result.append("packed")
        except OutputError as exc:
            result.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "pack_out blocked on a fifo"
    assert result and isinstance(result[0], OutputError)


def test_device_node_is_refused(tmp_path: Path) -> None:
    folder, _ = _setup(tmp_path)
    try:
        os.mknod(folder.out / "dev", stat.S_IFCHR | 0o600, os.makedev(1, 3))
    except PermissionError:
        pytest.skip("cannot mknod without root")
    with pytest.raises(OutputError):
        folder.pack_out(tmp_path / "o.bundle", 1 << 20)


def test_hardlink_to_a_readable_host_file_documented_residual(
    tmp_path: Path,
) -> None:
    """A hardlink is indistinguishable from a regular file (nlink>1 is the
    only tell). Under Docker the container cannot create one to a host file it
    cannot see; unsandboxed, the job's user could simply copy the file. This
    records the behaviour: it IS packed."""
    folder, secret = _setup(tmp_path)
    os.link(secret / "key", folder.out / "hard")
    assert _packed_has_secret(folder, tmp_path) is True


def test_output_over_the_cap_is_refused_and_leaves_no_bundle(tmp_path: Path) -> None:
    folder, _ = _setup(tmp_path)
    (folder.out / "big.bin").write_bytes(os.urandom(300_000))
    with pytest.raises(OutputError):
        folder.pack_out(tmp_path / "o.bundle", 4096)
    assert not (tmp_path / "o.bundle").exists()


def test_out_swapped_away_and_back_same_inode_is_the_same_folder(
    tmp_path: Path,
) -> None:
    folder, secret = _setup(tmp_path)
    (folder.out / "a.txt").write_text("a")
    moved = folder.root / "out.moved"
    folder.out.rename(moved)
    os.symlink(secret, folder.out)
    os.unlink(folder.out)
    moved.rename(folder.out)
    assert not _packed_has_secret(folder, tmp_path)


def test_out_swapped_and_left_as_link_is_refused(tmp_path: Path) -> None:
    folder, secret = _setup(tmp_path)
    folder.out.rmdir()
    os.symlink(secret, folder.out)
    with pytest.raises(OutputError):
        folder.pack_out(tmp_path / "o.bundle", 1 << 20)


def test_racing_inner_folder_swap_while_packing_never_leaks(tmp_path: Path) -> None:
    """A process that outlives the chunk flips out/sub between a real folder
    and a link to a host dir while the agent packs, 150 times."""
    folder, secret = _setup(tmp_path)
    sub = folder.out / "sub"
    spare = tmp_path / "spare"
    for i in range(40):
        (spare if False else folder.out).joinpath(f"pad{i}.bin").write_bytes(
            b"p" * 50_000
        )
    sub.mkdir()
    for i in range(40):
        (sub / f"f{i}.txt").write_bytes(b"honest" * 100)
    stop = threading.Event()

    def flip() -> None:
        hidden = folder.out / "sub.real"
        while not stop.is_set():
            try:
                sub.rename(hidden)
                os.symlink(secret, sub)
                os.unlink(sub)
                hidden.rename(sub)
            except OSError:
                try:
                    if sub.is_symlink():
                        os.unlink(sub)
                    if hidden.exists() and not sub.exists():
                        hidden.rename(sub)
                except OSError:
                    pass

    t = threading.Thread(target=flip, daemon=True)
    t.start()
    leaks = 0
    raw_oserrors: list[int] = []
    try:
        for i in range(150):
            dest = tmp_path / f"o{i}.bundle"
            try:
                folder.pack_out(dest, 1 << 24)
            except OutputError:
                continue
            except OSError:
                raw_oserrors.append(i)  # D3: escapes the agent's except clause
                continue
            target = tmp_path / f"u{i}"
            try:
                unpack(dest, target)
            except Exception:  # noqa: BLE001, S112
                continue
            leaks += sum(
                SECRET in p.read_bytes() for p in target.rglob("*") if p.is_file()
            )
    finally:
        stop.set()
        t.join(5)
    assert leaks == 0, f"{leaks} leaked files across the race"
    assert not raw_oserrors, (
        f"pack_out raised a raw OSError {len(raw_oserrors)}x (not OutputError): "
        "lane_chunk only catches (OutputError, UploadError)"
    )


def test_remove_with_root_replaced_by_link_never_touches_the_target(
    tmp_path: Path,
) -> None:
    folder, secret = _setup(tmp_path)
    import shutil

    shutil.rmtree(folder.root)
    os.symlink(secret, folder.root)
    folder.remove()
    assert (secret / "key").read_bytes() == SECRET
    assert not os.path.lexists(folder.root)


def test_remove_with_a_link_inside_out_never_follows_it(tmp_path: Path) -> None:
    folder, secret = _setup(tmp_path)
    os.symlink(secret, folder.out / "dirlink")
    os.symlink(secret / "key", folder.out / "filelink")
    folder.remove()
    assert (secret / "key").read_bytes() == SECRET


def test_a_link_planted_at_the_chunk_folder_path_is_unlinked_not_walked(
    tmp_path: Path,
) -> None:
    folder, secret = _setup(tmp_path)
    import shutil

    shutil.rmtree(folder.root)
    os.symlink(secret, folder.root)
    folder.create()  # the next chunk of this lane: must not recurse into it
    assert (secret / "key").read_bytes() == SECRET
    assert folder.out.is_dir() and not folder.out.is_symlink()
    assert not os.path.samefile(folder.root, secret)


def test_control_folder_is_owner_only(tmp_path: Path) -> None:
    folder, _ = _setup(tmp_path)
    for p in (folder.root, folder.out):
        assert stat.S_IMODE(p.stat().st_mode) & 0o077 == 0, oct(p.stat().st_mode)
