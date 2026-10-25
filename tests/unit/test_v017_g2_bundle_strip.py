"""Unit tests for the controller bundle's site-packages strip (V0.17 G2, Lead
decision on progress-note item 14: drop test suites/headers so the controller
.deb fits the Q17 150 MB budget with the size check on).

``scripts/build_bundle.py strip_site_packages`` runs against a fake
site-packages; the real controller is run from the built .deb in
``tests/component/packaging/test_v017_g2_controller_deb.py``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import sys
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

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX layout only")
LINUX = lib.LINUX_X86_64


def _hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode()


def _write(path: Path, data: bytes = b"x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"{_hash(data)},{len(data)}"


def _fake_site(root: Path) -> Path:
    site = LINUX.site_packages_path(root)
    rows = {
        "numpy/__init__.py": _write(site / "numpy/__init__.py"),
        "numpy/tests/test_a.py": _write(site / "numpy/tests/test_a.py"),
        "numpy/linalg/__init__.py": _write(site / "numpy/linalg/__init__.py"),
        "numpy/linalg/tests/test_b.py": _write(site / "numpy/linalg/tests/test_b.py"),
        "numpy/_core/include/numpy/x.h": _write(site / "numpy/_core/include/numpy/x.h"),
        "numpy/testing/__init__.py": _write(site / "numpy/testing/__init__.py"),
        "pyarrow/__init__.py": _write(site / "pyarrow/__init__.py"),
        "pyarrow/tests/test_c.py": _write(site / "pyarrow/tests/test_c.py"),
        "pyarrow/include/arrow/api.h": _write(site / "pyarrow/include/arrow/api.h"),
    }
    record = "".join(f"{name},{row}\n" for name, row in rows.items())
    record += "numpy-2.dist-info/RECORD,,\n"
    _write(site / "numpy-2.dist-info/RECORD", record.encode())
    return site


def test_controller_rules_drop_tests_and_headers_and_their_record_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    site = _fake_site(root)

    removed = bb.strip_site_packages(root, LINUX, bb.STRIP_SITE["controller"])

    prefix = "python/lib/python3.12/site-packages/"
    assert removed == sorted(
        prefix + name
        for name in (
            "numpy/_core/include/numpy/x.h",
            "numpy/linalg/tests/test_b.py",
            "numpy/tests/test_a.py",
            "pyarrow/include/arrow/api.h",
            "pyarrow/tests/test_c.py",
        )
    )
    for gone in ("numpy/tests", "numpy/linalg/tests", "numpy/_core/include",
                 "pyarrow/tests", "pyarrow/include"):  # fmt: skip
        assert not (site / gone).exists(), gone
    # importable modules stay, including numpy.testing (numpy imports it lazily)
    for kept in ("numpy/__init__.py", "numpy/linalg/__init__.py",
                 "numpy/testing/__init__.py", "pyarrow/__init__.py"):  # fmt: skip
        assert (site / kept).is_file(), kept
    record = (site / "numpy-2.dist-info/RECORD").read_text()
    assert "tests/" not in record and "include/" not in record
    assert "numpy/testing/__init__.py," in record
    assert "numpy-2.dist-info/RECORD,," in record
    # a second run is a no-op
    assert bb.strip_site_packages(root, LINUX, bb.STRIP_SITE["controller"]) == []


def test_agent_rules_strip_nothing(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    site = _fake_site(root)
    before = sorted(p.relative_to(site) for p in site.rglob("*"))

    assert bb.strip_site_packages(root, LINUX, bb.STRIP_SITE["agent"]) == []
    assert sorted(p.relative_to(site) for p in site.rglob("*")) == before


def test_every_role_has_rules_and_every_rule_says_why() -> None:
    assert set(bb.STRIP_SITE) == set(bb.ROLE_EXTRAS)
    for rules in bb.STRIP_SITE.values():
        for pattern, why in rules:
            assert pattern and why
            assert not pattern.startswith("/") and ".." not in pattern
