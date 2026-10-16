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


def _dist_js(tmp: Path, js: str) -> Path:
    (tmp / "assets").mkdir()
    (tmp / "index.html").write_text(HTML)
    (tmp / "assets" / "a.js").write_text(js)
    return tmp


def test_the_qr_librarys_dead_table_builder_is_the_only_exception(
    tmp_path: Path,
) -> None:
    """D199: qrcode-generator's never-called HTML builders are ignored."""
    done = _run(_dist_js(tmp_path, QR_DEAD))
    assert done.returncode == 0, done.stderr


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
        assert re.search(rf"const {page} = lazy\(\(\) => import\(", app), page
        assert not re.search(rf"^import \{{[^}}]*\b{page}\b", app, re.M), page


def test_the_built_dashboard_meets_the_budget() -> None:
    """When a build is present (the e2e tier builds it), it passes the gate."""
    dist = ROOT / "src" / "meshforge" / "controller" / "web" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("dashboard not built: cd frontend && npm run build")
    chunks = list((dist / "assets").glob("*.js"))
    assert len(chunks) > 3, "the build is not split"
    done = _run(dist)
    assert done.returncode == 0, done.stderr
