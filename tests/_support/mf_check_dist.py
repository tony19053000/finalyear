"""Synthetic dashboard builds for the post-build gate
(``frontend/scripts/check-dist.mjs``; V0.8 G2 D169/D181, G5 D195, C4 fix).

The gate also requires the "hive." wordmark utilities (``app/Wordmark.tsx``) in
the built CSS. A test that builds a small fake ``dist`` to exercise ONE rule
writes :data:`WORDMARK_CSS` alongside (:func:`write_wordmark_css`) so the dist is
otherwise clean; a test of the styles rule itself leaves it out.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

FRONTEND: Final = Path(__file__).resolve().parents[2] / "frontend"

#: The selectors check-dist.mjs REQUIRED_SELECTORS looks for, as Tailwind
#: escapes them in the built CSS (minified: no space before ``{``).
WORDMARK_SELECTORS: Final[tuple[str, ...]] = (
    r".text-\[34px\]",
    r".text-\[40px\]",
    r".font-display",
    r".tracking-\[-0\.045em\]",
    r".ml-\[0\.06em\]",
    r".h-\[0\.34em\]",
    r".w-\[0\.3em\]",
    r".bg-accent",
    r".\[clip-path\:polygon\(50\%_0\,100\%_25\%\,100\%_75\%\,50\%_100\%\,0_75\%\,0_25\%\)\]",
)
WORDMARK_CSS: Final = "".join(
    f"{selector}{{color:red}}" for selector in WORDMARK_SELECTORS
)
WORDMARK_CSS_NAME: Final = "wordmark.css"
#: What check-dist prints when the wordmark utilities are missing.
STYLES_MESSAGE: Final = "wordmark, app/Wordmark.tsx"


def write_wordmark_css(dist: Path) -> Path:
    """Add the wordmark utilities to a synthetic ``dist`` (in ``assets/``)."""
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / WORDMARK_CSS_NAME).write_text(WORDMARK_CSS)
    return dist


def run_check_dist(dist: Path) -> subprocess.CompletedProcess[str]:
    """Run the gate on ``dist``; skip when node is not installed."""
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
