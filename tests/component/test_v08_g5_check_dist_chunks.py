"""V0.8 G5 item 0f (D195): the dashboard is code-split per route and the
post-build gate (frontend/scripts/check-dist.mjs) refuses any JavaScript chunk
over 350 kB - so a heavy dependency slipping into a shared chunk fails the
build, not the evaluation laptop's first load."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
LIMIT = 350 * 1000
HTML = (
    '<!doctype html><html><head></head><body><div id="root"></div>'
    '<script type="module" src="/assets/a.js"></script></body></html>'
)


def _run(dist: Path) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return subprocess.run(
        [node, "scripts/check-dist.mjs", str(dist)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _dist(tmp: Path, js_bytes: int) -> Path:
    (tmp / "assets").mkdir()
    (tmp / "index.html").write_text(HTML)
    (tmp / "assets" / "a.js").write_text("x" * js_bytes)
    return tmp


def test_a_chunk_at_the_limit_passes(tmp_path: Path) -> None:
    done = _run(_dist(tmp_path, LIMIT))
    assert done.returncode == 0, done.stderr


def test_a_chunk_over_the_limit_fails_and_says_what_to_do(tmp_path: Path) -> None:
    done = _run(_dist(tmp_path, LIMIT + 1))
    assert done.returncode == 1
    assert "assets/a.js" in done.stderr
    assert "React.lazy" in done.stderr


QR_DEAD = 'createTableTag:function(){let n=``;n+=`<table style="`;n+=`<td style="`}'
#: The library's own chunk (vite.config.ts manualChunks, V0.8 G7 D204).
QR_CHUNK = "qrcode-generator-Bx_9-a1.js"


def _dist_js(tmp: Path, js: str, name: str = QR_CHUNK) -> Path:
    (tmp / "assets").mkdir()
    (tmp / "index.html").write_text(HTML)
    (tmp / "assets" / name).write_text(js)
    return tmp


def test_the_qr_librarys_dead_table_builder_is_the_only_exception(
    tmp_path: Path,
) -> None:
    """D199: qrcode-generator's never-called HTML builders are ignored."""
    done = _run(_dist_js(tmp_path, QR_DEAD))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "name", ["a.js", "SettingsRoutes-abc.js", "index-qrcode-generator-x.js"]
)
def test_the_exception_holds_only_in_the_qr_librarys_own_chunk(
    tmp_path: Path, name: str
) -> None:
    """D204: the same dead builder inside any other chunk (app code, or a
    chunk merely mentioning the name) is refused."""
    done = _run(_dist_js(tmp_path, QR_DEAD, name))
    assert done.returncode == 1, done.stdout
    assert name in done.stderr


@pytest.mark.parametrize(
    "js",
    [
        # the same literal without the library around it
        'n+=`<table style="`',
        # the library present, but some other inline style
        QR_DEAD + 'x.innerHTML=`<div style="color:red">`',
        QR_DEAD + "el.setAttribute('style','x')",
    ],
)
def test_anything_else_with_style_still_fails(tmp_path: Path, js: str) -> None:
    done = _run(_dist_js(tmp_path, js))
    assert done.returncode == 1, done.stdout


def test_the_app_routes_are_lazy() -> None:
    """Every page behind the shell is a dynamic import (one chunk per route)."""
    app = (FRONTEND / "src" / "app" / "App.tsx").read_text(encoding="utf-8")
    for page in (
        "OverviewPage",
        "NodesPage",
        "NodeDetailPage",
        "JobsListPage",
        "JobDetailPage",
        "SubmitJobPage",
        "SettingsRoutes",
    ):
        loader = re.search(
            rf"const {page} = lazy\(\(\) => PAGE_LOADERS\.(\w+)\(\)", app
        )
        assert loader, page
        assert re.search(
            rf"^  {loader.group(1)}: \(\) => import\(\"\.\./pages/", app, re.M
        ), page
        assert not re.search(rf"^import \{{[^}}]*\b{page}\b", app, re.M), page
    # Loaded in the background once signed in (offline navigation, G2 T-2).
    assert "<PreloadPages />" in app


def test_the_built_dashboard_meets_the_budget() -> None:
    """When a FRESH build is present (the e2e tier builds it), it passes the
    gate. A missing or stale build skips with the rebuild command (D206)."""
    from mf_dashboard import require_fresh_dist

    dist = require_fresh_dist()
    chunks = list((dist / "assets").glob("*.js"))
    assert len(chunks) > 3, "the build is not split"
    # D204: the QR library sits in its own chunk.
    assert [p.name for p in chunks if p.name.startswith("qrcode-generator-")]
    done = _run(dist)
    assert done.returncode == 0, done.stderr
