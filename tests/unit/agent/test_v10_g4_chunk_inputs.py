"""V0.10 G4 (D260/D278): what one chunk's process sees - placeholders, the
items file (list items are opaque data: JSON-escaped, never in argv or a path)
and the ``lines`` records file."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from meshforge.agent.execution.chunk_inputs import (
    ChunkPaths,
    chunk_env,
    split_view_path,
    substitute,
    write_items_json,
    write_lines_chunk,
)

pytestmark = pytest.mark.unit


def test_placeholders_are_replaced_once_inside_any_argv_item() -> None:
    values = {"items_file": "/w/{out}/i.json", "chunk_index": "7", "out": "/w/o"}
    got = substitute(
        ["python", "run.py", "--in={items_file}", "{chunk_index}", "{out}", "{x}"],
        values,
    )
    # a value that itself contains "{out}" is never expanded again
    assert got == ("python", "run.py", "--in=/w/{out}/i.json", "7", "/w/o", "{x}")


def test_chunk_env_adds_the_three_variables() -> None:
    env = chunk_env({"PATH": "/bin"}, items_file="/a", chunk_index=3, out="/o")
    assert env == {
        "PATH": "/bin",
        "MESHFORGE_CHUNK_ITEMS": "/a",
        "MESHFORGE_CHUNK_INDEX": "3",
        "MESHFORGE_CHUNK_OUT": "/o",
    }


def test_hostile_list_items_stay_data(tmp_path: Path) -> None:
    items = [
        "a\nb",  # a newline cannot add an item
        '"]; rm -rf / #',
        "../../etc/passwd",
        "{out}",
        "é ü " + chr(0x2028),
        "\\x00?",
    ]
    path = tmp_path / "items.json"
    write_items_json(path, items)
    assert json.loads(path.read_text("utf-8")) == items
    assert path.read_text("utf-8").count("\n") == 1  # one JSON document, one line
    with pytest.raises(FileExistsError):  # O_EXCL: never written through
        write_items_json(path, ["x"])


def test_lines_chunk_is_header_plus_records_byte_for_byte(tmp_path: Path) -> None:
    data = b"h1,h2\r\n\xff\xfe1,2\r\n3,4\n5,6"
    header = (0, 7)
    records = [(7, 7), (18, 3)]  # "\xff\xfe1,2\r\n" and "5,6"
    out = tmp_path / "items.txt"
    n = write_lines_chunk(out, io.BytesIO(data), header=header, records=records)
    assert out.read_bytes() == b"h1,h2\r\n\xff\xfe1,2\r\n5,6"
    assert n == len(out.read_bytes())
    with pytest.raises(ValueError, match="shorter"):
        write_lines_chunk(
            tmp_path / "x.txt", io.BytesIO(data), header=None, records=[(20, 99)]
        )


def test_chunk_paths_host_and_container(tmp_path: Path) -> None:
    host = ChunkPaths(tmp_path, lines=False, container_root=None)
    assert host.host_items == tmp_path / ".meshforge" / "chunk" / "items.json"
    assert host.seen_out == str(tmp_path / ".meshforge" / "chunk" / "out")
    docker = ChunkPaths(tmp_path, lines=True, container_root="/work")
    assert docker.seen_items == "/work/.meshforge/chunk/items.txt"
    assert docker.seen_out == "/work/.meshforge/chunk/out"


def test_split_view_path() -> None:
    mounts = ["data", "in/x"]
    assert split_view_path("data/a.jpg", mounts) == ("data", "a.jpg")
    assert split_view_path("in/x/y/z", mounts) == ("in/x", "y/z")
    assert split_view_path("database/a", mounts) == (None, "database/a")
