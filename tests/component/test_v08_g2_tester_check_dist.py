"""V0.8 G2 tester r1: the post-build CSP gate (frontend/scripts/check-dist.mjs)
rejects each inline-style/script/CDN/font pattern it claims to."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
GOOD = '<!doctype html><html><head><link rel="stylesheet" href="/assets/a.css"></head><body><div id="root"></div><script type="module" src="/assets/a.js"></script></body></html>'  # noqa: E501 - tester r1 test data / T-id name


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


def _dist(
    tmp: Path, html: str = GOOD, css: str = "a{color:red}", js: str = "export{}"
) -> Path:
    (tmp / "assets").mkdir()
    (tmp / "index.html").write_text(html)
    (tmp / "assets" / "a.css").write_text(css)
    (tmp / "assets" / "a.js").write_text(js)
    return tmp


def test_clean_dist_passes(tmp_path: Path) -> None:
    assert _run(_dist(tmp_path)).returncode == 0


@pytest.mark.parametrize(
    ("kind", "html", "css", "js"),
    [
        ("style attr", GOOD.replace("<div ", '<div style="color:red" '), "a{}", ""),
        (
            "style attr single quote",
            GOOD.replace("<div ", "<div style='x' "),
            "a{}",
            "",
        ),
        (
            "style element",
            GOOD.replace("<body>", "<body><style>a{}</style>"),
            "a{}",
            "",
        ),
        (
            "inline script",
            GOOD.replace("<body>", "<body><script>alert(1)</script>"),
            "a{}",
            "",
        ),
        ("handler", GOOD.replace("<div ", '<div onclick="x()" '), "a{}", ""),
        (
            "cdn script",
            GOOD.replace("/assets/a.js", "https://cdn.example/a.js"),
            "a{}",
            "",
        ),
        (
            "proto-relative",
            GOOD.replace("/assets/a.css", "//cdn.example/a.css"),
            "a{}",
            "",
        ),
        ("css import url", GOOD, "a{background:url(https://x/y.png)}", ""),
        ("css data font", GOOD, "@font-face{src:url(data:font/woff2;base64,AAAA)}", ""),
        ("js style attr", GOOD, "a{}", "e.innerHTML='<i style=\"x\">'"),
        ("js setAttribute", GOOD, "a{}", 'el.setAttribute("style","x")'),
    ],
)
def test_bad_dist_fails(
    tmp_path: Path, kind: str, html: str, css: str, js: str
) -> None:
    result = _run(_dist(tmp_path, html, css, js))
    assert result.returncode == 1, (kind, result.stdout, result.stderr)


def test_missing_index_html_fails(tmp_path: Path) -> None:
    assert _run(tmp_path).returncode == 1
