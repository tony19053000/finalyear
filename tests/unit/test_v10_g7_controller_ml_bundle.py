"""V1.0 G7 (D423): the controller bundle carries the ``ml`` extra and still fits.

* ``build_bundle.ROLE_EXTRAS["controller"]`` installs ``controller`` + ``ml``
  (the installed controller trains; without it every train exits 2);
* the new strip rules remove only test suites and the unused Arrow Flight / S3
  native parts, never an importable module MeshForge needs;
* ``build_deb.recompress_data_member`` rewrites nfpm's ``data.tar.xz`` with
  liblzma (177.2 -> 132.9 MB for the real controller .deb, Q17 budget 150 MB):
  same members, same headers apart from the data size, same tar bytes, and
  deterministic (a reproducible .deb stays reproducible).

The real controller .deb is installed and trains a model set in
``tests/component/packaging/test_v10_g7_controller_ml.py``.
"""

from __future__ import annotations

import importlib.util
import io
import lzma
import os
import random
import sys
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


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
bd = _load("build_deb")

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ extras


def test_the_controller_bundle_installs_the_ml_extra() -> None:
    assert bb.ROLE_EXTRAS["controller"] == ("controller", "ml")
    assert bb.ROLE_EXTRAS["agent"] == ("agent",)  # agents never train


def test_the_ml_extra_holds_what_the_trainer_requires() -> None:
    from meshforge.controller.ml.train import REQUIRED_LIBRARIES

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    shipped = " ".join(extras["controller"] + extras["ml"]).lower()
    for module in REQUIRED_LIBRARIES:
        dist = {"sklearn": "scikit-learn", "xgboost": "xgboost"}.get(module, module)
        assert dist.replace("_", "-") in shipped, module


