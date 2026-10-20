"""V0.10 G5 tester r2 (T1): a part file with more than one hard link is
``unsafe_part`` for every built-in, wherever it sits, with no output kept."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.combine import BUILTINS, CombineError, combine

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX hard links"),
]

DATA = {
    "concat": ("o.txt", b"x"),
    "merge-csv": ("o.csv", b"h\n1\n"),
    "merge-json": ("o.json", b"[1]"),
}


def _parts(tmp: Path, kind: str, n: int = 3) -> tuple[Path, Path]:
    name, data = DATA[kind]
    base = tmp / "parts"
    base.mkdir()
    for i in range(n):
        d = base / f"{i:05d}"
        d.mkdir()
        (d / name).write_bytes(data)
    out = tmp / "out"
    out.mkdir()
    return base, out


def _run(base: Path, out: Path) -> None:
    kind = next(k for k, (n, _) in DATA.items() if (base / "00000" / n).exists())
    combine(kind, base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)


@pytest.mark.parametrize("kind", BUILTINS)
def test_same_inode_in_two_parts_is_unsafe(tmp_path: Path, kind: str) -> None:
    base, out = _parts(tmp_path, kind)
    name = DATA[kind][0]
    (base / "00002" / name).unlink()
    os.link(base / "00001" / name, base / "00002" / name)
    with pytest.raises(CombineError) as exc:
        combine(kind, base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert exc.value.code == "unsafe_part"


@pytest.mark.parametrize("kind", BUILTINS)
def test_link_to_an_outside_file_in_the_first_and_last_part(
    tmp_path: Path, kind: str
) -> None:
    name = DATA[kind][0]
    for idx in ("00000", "00002"):
        root = tmp_path / idx
        root.mkdir()
        base, out = _parts(root, kind)
        outside = root / "outside"
        outside.write_bytes(DATA[kind][1])
        (base / idx / name).unlink()
        os.link(outside, base / idx / name)
        with pytest.raises(CombineError) as exc:
            combine(kind, base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
        assert exc.value.code == "unsafe_part"


def test_link_in_a_nested_directory_is_unsafe(tmp_path: Path) -> None:
    base, out = _parts(tmp_path, "concat")
    nested = base / "00001" / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "f").write_bytes(b"1")
    os.link(nested / "f", tmp_path / "elsewhere")
    with pytest.raises(CombineError) as exc:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert exc.value.code == "unsafe_part"


def test_a_link_whose_other_name_is_gone_is_a_plain_file(tmp_path: Path) -> None:
    """nlink back to 1: nothing outside shares the inode, so it is fine."""
    base, out = _parts(tmp_path, "concat")
    extra = tmp_path / "extra"
    os.link(base / "00001" / "o.txt", extra)
    extra.unlink()
    combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert (out / "o.txt").read_bytes() == b"xxx"


def test_refusal_message_does_not_leak_file_content(tmp_path: Path) -> None:
    base, out = _parts(tmp_path, "concat")
    secret = tmp_path / "secret"
    secret.write_text("TOPSECRET")
    (base / "00001" / "o.txt").unlink()
    os.link(secret, base / "00001" / "o.txt")
    with pytest.raises(CombineError) as exc:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert "TOPSECRET" not in str(exc.value)
    assert exc.value.code == "unsafe_part"
