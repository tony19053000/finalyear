"""V0.10 G5 tester r1: adversarial cases for the built-in combines, derived
from D283 pins (4), (5), (7) and brief 5e - not from the implementation.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from meshforge.agent.execution.combine import (
    EXIT_COMBINE_ERROR,
    CombineError,
    combine,
)

pytestmark = [pytest.mark.unit]
POSIX = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only")
Parts = list[Mapping[str, bytes]]


def _mk(root: Path, parts: Parts, width: int = 5) -> tuple[Path, Path]:
    base = root / "parts"
    base.mkdir()
    for i, files in enumerate(parts):
        part = base / f"{i:0{width}d}"
        part.mkdir()
        for rel, data in files.items():
            p = part / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    out = root / "out"
    out.mkdir()
    return base, out


def _tree(out: Path) -> dict[str, bytes]:
    return {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }


def _run(tmp: Path, kind: str, parts: Parts, **kw: int) -> dict[str, bytes]:
    base, out = _mk(tmp, parts)
    combine(
        kind,
        base,
        out,
        max_output_bytes=kw.get("cap", 1 << 30),
        json_max_bytes=kw.get("jcap", 1 << 20),
    )
    return _tree(out)


def _refuse(tmp: Path, kind: str, parts: Parts, **kw: int) -> CombineError:
    with pytest.raises(CombineError) as exc:
        _run(tmp, kind, parts, **kw)
    return exc.value


# ------------------------------------------------------------ chunk ordering


def test_order_is_numeric_across_the_digit_boundary_width5(tmp_path: Path) -> None:
    n = 120  # 2-3 digit indexes inside a 5 wide name: 9 < 10 < 100 numerically
    got = _run(tmp_path, "concat", [{"o": f"{i},".encode()} for i in range(n)])
    assert got["o"] == "".join(f"{i}," for i in range(n)).encode()


def test_order_above_99999_uses_width_6(tmp_path: Path) -> None:
    n = 100_003
    base = tmp_path / "parts"
    base.mkdir()
    for i in range(n):
        d = base / f"{i:06d}"
        d.mkdir()
        if i in (0, 9, 10, 99_999, 100_000, 100_002):
            (d / "o").write_bytes(f"<{i}>".encode())
    out = tmp_path / "out"
    out.mkdir()
    combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert (out / "o").read_bytes() == b"<0><9><10><99999><100000><100002>"


def test_mixed_widths_are_refused(tmp_path: Path) -> None:
    base = tmp_path / "parts"
    base.mkdir()
    (base / "99999").mkdir()
    (base / "100000").mkdir()  # width 5 and 6 together: not one numbering
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as exc:
        combine("concat", base, out, max_output_bytes=9, json_max_bytes=9)
    assert exc.value.code == "unsafe_part"


@pytest.mark.parametrize("names", [["00000", "00002"], ["00001"], ["00000", "x"]])
def test_gaps_and_foreign_names_are_refused(tmp_path: Path, names: list[str]) -> None:
    base = tmp_path / "parts"
    base.mkdir()
    for name in names:
        (base / name).mkdir()
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(CombineError) as exc:
        combine("concat", base, out, max_output_bytes=9, json_max_bytes=9)
    assert exc.value.code == "unsafe_part"


def test_output_independent_of_creation_order(tmp_path: Path) -> None:
    import random

    base = tmp_path / "parts"
    base.mkdir()
    order = list(range(40))
    random.Random(7).shuffle(order)  # noqa: S311
    for i in order:
        d = base / f"{i:05d}"
        d.mkdir()
        (d / "a.txt").write_bytes(f"{i};".encode())
    out = tmp_path / "out"
    out.mkdir()
    combine("concat", base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert (out / "a.txt").read_bytes() == "".join(f"{i};" for i in range(40)).encode()


# ------------------------------------------------------------------- concat


def test_concat_binary_all_byte_values_and_unicode_paths(tmp_path: Path) -> None:
    blob = bytes(range(256))
    got = _run(
        tmp_path,
        "concat",
        [{"déjà/データ.bin": blob}, {"déjà/データ.bin": blob}],
    )
    assert got == {"déjà/データ.bin": blob * 2}


def test_concat_large_streams_exactly(tmp_path: Path) -> None:
    big = os.urandom(3 * 1024 * 1024 + 17)
    got = _run(tmp_path, "concat", [{"b": big}, {"b": big[:5]}, {"b": b""}])
    assert got["b"] == big + big[:5]


# ---------------------------------------------------------------- merge-csv


def test_csv_crlf_first_lf_later_and_missing_terminators(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [
            {"t.csv": b"h1,h2\r\n1,2\r\n"},
            {"t.csv": b"h1,h2\n3,4"},  # LF header, no final terminator
            {
                "t.csv": b"h1,h2\r\n5,6"
            },  # last part too: the terminator is added (D283 pin 5)
        ],
    )
    # missing terminator of part 1 gets the FIRST header's style (CRLF)
    assert got["t.csv"] == b"h1,h2\r\n1,2\r\n3,4\r\n5,6\r\n"


def test_csv_lf_first_style_added_is_lf(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [{"t.csv": b"h\n1"}, {"t.csv": b"h\n2\n"}, {"t.csv": b"h\n3"}],
    )
    assert got["t.csv"] == b"h\n1\n2\n3\n"


def test_csv_bom_only_in_a_later_part_is_dropped_with_its_header(
    tmp_path: Path,
) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [{"t.csv": b"a,b\n1,2\n"}, {"t.csv": b"\xef\xbb\xbfa,b\n3,4\n"}],
    )
    assert got["t.csv"] == b"a,b\n1,2\n3,4\n"


def test_csv_bom_first_is_kept_once(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [{"t.csv": b"\xef\xbb\xbfa\n1\n"}, {"t.csv": b"\xef\xbb\xbfa\n2\n"}],
    )
    assert got["t.csv"] == b"\xef\xbb\xbfa\n1\n2\n"


@pytest.mark.parametrize(
    "second",
    [b"a,b \n", b"A,b\n", b"a,b,c\n", b"a;b\n", b"\xef\xbb\xbf\xef\xbb\xbfa,b\n"],
)
def test_csv_header_mismatch_is_byte_exact(tmp_path: Path, second: bytes) -> None:
    err = _refuse(
        tmp_path, "merge-csv", [{"t.csv": b"a,b\n1,2\n"}, {"t.csv": second + b"x\n"}]
    )
    assert err.code == "header_mismatch"


def test_csv_mismatch_in_a_late_part_still_refuses_whole_job(tmp_path: Path) -> None:
    parts: Parts = [{"t.csv": b"h\n1\n"} for _ in range(30)]
    parts.append({"t.csv": b"g\n1\n"})
    assert _refuse(tmp_path, "merge-csv", parts).code == "header_mismatch"


def test_csv_empty_first_part_second_provides_header(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [{"t.csv": b""}, {"t.csv": b"h\r\n1\r\n"}, {"t.csv": b""}],
    )
    assert got["t.csv"] == b"h\r\n1\r\n"


def test_csv_header_only_parts_and_header_only_everywhere(tmp_path: Path) -> None:
    got = _run(tmp_path, "merge-csv", [{"t.csv": b"h"}, {"t.csv": b"h\n"}])
    assert got["t.csv"] in (b"h\n", b"h")  # one header, never two
    assert got["t.csv"].count(b"h") == 1


def test_csv_uppercase_suffix_is_merged_and_other_files_concat(
    tmp_path: Path,
) -> None:
    got = _run(
        tmp_path,
        "merge-csv",
        [
            {"UP.CSV": b"h\n1\n", "n.txt": b"x"},
            {"UP.CSV": b"h\n2\n", "n.txt": b"y"},
        ],
    )
    assert got == {"UP.CSV": b"h\n1\n2\n", "n.txt": b"xy"}


def test_csv_quoted_embedded_newlines_survive(tmp_path: Path) -> None:
    row = b'1,"line1\r\nline2"\r\n'
    got = _run(
        tmp_path,
        "merge-csv",
        [{"t.csv": b"a,b\r\n" + row}, {"t.csv": b"a,b\r\n" + row}],
    )
    assert got["t.csv"] == b"a,b\r\n" + row + row


def test_csv_header_line_at_and_over_1mib(tmp_path: Path) -> None:
    ok = b"h" * (1024 * 1024 - 1) + b"\n"
    got = (
        _run(tmp_path / "ok", "merge-csv", [{"t.csv": ok + b"1\n"}, {"t.csv": ok}])
        if (tmp_path / "ok").mkdir() is None
        else {}
    )
    assert got["t.csv"] == ok + b"1\n"
    (tmp_path / "bad").mkdir()
    err = _refuse(
        tmp_path / "bad", "merge-csv", [{"t.csv": b"h" * (1024 * 1024 + 5) + b"\n"}]
    )
    assert err.code == "too_large"


def test_csv_many_parts_header_once(tmp_path: Path) -> None:
    n = 600
    got = _run(
        tmp_path, "merge-csv", [{"t.csv": f"id\n{i}\n".encode()} for i in range(n)]
    )
    assert got["t.csv"] == b"id\n" + b"".join(f"{i}\n".encode() for i in range(n))


# --------------------------------------------------------------- merge-json


def test_json_arrays_in_order_and_normalised(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-json",
        [
            {"r.json": b" [1, 2 ] "},
            {"r.json": b"[]"},
            {"r.json": b'\xef\xbb\xbf[ {"a": "\xc3\xa9"} ]'},
        ],
    )
    assert got["r.json"] == '[1,2,{"a":"é"}]\n'.encode()


def test_json_all_empty_arrays(tmp_path: Path) -> None:
    got = _run(tmp_path, "merge-json", [{"r.json": b"[]"}, {"r.json": b"[]"}])
    assert json.loads(got["r.json"]) == []


def test_json_objects_merge_and_nested_same_keys_are_fine(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-json",
        [{"r.json": b'{"a":{"k":1}}'}, {"r.json": b'{"b":{"k":2}}'}],
    )
    assert got["r.json"] == b'{"a":{"k":1},"b":{"k":2}}\n'


@pytest.mark.parametrize(
    "parts",
    [
        [b'{"a":1}', b'{"a":2}'],
        [b'{"a":1}', b'{"\\u0061":2}'],  # escaped spelling of the same key
        [b'{"a":1,"a":1}'],  # same value twice inside one document
        [b'{"x":{"a":1,"a":2}}'],  # nested duplicate
        [b'[{"a":1,"a":2}]'],  # duplicate inside an array element
    ],
)
def test_json_duplicate_key_any_depth(tmp_path: Path, parts: list[bytes]) -> None:
    assert (
        _refuse(tmp_path, "merge-json", [{"r.json": p} for p in parts]).code
        == "duplicate_key"
    )


@pytest.mark.parametrize(
    "parts",
    [
        [b"[1]", b'{"a":1}'],
        [b'{"a":1}', b"[1]"],
        [b"[1]", b"2"],
        [b'"s"'],
        [b"null"],
        [b"true"],
    ],
)
def test_json_mixed_or_scalar_is_type_mismatch(
    tmp_path: Path, parts: list[bytes]
) -> None:
    assert (
        _refuse(tmp_path, "merge-json", [{"r.json": p} for p in parts]).code
        == "type_mismatch"
    )


@pytest.mark.parametrize(
    "doc",
    [
        b"",
        b"   ",
        b"[1,",
        b"[NaN]",
        b"[Infinity]",
        b"[-Infinity]",
        b"[1e400]",
        b"[1] [2]",
        b"[1]x",
        b"\xff\xfe[1]",
        b'["\\ud800"]',  # lone surrogate cannot be written back as UTF-8
        b"[" * 200_000,  # recursion bomb: refusal, not a crash
        b"[" + b"9" * 5000 + b"]",  # int digit limit
    ],
)
def test_json_invalid_documents_are_invalid_json(tmp_path: Path, doc: bytes) -> None:
    err = _refuse(tmp_path, "merge-json", [{"r.json": b"[0]"}, {"r.json": doc}])
    assert err.code == "invalid_json"


def test_json_parse_cap_exact_boundary(tmp_path: Path) -> None:
    doc = b"[" + b"1," * 48 + b"1]"
    doc = doc + b" " * (100 - len(doc))
    assert len(doc) == 100
    (tmp_path / "ok").mkdir()
    assert _run(tmp_path / "ok", "merge-json", [{"r.json": doc}], jcap=100)
    (tmp_path / "bad").mkdir()
    err = _refuse(tmp_path / "bad", "merge-json", [{"r.json": doc + b" "}], jcap=100)
    assert err.code == "too_large"


def test_json_big_int_and_float_stay_deterministic(tmp_path: Path) -> None:
    doc = b"[12345678901234567890123, 1.5, 1E2, -0, 0.1]"
    a = _run(
        tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path,
        "merge-json",
        [{"r.json": doc}],
    )
    (tmp_path / "b").mkdir()
    b = _run(tmp_path / "b", "merge-json", [{"r.json": doc}])
    assert a == b
    assert b"12345678901234567890123" in a["r.json"]


def test_json_non_json_files_are_concatenated(tmp_path: Path) -> None:
    got = _run(
        tmp_path,
        "merge-json",
        [{"r.json": b"[1]", "n.txt": b"a"}, {"r.json": b"[2]", "n.txt": b"b"}],
    )
    assert got["n.txt"] == b"ab"


# ----------------------------------------------------------------- too_large


def test_output_cap_exact_boundary_across_files(tmp_path: Path) -> None:
    parts: Parts = [{"a": b"12345", "b": b"123"}, {"a": b"12345"}]  # 13 bytes total
    (tmp_path / "ok").mkdir()
    assert _run(tmp_path / "ok", "concat", parts, cap=13)
    (tmp_path / "bad").mkdir()
    assert _refuse(tmp_path / "bad", "concat", parts, cap=12).code == "too_large"


def test_csv_added_terminator_counts_against_the_cap(tmp_path: Path) -> None:
    parts: Parts = [{"t.csv": b"h\n1"}, {"t.csv": b"h\n2"}]  # -> "h\n1\n2\n" = 6 bytes
    (tmp_path / "ok").mkdir()
    assert _run(tmp_path / "ok", "merge-csv", parts, cap=6)["t.csv"] == b"h\n1\n2\n"
    (tmp_path / "bad").mkdir()
    assert _refuse(tmp_path / "bad", "merge-csv", parts, cap=5).code == "too_large"


# -------------------------------------------------------------- unsafe parts


def _two_parts(tmp: Path) -> tuple[Path, Path]:
    base, out = _mk(tmp, [{"ok.txt": b"fine"}, {"ok.txt": b"fine"}])
    return base, out


def _expect_unsafe(base: Path, out: Path, kind: str = "concat") -> None:
    with pytest.raises(CombineError) as exc:
        combine(kind, base, out, max_output_bytes=1 << 20, json_max_bytes=1 << 20)
    assert exc.value.code == "unsafe_part"
    # checked before a byte is written
    assert not any(out.iterdir()), list(out.iterdir())


@POSIX
@pytest.mark.parametrize("kind", ["concat", "merge-csv", "merge-json"])
def test_symlink_file_to_outside_secret(tmp_path: Path, kind: str) -> None:
    secret = tmp_path / "secret"
    secret.write_text("TOPSECRET")
    base, out = _two_parts(tmp_path)
    (base / "00001" / "leak.txt").symlink_to(secret)
    _expect_unsafe(base, out, kind)


@POSIX
def test_symlink_dir_inside_part_and_dangling_link(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    (base / "00001" / "d").symlink_to(tmp_path)
    _expect_unsafe(base, out)
    (base / "00001" / "d").unlink()
    (base / "00001" / "dangling").symlink_to("/nonexistent/x")
    _expect_unsafe(base, out)


@POSIX
def test_symlink_part_root(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    (real / "x").write_bytes(b"x")
    (base / "00002").symlink_to(real, target_is_directory=True)
    _expect_unsafe(base, out)


@POSIX
def test_fifo_does_not_hang_and_is_refused(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    os.mkfifo(base / "00000" / "pipe")
    _expect_unsafe(base, out)


@POSIX
def test_unix_socket_is_refused(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(base / "00000" / "s"))
    except OSError:
        pytest.skip("path too long for a unix socket")
    try:
        _expect_unsafe(base, out)
    finally:
        s.close()


@POSIX
def test_hardlink_to_a_file_outside_the_parts_is_refused(tmp_path: Path) -> None:
    """D283 pin 4 / the brief: a hard link in a part is unsafe (a regular
    file whose inode is shared with something outside the parts)."""
    secret = tmp_path / "secret"
    secret.write_text("TOPSECRET")
    base, out = _two_parts(tmp_path)
    os.link(secret, base / "00001" / "leak.txt")
    _expect_unsafe(base, out)


@POSIX
def test_unsafe_entry_in_the_last_part_leaves_no_partial_output(tmp_path: Path) -> None:
    base, out = _mk(tmp_path, [{"a": b"1"}] * 5)
    os.mkfifo(base / "00004" / "p")
    _expect_unsafe(base, out)


@POSIX
def test_main_process_exit_code_and_result_file(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    os.mkfifo(base / "00001" / "p")
    result = tmp_path / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.agent.execution.combine",
            "concat",
            str(base),
            str(out),
            str(result),
            "--max-output-bytes",
            "1000",
            "--json-max-bytes",
            "1000",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == EXIT_COMBINE_ERROR, proc.stderr
    doc = json.loads(result.read_text())
    assert doc["ok"] is False and doc["code"] == "unsafe_part"


def test_main_process_success_result_file(tmp_path: Path) -> None:
    base, out = _two_parts(tmp_path)
    result = tmp_path / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.agent.execution.combine",
            "concat",
            str(base),
            str(out),
            str(result),
            "--max-output-bytes",
            "1000",
            "--json-max-bytes",
            "1000",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(result.read_text())["ok"] is True
    assert (out / "ok.txt").read_bytes() == b"finefine"


def test_main_missing_parts_folder_is_a_crash_not_a_refusal(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.agent.execution.combine",
            "concat",
            str(tmp_path / "nope"),
            str(out),
            str(tmp_path / "r.json"),
            "--max-output-bytes",
            "1",
            "--json-max-bytes",
            "1",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode not in (0, EXIT_COMBINE_ERROR)


@POSIX
def test_unpack_refuses_a_hardlink_entry_in_a_part_bundle(tmp_path: Path) -> None:
    """The real barrier for hard links: the part bundle's unpack."""
    import io
    import tarfile

    from meshforge.common.bundle import BundleError, BundleLimits, unpack

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        f = tarfile.TarInfo("a")
        f.size = 3
        tar.addfile(f, io.BytesIO(b"abc"))
        h = tarfile.TarInfo("b")
        h.type = tarfile.LNKTYPE
        h.linkname = "a"
        tar.addfile(h)
    bundle = tmp_path / "b.tar"
    bundle.write_bytes(buf.getvalue())
    with pytest.raises(BundleError):
        unpack(bundle, tmp_path / "dest", limits=BundleLimits(max_total_bytes=1 << 20))