def test_the_release_and_ci_workflows_build_the_controller_with_build_bundle() -> None:
    # the extras come from ROLE_EXTRAS, not from the workflow's `uv sync`: a
    # workflow that passed its own --extra list would bypass D423
    for workflow in ("release.yml", "ci.yml"):
        text = (REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        assert "build_bundle.py --role controller" in text, workflow
        # ...and both check the built bundle before packaging it
        assert "BUILDINFO extras lack ml" in text, workflow
        assert "import sys, sklearn, scipy, lightgbm, xgboost" in text, workflow


# ------------------------------------------------------------------- strip


def _site(root: Path) -> Path:
    site = lib.LINUX_X86_64.site_packages_path(root)
    for rel in (
        "scipy/__init__.py",
        "scipy/stats/__init__.py",
        "scipy/stats/tests/test_x.py",
        "scipy/tests/test_top.py",
        "sklearn/__init__.py",
        "sklearn/utils/_testing.py",
        "sklearn/linear_model/tests/test_ridge.py",
        "xgboost/__init__.py",
        "xgboost/testing/__init__.py",
        "pyarrow/__init__.py",
        "pyarrow/parquet/__init__.py",
        "pyarrow/fs.py",
        "pyarrow/_fs.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/libarrow.so.2600",
        "pyarrow/libarrow_substrait.so.2600",
        "pyarrow/libarrow_flight.so.2600",
        "pyarrow/libarrow_python_flight.so",
        "pyarrow/libarrow_python_flight.so.2600.0.0",
        "pyarrow/_flight.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/_flight.pyx",
        "pyarrow/flight.py",
        "pyarrow/libarrow_s3.so.2600",
        "pyarrow/_s3fs.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/_s3fs.pyx",
    ):
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return site


@pytest.mark.skipif(os.name != "posix", reason="POSIX layout only")
def test_strip_drops_ml_test_suites_and_flight_s3_but_keeps_modules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    site = _site(root)
    removed = bb.strip_site_packages(
        root, lib.LINUX_X86_64, bb.STRIP_SITE["controller"]
    )
    prefix = "python/lib/python3.12/site-packages/"
    assert {r.removeprefix(prefix) for r in removed} == {
        "scipy/stats/tests/test_x.py",
        "scipy/tests/test_top.py",
        "sklearn/linear_model/tests/test_ridge.py",
        "xgboost/testing/__init__.py",
        "pyarrow/libarrow_flight.so.2600",
        "pyarrow/libarrow_python_flight.so",
        "pyarrow/libarrow_python_flight.so.2600.0.0",
        "pyarrow/_flight.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/_flight.pyx",
        "pyarrow/flight.py",
        "pyarrow/libarrow_s3.so.2600",
        "pyarrow/_s3fs.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/_s3fs.pyx",
    }
    for kept in (
        "scipy/stats/__init__.py",
        "sklearn/utils/_testing.py",  # imported by sklearn itself, not a test
        "xgboost/__init__.py",
        "pyarrow/parquet/__init__.py",
        "pyarrow/fs.py",
        "pyarrow/_fs.cpython-312-x86_64-linux-gnu.so",
        "pyarrow/libarrow.so.2600",
        "pyarrow/libarrow_substrait.so.2600",  # every pyarrow module links it
    ):
        assert (site / kept).is_file(), kept


def test_meshforge_never_imports_what_is_stripped() -> None:
    gone = ("pyarrow.flight", "pyarrow._flight", "pyarrow._s3fs", "xgboost.testing")
    for path in (REPO / "src" / "meshforge").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module in gone:
            assert f"import {module}" not in text, (path, module)
            assert f"from {module}" not in text, (path, module)


# ------------------------------------------------------------- recompress


def _tar_xz(files: dict[str, bytes], *, preset: int = 0) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 1_700_000_000
            tar.addfile(info, io.BytesIO(data))
    return lzma.compress(raw.getvalue(), format=lzma.FORMAT_XZ, preset=preset)


def _ar(members: list[tuple[str, bytes]], mtime: int = 1793219849) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = (
            f"{name:<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}".encode()
            + b"`\n"
        )
        out += header + data + (b"\n" if len(data) % 2 else b"")
    return bytes(out)


def _headers(blob: bytes) -> list[bytes]:
    out, offset = [], 8
    while offset < len(blob):
        header = blob[offset : offset + 60]
        size = int(header[48:58])
        out.append(header)
        offset += 60 + size + size % 2
    return out


def _deb(tmp_path: Path, payload: dict[str, bytes]) -> Path:
    path = tmp_path / "t.deb"
    path.write_bytes(
        _ar(
            [
                ("debian-binary", b"2.0\n"),
                ("control.tar.gz", b"odd-length-control"),  # 18 bytes: padding
                ("data.tar.xz", _tar_xz(payload)),
            ]
        )
    )
    return path


def _payload() -> dict[str, bytes]:
    # one random 384 KiB block in 4 files: the repeats are farther apart than
    # preset 0's 256 KiB dictionary reaches and well inside preset 9's 64 MiB
    block = random.Random(7).randbytes(384 * 1024)  # noqa: S311 - test data
    return {f"./opt/meshforge/f{i}.bin": block for i in range(4)}


def test_recompress_keeps_every_member_and_shrinks_the_data(tmp_path: Path) -> None:
    deb = _deb(tmp_path, _payload())
    before = deb.read_bytes()
    old_members = bd.read_ar(before)
    old, new = bd.recompress_data_member(deb)
    after = deb.read_bytes()
    members = bd.read_ar(after)

    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.xz"]
    assert members["debian-binary"] == old_members["debian-binary"]
    assert members["control.tar.gz"] == old_members["control.tar.gz"]
    assert (old, new) == (len(old_members["data.tar.xz"]), len(members["data.tar.xz"]))
    assert new < old
    # the tar inside is byte-identical: only the compression changed
    assert lzma.decompress(members["data.tar.xz"]) == lzma.decompress(
        old_members["data.tar.xz"]
    )
    # headers: identical except the data member's size field
    hb, ha = _headers(before), _headers(after)
    assert hb[:2] == ha[:2]
    assert hb[2][:48] == ha[2][:48] and hb[2][58:] == ha[2][58:]
    assert int(ha[2][48:58]) == new
    # the inspector reads it like any .deb
    with tarfile.open(fileobj=io.BytesIO(members["data.tar.xz"]), mode="r:xz") as tar:
        assert len(tar.getnames()) == 4


def test_recompress_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a, b = _deb(tmp_path / "a", _payload()), _deb(tmp_path / "b", _payload())
    bd.recompress_data_member(a)
    bd.recompress_data_member(b)
    assert a.read_bytes() == b.read_bytes()


def test_recompress_refuses_what_is_not_a_deb(tmp_path: Path) -> None:
    bad = tmp_path / "x.deb"
    bad.write_bytes(b"PK\x03\x04 nope")
    with pytest.raises(lib.BundleError, match="not an ar archive"):
        bd.recompress_data_member(bad)
    no_data = tmp_path / "n.deb"
    no_data.write_bytes(_ar([("debian-binary", b"2.0\n")]))
    with pytest.raises(lib.BundleError, match=r"no data\.tar\.xz"):
        bd.recompress_data_member(no_data)
    truncated = tmp_path / "t.deb"
    data = _tar_xz(_payload())
    truncated.write_bytes(_ar([("data.tar.xz", data[: len(data) // 2])]))
    with pytest.raises(lib.BundleError, match="truncated"):
        bd.recompress_data_member(truncated)
    assert not list(tmp_path.glob("*.recompress"))  # nothing half-written


def test_the_preset_is_the_measured_one() -> None:
    assert bd.DATA_XZ_PRESET == 9
