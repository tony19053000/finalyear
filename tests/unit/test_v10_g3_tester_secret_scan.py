"""V1.0 G3 tester: try to get a secret past ``scripts/package_secret_scan.py``.

Derived from the brief: "package secret scan clean" must mean a real credential
cannot ride along in a release artefact. Tests marked ``xfail(strict=True)`` are
confirmed false negatives (defects TG3-n in the tester report); when the scanner
is fixed they flip to XPASS(strict) = red, prompting removal of the marker.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

_SPEC = importlib.util.spec_from_file_location(
    "tester_pss",
    Path(__file__).resolve().parents[2] / "scripts" / "package_secret_scan.py",
)
assert _SPEC and _SPEC.loader
pss = importlib.util.module_from_spec(_SPEC)
sys.modules["tester_pss"] = pss
_SPEC.loader.exec_module(pss)

TOKEN = b"mfat_" + b"A" * 43
SESSION = b"mfbs_" + b"B" * 43
BODY = b"QUJD" * 16
PEM = b"-----BEGIN PRIVATE KEY-----\n" + BODY + b"\n-----END PRIVATE KEY-----\n"


def rules(name: str, data: bytes) -> list[str]:
    return [h.rule for h in pss.check_member("a", name, data)]


@pytest.mark.parametrize(
    "kind",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "DSA PRIVATE KEY",
    ],
)
@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_every_pem_flavour_is_caught(kind: str, eol: bytes) -> None:
    pem = (
        b"-----BEGIN "
        + kind.encode()
        + b"-----"
        + eol
        + (b"A" * 64 + eol) * 5
        + b"AAAA=="
        + eol
        + b"-----END "
        + kind.encode()
        + b"-----"
        + eol
    )
    assert "pem_private_key" in rules("blob.bin", pem)


def test_pem_with_proc_type_headers() -> None:
    pem = (
        b"-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
        b"DEK-Info: AES-128-CBC,0123456789ABCDEF\n\n" + BODY + b"\n"
        b"-----END RSA PRIVATE KEY-----\n"
    )
    assert "pem_private_key" in rules("k.txt", pem)


def test_pem_buried_in_a_big_file_and_with_surrounding_text() -> None:
    data = b"x" * 200_000 + b"\nkey=\n" + PEM + b"y" * 200_000
    assert "pem_private_key" in rules("big.txt", data)


@pytest.mark.parametrize("fill", [b"A", b"-", b"_", b"Z", b"0"])
def test_tokens_with_any_urlsafe_alphabet(fill: bytes) -> None:
    assert "api_token" in rules("x", b"t=mfat_" + fill * 43)
    assert "session_token" in rules("x", b"Bearer mfbs_" + fill * 43 + b"\n")


@pytest.mark.parametrize("wrap", [b'"%s"', b"'%s'", b"%s\n", b"token=%s;", b"<%s>"])
def test_tokens_in_assorted_wrappers(wrap: bytes) -> None:
    assert "api_token" in rules("x.log", wrap % TOKEN)


def test_token_inside_a_url_or_header() -> None:
    assert "api_token" in rules("x", b"https://h/?t=" + TOKEN + b"&a=1")
    assert "api_token" in rules("x", b"Authorization: Bearer " + TOKEN)


def test_join_code_upper_and_lower() -> None:
    code = b"mf1-ABCDE-FGHIJ-KLMNO-PQRST-UVWXY-23456-ABCDE"
    assert "join_code" in rules("x", code)
    assert "join_code" in rules("x", code.upper())


def test_clean_third_party_text_has_no_hits() -> None:
    assert (
        rules(
            "lib/x.py",
            b"# BEGIN PRIVATE KEY marker only\nre.compile('-----BEGIN PRIVATE KEY-----')",
        )
        == []
    )
    assert rules("lib/x.py", b"mfat_ short") == []


def test_pem_with_escaped_newlines_in_source_or_json() -> None:
    escaped = PEM.replace(b"\n", b"\\n")
    assert "pem_private_key" in rules("cfg.json", b'{"k": "' + escaped + b'"}')
    assert "pem_private_key" in rules("cfg.py", b'KEY = "' + escaped + b'"')


def test_pem_with_unusual_line_lengths() -> None:
    long_lines = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + b"A" * 200
        + b"\n"
        + b"A" * 200
        + b"\n-----END PRIVATE KEY-----\n"
    )
    assert "pem_private_key" in rules("k", long_lines)
    no_nl = (
        b"-----BEGIN EC PRIVATE KEY-----\n"
        + b"A" * 70
        + b"-----END EC PRIVATE KEY-----"
    )
    assert "pem_private_key" in rules("k", no_nl)


def test_nested_compressed_archive_is_opened(tmp_path: Path) -> None:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("k.txt", TOKEN * 5)
    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "dep.whl").write_bytes(inner.getvalue())
    assert pss.scan(folder), "a token inside a deflated wheel in the bundle went unseen"


def test_tar_gz_inside_zip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("s.txt")
        info.size = len(TOKEN)
        t.addfile(info, io.BytesIO(TOKEN))
    outer = tmp_path / "o.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("inner.tar.gz", buf.getvalue())
    assert pss.scan(outer)


def test_token_after_an_escaped_newline() -> None:
    assert "api_token" in rules("x.json", b'{"log": "a\\n' + TOKEN + b'"}')


@pytest.mark.parametrize(
    "name", ["cert.p12", "store.pfx", "keystore.jks", "id_dsa", ".env.local"]
)
def test_more_secret_file_names(name: str, tmp_path: Path) -> None:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"opaque")
    assert pss.scan(tmp_path)


@pytest.mark.parametrize(
    "name", ["deep/dir/fabric.token", "x/.pgpass", "a/b/secrets.key", "setup-token"]
)
def test_secret_names_at_any_depth(name: str, tmp_path: Path) -> None:
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"opaque")
    assert any(h.rule == "secret_file" for h in pss.scan(tmp_path))


def test_windows_separators_in_member_names() -> None:
    assert "secret_file" in [
        h.rule for h in pss.check_member("a", "dir\\sub\\fabric.token", None)
    ]
    assert "test_dir" in [
        h.rule for h in pss.check_member("a", "meshforge\\tests\\x.py", None)
    ]


def test_unicode_paths_do_not_crash(tmp_path: Path) -> None:
    d = tmp_path / "ünï" / "名前"
    d.mkdir(parents=True)
    (d / "a.txt").write_bytes(b"x " + TOKEN)
    hits = pss.scan(tmp_path)
    assert [h.rule for h in hits] == ["api_token"]


def test_hits_never_leak_the_secret_in_render_or_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "f.txt").write_bytes(TOKEN + b"\n" + SESSION + b"\n" + PEM)
    code = pss.main([str(tmp_path), "--json"])
    out = capsys.readouterr()
    assert code == 1
    for secret in (TOKEN, SESSION, BODY):
        assert secret.decode() not in out.out + out.err


def test_empty_directory_and_empty_file_are_clean(tmp_path: Path) -> None:
    assert pss.scan(tmp_path) == []
    (tmp_path / "e").write_bytes(b"")
    assert pss.scan(tmp_path) == []


def test_symlink_to_a_secret_outside_is_not_followed_or_crashes(tmp_path: Path) -> None:
    outside = tmp_path / "out.txt"
    outside.write_bytes(TOKEN)
    folder = tmp_path / "b"
    folder.mkdir()
    (folder / "ln").symlink_to(outside)
    (folder / "dangling").symlink_to(tmp_path / "nowhere")
    assert pss.scan(folder) == []  # must not raise


def test_truncated_deb_and_truncated_tar_are_exit_2(tmp_path: Path) -> None:
    deb = tmp_path / "a.deb"
    deb.write_bytes(b"!<arch>\ndebian-binary   0")
    assert pss.main([str(deb)]) == 2
    tar = tmp_path / "a.tar.xz"
    tar.write_bytes(b"\xfd7zXZ\x00junk")
    assert pss.main([str(tar)]) == 2


def test_exit_code_is_2_even_when_another_artefact_has_hits(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "x").write_bytes(TOKEN)
    assert pss.main([str(tmp_path / "bad"), str(tmp_path / "missing")]) == 2


def test_deb_control_archive_secret_is_found(tmp_path: Path) -> None:
    def tar_bytes(name: str, data: bytes) -> bytes:
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w") as t:
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
        return b.getvalue()

    def ar(members: list[tuple[str, bytes]]) -> bytes:
        out = b"!<arch>\n"
        for n, d in members:
            out += (
                f"{n:<16}0           0     0     100644  {len(d):<10}`\n".encode() + d
            )
            if len(d) % 2:
                out += b"\n"
        return out

    deb = tmp_path / "p.deb"
    deb.write_bytes(
        ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar", tar_bytes("./postinst", b"echo " + TOKEN)),
                ("data.tar", tar_bytes("./usr/x", b"ok")),
            ]
        )
    )
    assert [h.rule for h in pss.scan(deb)] == ["api_token"]
