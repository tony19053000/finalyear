"""V0.8 G5 tester r1: independent static checks of item 0 f (chunk gate) and
g (status-token contrast), computed here from the CSS/dist, not from the
implementation's own tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
TOKENS = ROOT / "frontend" / "src" / "styles" / "tokens.css"
DIST = ROOT / "src" / "meshforge" / "controller" / "web" / "dist"
SURFACES = ("bg", "surface", "surface-raised", "surface-sunken")
STATUSES = ("ok", "busy", "degraded", "failed", "unknown")
TEXT = ("fg", "fg-muted", "fg-subtle")


def _blocks() -> dict[str, dict[str, str]]:
    css = TOKENS.read_text("utf-8")
    out: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        names = [s.strip() for s in selector.split(",")]
        theme = "light" if any('"light"' in n for n in names) else "dark"
        out.setdefault(theme, {}).update(
            dict(re.findall(r"--mf-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6,8})\s*;", body))
        )
    return out


def _rgba(hex_: str) -> tuple[float, float, float, float]:
    h = hex_.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    return r, g, b, a


def _blend(top: str, under: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b, a = _rgba(top)
    return tuple(a * c + (1 - a) * u for c, u in zip((r, g, b), under, strict=True))  # type: ignore[return-value]


def _lum(rgb: tuple[float, ...]) -> float:
    def lin(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_status_colour_is_4_5_to_1_on_its_soft_tone_over_every_surface(
    theme: str,
) -> None:
    tokens = _blocks()[theme]
    bad: list[str] = []
    for status in STATUSES:
        fg = _rgba(tokens[f"status-{status}"])[:3]
        for surface in SURFACES:
            base = _rgba(tokens[surface])[:3]
            for tone in (None, tokens[f"status-{status}-soft"]):
                bg = base if tone is None else _blend(tone, base)
                r = _ratio(fg, bg)
                if r < 4.5:
                    bad.append(f"{theme} {status} on {surface} soft={tone}: {r:.2f}")
    assert not bad, bad


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_body_text_tokens_keep_4_5_to_1_on_every_surface(theme: str) -> None:
    tokens = _blocks()[theme]
    bad = [
        f"{theme} {t} on {s}: {_ratio(_rgba(tokens[t])[:3], _rgba(tokens[s])[:3]):.2f}"
        for t in TEXT
        for s in SURFACES
        if _ratio(_rgba(tokens[t])[:3], _rgba(tokens[s])[:3]) < 4.5
    ]
    assert not bad, bad


def test_no_javascript_chunk_exceeds_350_kb_and_the_entry_stays_small() -> None:
    if not (DIST / "index.html").is_file():
        pytest.skip("dashboard not built")
    sizes = {p.name: p.stat().st_size for p in (DIST / "assets").glob("*.js")}
    assert sizes, "no chunks?"
    over = {n: s for n, s in sizes.items() if s > 350_000}
    assert not over, over
    # the lazy-split really happened: more than just an entry + vendor chunk
    assert len(sizes) >= 8, sizes
    entry = [n for n in sizes if n.startswith("index-")]
    assert entry and max(sizes[n] for n in entry) <= 350_000


def test_built_bundle_carries_no_secret_shaped_literal() -> None:
    if not (DIST / "index.html").is_file():
        pytest.skip("dashboard not built")
    pat = re.compile(
        rb"mf1-[A-Z2-7]{16,}|Bearer [A-Za-z0-9_-]{20,}|BEGIN [A-Z ]*PRIVATE KEY"
    )
    hits = [
        p.name
        for p in DIST.rglob("*")
        if p.is_file()
        and p.suffix in {".js", ".css", ".html", ".map"}
        and pat.search(p.read_bytes())
    ]
    assert not hits, hits
