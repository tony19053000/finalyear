"""V0.17 G4 CI fix: the bundle leak check flags *this* build machine's dirs, not
every string under a home directory.

The first CI run (40280010136) failed ``package-linux`` and ``package-windows``
because hash-checked third-party wheels embed their own build machines' paths
(``C:\\Users\\runneradmin\\.cargo\\registry`` in Rust extensions and uv.exe,
``C:/Users/runneradmin/AppData/Local/Temp/cibw-run-*`` in numpy's
``__config__.py``, ``/home/runner/work/uv/uv`` in cyclonedx SBOMs), and a GitHub
runner has that same home. These tests pin both halves: those strings pass,
and a real leak (a shebang or ``.pth`` naming the staging dir, the checkout,
the cache or the build interpreter) is still caught.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

pytestmark = pytest.mark.unit


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


def _needles(home: str, sep: str = "/") -> list[bytes]:
    """The needles of a CI-shaped build on a runner whose home is ``home``
    (a Linux runner for ``sep="/"``, a Windows one for ``"\\"``, whichever OS
    the test runs on)."""
    j = sep.join
    P = PurePosixPath if sep == "/" else PureWindowsPath
    return lib.leak_needles(  # type: ignore[no-any-return]
        bb.build_machine_paths(
            work=P(j([home, "work", "finalyear", "finalyear", "dist", ".bundle-x"])),
            out=P(j([home, "work", "finalyear", "finalyear", "dist"])),
            repo=P(j([home, "work", "finalyear", "finalyear"])),
            cache=P(j([home, ".cache", "meshforge-bundle"])),
            environ={"SSL_CERT_FILE": "/srv/corp-trust/ca.pem", "PATH": "/usr/bin"},
            interpreter=[
                j([home, "work", "finalyear", "finalyear", ".venv"]),
                j([home, ".local", "share", "uv", "python", "cpython-3.12.14"]),
                "/usr",
                "/usr/local",
            ],
            uv=[
                j([home, ".cache", "uv"]),
                j([home, ".local", "share", "uv", "python"]),
            ],
        )
    )


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


# ----------------------------------------------------- the CI false positives
def test_linux_wheel_sboms_naming_the_runner_home_are_not_leaks(
    tmp_path: Path,
) -> None:
    site = "python/lib/python3.12/site-packages"
    root = _tree(
        tmp_path / "b",
        {
            f"{site}/uv-0.12.21.dist-info/sboms/uv.cyclonedx.json": (
                b'{"bom-ref": "path+file:///home/runner/work/uv/uv/crates/uv#0.12.21"}'
            ),
            f"{site}/pydantic_core-2.46.5.dist-info/sboms/pydantic-core.cyclonedx"
            ".json": b'"/home/runner/work/pydantic/pydantic/pydantic-core#2.46.5"',
            f"{site}/numpy/__config__.py": b'"libdir": "/usr/local/lib",\n',
        },
    )
    assert lib.find_leaks(root, _needles("/home/runner")) == []


def test_windows_wheel_build_paths_under_runneradmin_are_not_leaks(
    tmp_path: Path,
) -> None:
    home = r"C:\Users\runneradmin"
    cargo = rb"C:\Users\runneradmin\.cargo\registry\src\index.crates.io-1949cf\pyo3"
    root = _tree(
        tmp_path / "b",
        {
            "python/Lib/site-packages/cryptography/hazmat/bindings/_rust.pyd": (
                b"MZ\x00\x00" + cargo + b"\x00"
            ),
            "python/Lib/site-packages/pydantic_core/_pydantic_core.pyd": (
                b"MZ\x00" + cargo + b"\x00"
            ),
            "python/Scripts/uv.exe": b"MZ\x00" + cargo + b"\\h2-0.4.15\\src\x00",
            "python/Lib/site-packages/numpy/__config__.py": (
                b'"C:/Users/runneradmin/AppData/Local/Temp/cibw-run-kywcz0lc/'
                b'cp312-win_amd64/build/venv/Lib/site-packages"\n'
            ),
            "python/Lib/site-packages/numpy/__pycache__/__config__.cpython-312.pyc": (
                b"\x00\x00C:/Users/runneradmin/AppData/Local/Temp/cibw-run-x/b\x00"
            ),
        },
    )
    assert lib.find_leaks(root, _needles(home, "\\")) == []


def test_the_home_directory_itself_is_not_a_root() -> None:
    paths = bb.build_machine_paths(
        work=Path("/srv/w"),
        out=Path("/srv/o"),
        repo=Path("/srv/r"),
        cache=Path("/srv/c"),
        environ={},
        interpreter=[],
        uv=[],
    )
    assert str(Path.home()) not in paths


# ------------------------------------------------------- genuine leaks caught
@pytest.mark.parametrize(
    ("rel", "data"),
    [
        # a console script whose shebang still names the staging dir
        (
            "python/bin/meshforge-agent",
            b"#!/home/runner/work/finalyear/finalyear/dist/.bundle-x/"
            b"meshforge-agent/python/bin/python3\n",
        ),
        # a .pth adding the source checkout to sys.path (an editable install)
        (
            "python/lib/python3.12/site-packages/__editable__.meshforge.pth",
            b"/home/runner/work/finalyear/finalyear/src\n",
        ),
        # a sysconfig/pyvenv pointing at the build interpreter
        (
            "python/pyvenv.cfg",
            b"home = /home/runner/.local/share/uv/python/cpython-3.12.14/bin\n",
        ),
        # a RECORD/launcher naming the bundle cache or the uv cache
        ("bin/x", b"exec /home/runner/.cache/meshforge-bundle/python/bin/python3\n"),
        ("python/x.cfg", b"cache = /home/runner/.cache/uv/builds-v0/abc\n"),
        ("python/ssl.cfg", b"cafile = /srv/corp-trust/ca.pem\n"),
    ],
)
def test_build_machine_dirs_are_still_leaks(
    tmp_path: Path, rel: str, data: bytes
) -> None:
    root = _tree(tmp_path / "b", {rel: data, "python/clean.py": b"X = 1\n"})
    leaks = lib.find_leaks(root, _needles("/home/runner"))
    assert [leak.path for leak in leaks] == [rel]


def test_windows_staging_dir_in_a_launcher_is_a_leak(tmp_path: Path) -> None:
    root = _tree(
        tmp_path / "b",
        {
            "meshforge-agent.cmd": (
                b'@"c:/Users/runneradmin/work/finalyear/finalyear/dist/.bundle-x/'
                b'python/python.exe" -m meshforge %*\r\n'
            )
        },
    )
    leaks = lib.find_leaks(root, _needles(r"C:\Users\runneradmin", "\\"))
    assert [leak.path for leak in leaks] == ["meshforge-agent.cmd"]


def test_uv_dirs_are_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {"cache": "/srv/u/cache\n", "python": ""}

    def fake_run(argv: list[str], **_kw: object) -> str:
        if argv[1] == "python":
            raise bb.BundleError("old uv", reason="tool_failed")
        return answers[argv[1]]

    monkeypatch.setattr(bb, "run", fake_run)
    assert bb.uv_dirs("uv") == ["/srv/u/cache"]
