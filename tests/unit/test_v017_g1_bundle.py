"""V0.17 G1: the production bundle builder's new parts (brief §5a item 1).

* ``MANIFEST.sha256`` + ``BUILDINFO.json``: canonical, byte-identical when the
  same tree is built twice in different temp dirs, ``sha256sum``-compatible;
* the reproducible ``.tar.xz`` and the compressed-size budget (Q17);
* ``packaging/pins.toml``: URL + sha256, never "latest"; downloads are checked;
* target OS/arch checks, the controller's dashboard build, F3 leak needles.

No network and no real build here; the real double build is
``tests/component/lab/test_v017_sp3_bundle.py`` (slow).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

from meshforge.common.buildinfo import BUILDINFO_NAME, read_build_info

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
EPOCH = 1_700_000_000


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load("bundle_lib")
bb = _load("build_bundle")

pytestmark = pytest.mark.unit
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes/symlinks")


def _synthetic_bundle(root: Path) -> None:
    """A small bundle-shaped tree, as a build leaves it before the manifest."""
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "meshforge-agent").write_text("#!/bin/sh\nexec x\n")
    (root / "bin" / "meshforge-agent").chmod(0o700)
    (root / "python" / "lib").mkdir(parents=True)
    (root / "python" / "lib" / "a b.py").write_text("X = 1\n")
    (root / "python" / "empty").mkdir()
    if os.name == "posix":
        (root / "uv").symlink_to("python/lib/a b.py")


def _manifest(root: Path) -> tuple[bytes, bytes]:
    bb.normalise_tree(root, EPOCH)
    meta = lib.Manifest(meta={"version": "1.0", "git_sha": "abc", "role": "agent"})
    bb._write_normalised(root / lib.BUILDINFO_NAME, meta.buildinfo_json(), EPOCH)
    text = lib.manifest_text(lib.scan_tree(root))
    bb._write_normalised(root / lib.MANIFEST_NAME, text, EPOCH)
    return (
        (root / lib.MANIFEST_NAME).read_bytes(),
        (root / lib.BUILDINFO_NAME).read_bytes(),
    )


def test_buildinfo_name_matches_the_runtime_reader() -> None:
    assert lib.BUILDINFO_NAME == BUILDINFO_NAME


def test_manifest_is_byte_identical_across_build_dirs(tmp_path: Path) -> None:
    one = tmp_path / "first build" / "meshforge-1.0-linux-x86_64"
    two = tmp_path / "other" / "deeper" / "meshforge-1.0-linux-x86_64"
    _synthetic_bundle(one)
    _synthetic_bundle(two)
    assert _manifest(one) == _manifest(two)
    info = read_build_info(one)
    assert info is not None and info.version == "1.0" and info.git_sha == "abc"


@posix_only
def test_manifest_records_what_sha256sum_cannot(tmp_path: Path) -> None:
    root = tmp_path / "b"
    _synthetic_bundle(root)
    text, _ = _manifest(root)
    lines = text.decode().splitlines()
    assert lines[0].startswith("# MeshForge bundle manifest")
    assert "#exec bin/meshforge-agent" in lines
    assert "#symlink uv -> python/lib/a b.py" in lines
    assert "#dir python/empty" in lines
    digest = hashlib.sha256(b"X = 1\n").hexdigest()
    assert f"{digest}  python/lib/a b.py" in lines
    assert any(line.endswith(f"  {lib.BUILDINFO_NAME}") for line in lines)
    assert not any(line.endswith(f"  {lib.MANIFEST_NAME}") for line in lines)
    # parse -> render is the identity: the text is canonical
    assert lib.manifest_text(lib.parse_manifest_text(text.decode())) == text.decode()
    assert lib.verify_bundle(root) == []


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="needs coreutils")
def test_sha256sum_checks_the_manifest(tmp_path: Path) -> None:
    root = tmp_path / "b"
    _synthetic_bundle(root)
    _manifest(root)
    ok = subprocess.run(
        ["sha256sum", "-c", "--strict", "--quiet", lib.MANIFEST_NAME],
        cwd=root,
        capture_output=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    (root / "python" / "lib" / "a b.py").write_text("X = 2\n")
    bad = subprocess.run(
        ["sha256sum", "-c", "--quiet", lib.MANIFEST_NAME],
        cwd=root,
        capture_output=True,
        check=False,
    )
    assert bad.returncode != 0


@pytest.mark.parametrize("name", ["a\nb", "a\\b"])
def test_manifest_refuses_names_sha256sum_would_escape(name: str) -> None:
    entry = {"path": name, "type": "file", "sha256": "0" * 64, "mode": "0644"}
    with pytest.raises(lib.BundleError) as info:
        lib.manifest_text([entry])
    assert info.value.reason == "bad_file"


def test_a_garbled_manifest_is_a_typed_error() -> None:
    with pytest.raises(lib.BundleError) as info:
        lib.parse_manifest_text("not a manifest line\n")
    assert info.value.reason == "bad_manifest"


# ---------------------------------------------------------------- archive


def test_the_archive_is_reproducible_and_owner_free(tmp_path: Path) -> None:
    one = tmp_path / "x" / "meshforge-1.0-linux-x86_64"
    two = tmp_path / "y z" / "meshforge-1.0-linux-x86_64"
    for root in (one, two):
        _synthetic_bundle(root)
        _manifest(root)
    a = tmp_path / "a.tar.xz"
    b = tmp_path / "b.tar.xz"
    size = lib.deterministic_tar_xz(one, a, EPOCH, preset=1)
    lib.deterministic_tar_xz(two, b, EPOCH, preset=1)
    assert size == a.stat().st_size
    assert a.read_bytes() == b.read_bytes()
    with tarfile.open(a, "r:xz") as tar:
        members = tar.getmembers()
    assert members[0].name == "meshforge-1.0-linux-x86_64"
    assert all(m.name.startswith("meshforge-1.0-linux-x86_64") for m in members)
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members} == {
        (0, 0, "", "", EPOCH)
    }


def test_size_budget_is_on_the_compressed_size() -> None:
    lib.check_size_budget(99_999_999, 100, "agent.tar.xz")
    with pytest.raises(lib.BundleError) as info:
        lib.check_size_budget(100_000_001, 100, "agent.tar.xz")
    assert info.value.reason == "size_budget"
    assert "100 MB" in str(info.value)


# ------------------------------------------------------------------- pins


def test_the_real_pins_are_complete_and_agree_with_the_lock() -> None:
    pins = lib.load_pins(REPO / "packaging" / "pins.toml")
    for section, target in (
        ("python", "linux-x86_64"),
        ("python", "windows-x86_64"),
        ("nfpm", "linux-x86_64"),
    ):
        download = lib.pinned(pins, section, target)
        assert download.url.startswith("https://")
    python = lib.pinned(pins, "python", "linux-x86_64")
    assert python.version == "3.12.14"
    assert python.file_name.startswith("cpython-3.12.14+")
    lock = (REPO / "uv.lock").read_text(encoding="utf-8")
    assert pins["uv"]["version"] == bb.locked_version(lock, "uv")
    assert pins["size_budget_mb"] == {"agent": 100, "controller": 150}


@pytest.mark.parametrize(
    ("table", "words"),
    [
        ({}, "has no [python.linux-x86_64]"),
        (
            {"python": {"linux-x86_64": {"url": "http://x/y", "sha256": "0" * 64}}},
            "https",
        ),
        (
            {
                "python": {
                    "version": "1",
                    "linux-x86_64": {
                        "url": "https://x/releases/latest/y",
                        "sha256": "0" * 64,
                    },
                }
            },
            "latest",
        ),
        (
            {
                "python": {
                    "version": "1",
                    "linux-x86_64": {"url": "https://x/y", "sha256": "abc"},
                }
            },
            "64-hex",
        ),
    ],
)
def test_bad_pins_are_refused(table: dict[str, object], words: str) -> None:
    with pytest.raises(lib.BundleError) as info:
        lib.pinned(table, "python", "linux-x86_64")
    assert info.value.reason == "bad_pins"
    assert words in str(info.value)


def _download(data: bytes) -> object:
    return lib.Download(
        "python",
        "1",
        "https://example.invalid/a%2Bb.tar.gz",
        hashlib.sha256(data).hexdigest(),
    )


def test_fetch_checks_the_sha256_and_reuses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(b"payload")

    monkeypatch.setattr(bb.urllib.request, "urlopen", urlopen)
    download = _download(b"payload")
    path = bb.fetch(download, tmp_path)
    assert path.name == "a+b.tar.gz" and path.read_bytes() == b"payload"
    assert bb.fetch(download, tmp_path) == path
    assert len(calls) == 1  # second time from the cache


def test_fetch_refuses_and_deletes_a_wrong_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bb.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"evil")
    )
    with pytest.raises(lib.BundleError) as info:
        bb.fetch(_download(b"payload"), tmp_path)
    assert info.value.reason == "sha256_mismatch"
    assert list((tmp_path / "downloads").iterdir()) == []


def test_fetch_failure_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def urlopen(url: str, timeout: float) -> io.BytesIO:
        raise OSError("no route to host")

    monkeypatch.setattr(bb.urllib.request, "urlopen", urlopen)
    with pytest.raises(lib.BundleError) as info:
        bb.fetch(_download(b"x"), tmp_path)
    assert info.value.reason == "download_failed"
    assert list((tmp_path / "downloads").iterdir()) == []


# ------------------------------------------------------------ build guards


@pytest.mark.skipif(sys.platform != "linux", reason="Linux host")
@pytest.mark.parametrize(("os_name", "arch"), [("windows", None), (None, "aarch64")])
def test_no_cross_builds(tmp_path: Path, os_name: str | None, arch: str | None) -> None:
    options = bb.BuildOptions(
        role="agent", out=tmp_path, cache=tmp_path, os_name=os_name, arch=arch
    )
    with pytest.raises(lib.BundleError) as info:
        bb.target_layout(options)
    assert info.value.reason == "unsupported"


def test_the_controller_needs_npm_for_its_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bb.shutil, "which", lambda _n: None)
    with pytest.raises(lib.BundleError) as info:
        bb.ensure_dashboard(tmp_path, build=True)
    assert info.value.reason == "no_dashboard"
    assert "--skip-dashboard-build" in str(info.value)
    bb.ensure_dashboard(tmp_path, build=False)  # skipping is explicit


def test_the_controller_dashboard_build_runs_npm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[object], **_kw: object) -> str:
        seen.append([str(a) for a in argv])
        return ""

    monkeypatch.setattr(bb.shutil, "which", lambda _n: "/usr/bin/npm")
    monkeypatch.setattr(bb, "run", fake_run)
    bb.ensure_dashboard(tmp_path, build=True)
    assert [a[-2:] for a in seen] == [
        [str(tmp_path / "frontend"), "ci"],
        ["run", "build"],
    ]


def test_trust_store_paths_become_leak_needles(tmp_path: Path) -> None:
    """SP3 F3: a build host's SSL_CERT_FILE may be used by the build, never shipped."""
    ca = "/srv/corp-trust/ca-bundle.pem"
    paths = bb.trust_store_paths({"SSL_CERT_FILE": ca, "PATH": "/usr/bin"})
    assert paths == [ca]
    (tmp_path / "f.cfg").write_text(f"cafile = {ca}\n")
    leaks = lib.find_leaks(tmp_path, lib.leak_needles(paths))
    assert [leak.path for leak in leaks] == ["f.cfg"]


def test_parse_args_knows_the_g1_options(tmp_path: Path) -> None:
    args = bb.parse_args(
        [
            "--role",
            "agent",
            "--os",
            "linux",
            "--arch",
            "x86_64",
            "--out",
            str(tmp_path),
            "--no-archive",
        ]
    )
    assert (args.os_name, args.arch, args.no_archive) == ("linux", "x86_64", True)
    with pytest.raises(SystemExit):
        bb.parse_args(["--role", "agent", "--out", str(tmp_path), "--os", "macos"])
