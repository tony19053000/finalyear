"""Analysis -> the Markdown evaluation report + SVG figures (D419).

    uv run --group eval python scripts/eval/make_report.py \\
        --analysis OUT_DIR/analysis.json --out docs/reports/v1.0-evaluation.md \\
        [--figures-dir docs/reports/v1.0-evaluation-figures] [--no-figures]

Figures need matplotlib (the ``eval`` dependency group: ``uv sync --group
eval``); ``--no-figures`` writes the report without them. Same analysis ->
same report and figure bytes.

Exit codes: 0 written; 2 bad arguments, unreadable analysis or no matplotlib.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.canonical import sha256_bytes, write_text
from evalkit.report import render_report

__all__ = ["main", "make_report"]


def make_report(
    analysis_path: Path,
    out: Path,
    *,
    figures_dir: Path | None = None,
    figures: bool = True,
) -> list[str]:
    """Write the report (and figures); the figure file names."""
    data = analysis_path.read_bytes()
    analysis = json.loads(data)
    folder = figures_dir or out.with_name(out.stem + "-figures")
    made: list[str] = []
    if figures:
        from evalkit.figures import write_figures

        made = write_figures(analysis, folder)
    relative = Path(os.path.relpath(folder, out.parent)).as_posix()
    text = render_report(
        analysis,
        analysis_sha256=sha256_bytes(data),
        figures=made,
        figures_dir=relative,
    )
    write_text(out, text)
    return made


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="make_report.py", description=__doc__.split("\n")[0]
    )
    p.add_argument("--analysis", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--figures-dir", type=Path, default=None)
    p.add_argument("--no-figures", action="store_true")
    args = p.parse_args(argv)
    try:
        made = make_report(
            args.analysis,
            args.out,
            figures_dir=args.figures_dir,
            figures=not args.no_figures,
        )
    except ModuleNotFoundError as exc:
        print(f"make_report: {exc} (uv sync --group eval)", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"make_report: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.out} and {len(made)} figure(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
