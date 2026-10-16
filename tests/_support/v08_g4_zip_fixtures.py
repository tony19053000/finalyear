"""Real zips (made by Python's zipfile) for the V0.8 G4 tester r1 zip-reader tests.

Run:  uv run python tests/_support/v08_g4_zip_fixtures.py <outdir>
"""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path


def _write(
    path: Path,
    items: list[tuple[str, bytes, int, int]],
    *,
    compress: int = zipfile.ZIP_DEFLATED,
) -> None:
    with zipfile.ZipFile(path, "w", compression=compress) as z:
        for name, data, mode, ctype in items:
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 16, 12, 0, 0))
            info.external_attr = mode << 16
            info.compress_type = ctype
            info.flag_bits |= 0x800
            z.writestr(info, data)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    D, S = zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED
    f, x, d = 0o100644, 0o100755, 0o040755
    _write(
        out / "normal.zip",
        [
            ("proj/", b"", d, S),
            ("proj/main.py", b"print('hi')\n" * 50, f, D),
            ("proj/run.sh", b"#!/bin/sh\n", x, S),
            ("proj/data/\u65e5\u672c\u8a9e \U0001f600.txt", "\u00e9".encode(), f, D),
            ("proj/empty/", b"", d, S),
            ("proj/zero.txt", b"", f, S),
        ],
    )
    _write(
        out / "finder.zip",
        [
            ("proj/", b"", d, S),
            ("proj/main.py", b"print(1)\n", f, D),
            ("__MACOSX/", b"", d, S),
            ("__MACOSX/proj/", b"", d, S),
            ("__MACOSX/proj/._main.py", b"\x00\x05", f, S),
        ],
    )
    for name in [
        "../evil.txt",
        "/abs.txt",
        "a/../../b.txt",
        "a\\b.txt",
        "C:/x.txt",
        "ok/../x.txt",
    ]:
        safe = (
            name.replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .replace("..", "dd")
        )
        _write(out / f"bad_{safe}.zip", [("main.py", b"1", f, S), (name, b"x", f, S)])
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _write(out / "dupe.zip", [("main.py", b"1", f, S), ("main.py", b"2", f, S)])
    _write(out / "empty.zip", [])
    _write(out / "onlydirs.zip", [("a/", b"", d, S), ("a/b/", b"", d, S)])
    _write(
        out / "symlink.zip",
        [("main.py", b"1", f, S), ("link", b"main.py", 0o120777, S)],
    )
    # A lying central directory: 64 MiB of zeros declared as 10 bytes.
    bomb = out / "bomb.zip"
    _write(bomb, [("main.py", b"\x00" * (64 * 1024 * 1024), f, D)])
    raw = bytearray(bomb.read_bytes())
    at = raw.rfind(b"PK\x01\x02")
    raw[at + 24 : at + 28] = struct.pack("<I", 10)
    bomb.write_bytes(bytes(raw))
    # 600 files in a deflated zip.
    _write(
        out / "many.zip",
        [(f"p/d{i % 30}/f{i}.txt", f"{i}".encode(), f, D) for i in range(600)]
        + [("p/main.py", b"1", f, D)],
    )
    # Not a zip at all but with a zip name; and a truncated zip.
    (out / "garbage.zip").write_bytes(b"PK\x03\x04" + bytes(range(256)) * 10)
    good = (out / "normal.zip").read_bytes()
    (out / "truncated.zip").write_bytes(good[: len(good) // 2])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
