"""V0.8 G2 tester r1: the post-build CSP gate (frontend/scripts/check-dist.mjs)
rejects each inline-style/script/CDN/font pattern it claims to."""

from __future__ import annotations

from pathlib import Path

import pytest
from mf_check_dist import STYLES_MESSAGE, run_check_dist, write_wordmark_css

pytestmark = pytest.mark.component

GOOD = '<!doctype html><html><head><link rel="stylesheet" href="/assets/a.css"></head><body><div id="root"></div><script type="module" src="/assets/a.js"></script></body></html>'  # noqa: E501 - tester r1 test data / T-id name
_run = run_check_dist


def _dist(
    tmp: Path, html: str = GOOD, css: str = "a{color:red}", js: str = "export{}"
) -> Path:
    (tmp / "assets").mkdir()
    (tmp / "index.html").write_text(html)
    (tmp / "assets" / "a.css").write_text(css)
    (tmp / "assets" / "a.js").write_text(js)
    return write_wordmark_css(tmp)


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


def test_dist_without_the_wordmark_styles_fails(tmp_path: Path) -> None:
    """C4: a build whose CSS lacks the "hive." wordmark utilities is refused."""
    dist = _dist(tmp_path)
    (dist / "assets" / "wordmark.css").unlink()
    result = _run(dist)
    assert result.returncode == 1, result.stdout
    assert "its styles" in result.stderr
    assert STYLES_MESSAGE in result.stderr
    assert r"CSS is missing .text-\[34px\]" in result.stderr


@pytest.mark.parametrize(
    ("kind", "css"),
    [
        # only a longer class sharing the prefix: no selector boundary
        ("prefix only", ".font-display-x{a:b}"),
        # the selector text without a rule after it
        ("no rule", "/* .font-display */"),
    ],
)
def test_a_wordmark_selector_must_be_a_whole_selector(
    tmp_path: Path, kind: str, css: str
) -> None:
    dist = _dist(tmp_path, css=css)
    wordmark = dist / "assets" / "wordmark.css"
    wordmark.write_text(wordmark.read_text().replace(".font-display{", ".zz{"))
    result = _run(dist)
    assert result.returncode == 1, (kind, result.stdout)
    assert "CSS is missing .font-display (wordmark" in result.stderr, kind


@pytest.mark.parametrize(
    ("kind", "css"),
    [
        ("grouped", ".a,.font-display{a:b}"),
        ("unminified", ".font-display {\n  a: b;\n}"),
        ("grouped unminified", ".font-display ,\n.a { a: b }"),
    ],
)
def test_a_wordmark_selector_counts_before_brace_or_comma(
    tmp_path: Path, kind: str, css: str
) -> None:
    dist = _dist(tmp_path, css=css)
    wordmark = dist / "assets" / "wordmark.css"
    wordmark.write_text(wordmark.read_text().replace(".font-display{", ".zz{"))
    result = _run(dist)
    assert result.returncode == 0, (kind, result.stderr)
