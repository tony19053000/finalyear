"""V0.10 G5 (D268/D283): the built-in combines, golden cases.

Each case writes ``parts/NNNNN/...`` exactly as given, runs the built-in and
compares every output file with its golden bytes (or the refusal's code).
Covered: empty chunks, files in only some chunks, CRLF, BOM, no final newline,
header-only and empty CSV parts, header mismatch, duplicate keys (across parts
and inside one document), mixed / scalar JSON, invalid JSON, size caps,
symlinks and other unsafe entries, a file/folder clash, determinism under
shuffled creation order.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from meshforge.agent.execution.combine import (
    EXIT_COMBINE_ERROR,
    CombineError,
    combine,
    main,
)

pytestmark = [pytest.mark.unit]

POSIX = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX links/FIFOs")

Parts = list[Mapping[str, bytes]]


def _parts(root: Path, parts: Parts, *, width: int = 5) -> Path:
    base = root / "parts"
    base.mkdir()
    for index, files in enumerate(parts):
        part = base / f"{index:0{width}d}"
        part.mkdir()
        for rel, data in files.items():
            path = part / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return base


def _run(
    tmp_path: Path,
    kind: str,
    parts: Parts,
    *,
    max_output_bytes: int = 1 << 30,
    json_max_bytes: int = 1 << 20,
) -> dict[str, bytes]:
    base = _parts(tmp_path, parts)
    out = tmp_path / "out"
    out.mkdir()
    combine(
        kind,
        base,
        out,
        max_output_bytes=max_output_bytes,
        json_max_bytes=json_max_bytes,
    )
    return {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }


def _refused(tmp_path: Path, kind: str, parts: Parts, **kw: int) -> CombineError:
    with pytest.raises(CombineError) as caught:
        _run(tmp_path, kind, parts, **kw)
    return caught.value


# ------------------------------------------------------------------ concat


def test_concat_in_chunk_order_with_empty_chunks_and_partial_files(
    tmp_path: Path,
) -> None:
    got = _run(
        tmp_path,
        "concat",
        [
            {"labels.txt": b"a\n", "sub/x.bin": b"\x00\x01"},
            {},  # an empty chunk: wrote nothing
            {"labels.txt": b"b"},  # no final newline: nothing is inserted
            {"labels.txt": b"c\n", "only3.txt": b"three"},
            {"sub/x.bin": b"\xff"},
        ],
    )
    assert got == {
        "labels.txt": b"a\nbc\n",
        "only3.txt": b"three",
        "sub/x.bin": b"\x00\x01\xff",
    }


def test_concat_of_all_empty_chunks_is_empty(tmp_path: Path) -> None:
    assert _run(tmp_path, "concat", [{}, {}, {}]) == {}


def test_concat_keeps_empty_files(tmp_path: Path) -> None:
    got = _run(tmp_path, "concat", [{"e.txt": b""}, {"e.txt": b""}])
    assert got == {"e.txt": b""}


def test_part_order_is_numeric_not_creation_order(tmp_path: Path) -> None:
    """Chunk folders created in a shuffled order give the same bytes."""
    base = tmp_path / "parts"
    base.mkdir()
    order = list(range(12))
    random.Random(7).shuffle(order)  # noqa: S311 - a fixed test order
    for index in order:
        part = base / f"{index:05d}"
        part.mkdir()
        (part / "r.txt").write_bytes(f"{index}\n".encode())
    out = tmp_path / "out"
    out.mkdir()
    combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert (out / "r.txt").read_bytes() == b"".join(
        f"{i}\n".encode() for i in range(12)
    )


# ------------------------------------------------------------------ merge-csv

HEADER = b"path,label"


def test_merge_csv_crlf_bom_and_missing_final_newline(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [
            {"out.csv": b"\xef\xbb\xbf" + HEADER + b"\r\na,1\r\nb,2"},  # no final CRLF
            {"out.csv": HEADER + b"\nc,3\n"},  # LF header: same header text
            {"out.csv": b"\xef\xbb\xbf" + HEADER + b"\r\nd,4\r\n"},  # BOM dropped
            {"notes.txt": b"kept as is"},  # not a CSV: concatenated
        ],
    )
    assert got == {
        # first part's BOM + header kept; its missing terminator gets CRLF
        "out.csv": b"\xef\xbb\xbf" + HEADER + b"\r\na,1\r\nb,2\r\nc,3\n" + b"d,4\r\n",
        "notes.txt": b"kept as is",
    }


def test_merge_csv_header_only_and_empty_parts(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [
            {"t.CSV": b""},  # empty file: no header, no rows
            {"t.CSV": HEADER},  # header only, no terminator
            {"t.CSV": HEADER + b"\n"},  # header only
            {"t.CSV": HEADER + b"\nx,9"},
        ],
    )
    # the first header found is kept and terminated (LF: it had none)
    assert got == {"t.CSV": HEADER + b"\nx,9\n"}


def test_merge_csv_last_part_without_newline_is_terminated(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [{"o.csv": HEADER + b"\r\na,1\r\n"}, {"o.csv": HEADER + b"\r\nb,2"}],
    )
    assert got == {"o.csv": HEADER + b"\r\na,1\r\nb,2\r\n"}


def test_merge_csv_header_mismatch_is_refused(tmp_path: Path) -> None:
    err = _refused(
        tmp_path,
        "merge-csv",
        [
            {"o.csv": HEADER + b"\na,1\n"},
            {"o.csv": b"path,label \nb,2\n"},  # one trailing space: different
        ],
    )
    assert err.code == "header_mismatch"
    assert "o.csv" in err.message
    assert "00001" in err.message


def test_merge_csv_header_case_differs_is_refused(tmp_path: Path) -> None:
    err = _refused(
        tmp_path,
        "merge-csv",
        [{"o.csv": b"a,b\n"}, {"o.csv": b"A,b\n"}],
    )
    assert err.code == "header_mismatch"


def test_merge_csv_huge_header_line_is_too_large(tmp_path: Path) -> None:
    err = _refused(tmp_path, "merge-csv", [{"o.csv": b"x" * (1024 * 1024 + 10)}])
    assert err.code == "too_large"


# ------------------------------------------------------------------ merge-json


def test_merge_json_arrays_in_chunk_order(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-json",
        [
            {"r.json": b'[1, {"a": "\xc3\xa9"}]'},
            {"r.json": b"[]"},
            {},
            {"r.json": b"\xef\xbb\xbf[2.5, null, true]\n"},
            {"other.txt": b"x"},
        ],
    )
    assert got == {
        "r.json": '[1,{"a":"é"},2.5,null,true]\n'.encode(),
        "other.txt": b"x",
    }


def test_merge_json_objects_merge_in_chunk_order(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-json",
        [{"m.json": b'{"b": 1, "a": [1]}'}, {"m.json": b'{"c": {"d": 2}}'}],
    )
    assert got == {"m.json": b'{"b":1,"a":[1],"c":{"d":2}}\n'}


def test_merge_json_all_empty_arrays(tmp_path: Path) -> None:
    got = _run(tmp_path, "merge-json", [{"r.json": b"[]"}, {"r.json": b"[ ]"}])
    assert got == {"r.json": b"[]\n"}


def test_merge_json_duplicate_key_across_parts_is_refused(tmp_path: Path) -> None:
    err = _refused(
        tmp_path,
        "merge-json",
        [{"m.json": b'{"k": 1}'}, {"m.json": b'{"j": 2, "k": 1}'}],
    )
    assert err.code == "duplicate_key"
    assert "'k'" in err.message


def test_merge_json_duplicate_key_inside_a_document_is_refused(tmp_path: Path) -> None:
    err = _refused(tmp_path, "merge-json", [{"m.json": b'[{"x": 1, "x": 2}]'}])
    assert err.code == "duplicate_key"


@pytest.mark.parametrize(
    ("first", "second"),
    [(b"[1]", b'{"a": 1}'), (b'{"a": 1}', b"[1]"), (b"[1]", b"3"), (b'"s"', b"[1]")],
)
def test_merge_json_mixed_or_scalar_top_level_is_type_mismatch(
    tmp_path: Path, first: bytes, second: bytes
) -> None:
    err = _refused(tmp_path, "merge-json", [{"m.json": first}, {"m.json": second}])
    assert err.code == "type_mismatch"


@pytest.mark.parametrize(
    "data", [b"", b"[1,", b"[NaN]", b"[Infinity]", b"[1e400]", b"\xff[1]"]
)
def test_merge_json_invalid_documents(tmp_path: Path, data: bytes) -> None:
    err = _refused(tmp_path, "merge-json", [{"m.json": data}])
    assert err.code == "invalid_json"


def test_merge_json_file_over_the_parse_cap_is_too_large(tmp_path: Path) -> None:
    big = b"[" + b",".join(b"1" for _ in range(2000)) + b"]"
    err = _refused(tmp_path, "merge-json", [{"m.json": big}], json_max_bytes=1024)
    assert err.code == "too_large"
    assert "combine_json_max_bytes" in err.message


# ------------------------------------------------------------------ caps / safety


def test_output_over_max_bytes_is_too_large(tmp_path: Path) -> None:
    err = _refused(
        tmp_path,
        "concat",
        [{"a": b"x" * 600}, {"a": b"y" * 600}],
        max_output_bytes=1000,
    )
    assert err.code == "too_large"


def test_file_in_one_part_folder_in_another_is_type_mismatch(tmp_path: Path) -> None:
    err = _refused(tmp_path, "concat", [{"a": b"1"}, {"a/b": b"2"}])
    assert err.code == "type_mismatch"


@POSIX
def test_symlink_part_file_is_unsafe(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"private")
    base = _parts(tmp_path, [{"a.txt": b"1"}, {}])
    (base / "00001" / "a.txt").symlink_to(secret)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert caught.value.code == "unsafe_part"
    assert b"private" not in b"".join(
        p.read_bytes() for p in out.rglob("*") if p.is_file()
    )


@POSIX
def test_symlink_inside_a_part_pointing_inside_is_still_unsafe(tmp_path: Path) -> None:
    base = _parts(tmp_path, [{"a.txt": b"1"}])
    (base / "00000" / "b.txt").symlink_to("a.txt")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("merge-csv", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


@POSIX
def test_symlinked_part_folder_is_unsafe(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.txt").write_bytes(b"x")
    base = _parts(tmp_path, [{"a.txt": b"1"}])
    (base / "00001").symlink_to(elsewhere, target_is_directory=True)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


@POSIX
def test_symlinked_folder_inside_a_part_is_unsafe(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    base = _parts(tmp_path, [{"a.txt": b"1"}])
    (base / "00000" / "sub").symlink_to(elsewhere, target_is_directory=True)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


@POSIX
def test_fifo_in_a_part_is_unsafe(tmp_path: Path) -> None:
    base = _parts(tmp_path, [{"a.txt": b"1"}])
    os.mkfifo(base / "00000" / "pipe")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


@pytest.mark.parametrize("names", [["00000", "00002"], ["0", "01"], ["00000", "x"]])
def test_part_folders_must_be_chunks_zero_to_n(
    tmp_path: Path, names: list[str]
) -> None:
    base = tmp_path / "parts"
    base.mkdir()
    for name in names:
        (base / name).mkdir()
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


def test_a_file_among_the_part_folders_is_unsafe(tmp_path: Path) -> None:
    base = _parts(tmp_path, [{"a": b"1"}])
    (base / "00001").write_bytes(b"not a folder")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as caught:
        combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert caught.value.code == "unsafe_part"


def test_wide_part_names_for_many_chunks(tmp_path: Path) -> None:
    """A job with more than 99 999 chunks gets wider names; one width each."""
    base = _parts(tmp_path, [{"a": b"1"}, {"a": b"2"}], width=6)
    out = tmp_path / "out"
    out.mkdir()
    combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1)
    assert (out / "a").read_bytes() == b"12"


# ------------------------------------------------------------------ the process


def test_main_writes_the_result_file_and_exit_codes(tmp_path: Path) -> None:
    base = _parts(tmp_path, [{"o.csv": b"a\n1\n"}, {"o.csv": b"b\n2\n"}])
    out = tmp_path / "out"
    out.mkdir()
    result = tmp_path / "result.json"
    code = main(
        [
            "merge-csv",
            str(base),
            str(out),
            str(result),
            "--max-output-bytes",
            "1000",
            "--json-max-bytes",
            "1000",
        ]
    )
    assert code == EXIT_COMBINE_ERROR
    doc = json.loads(result.read_text())
    assert doc["ok"] is False
    assert doc["code"] == "header_mismatch"

    out2 = tmp_path / "out2"
    out2.mkdir()
    base2 = tmp_path / "p2"
    base2.mkdir()
    (base2 / "00000").mkdir()
    (base2 / "00000" / "f").write_bytes(b"hello")
    code = main(
        [
            "concat",
            str(base2),
            str(out2),
            str(result),
            "--max-output-bytes",
            "1000",
            "--json-max-bytes",
            "1000",
        ]
    )
    assert code == 0
    assert json.loads(result.read_text()) == {
        "ok": True,
        "parts": 1,
        "files": 1,
        "bytes": 5,
    }
