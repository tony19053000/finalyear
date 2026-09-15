"""Hardware/software inventory of one lab laptop -> JSON (roadmap task 0.2.2).

Usage (from the repo root, after ``uv sync``)::

    uv run python lab/inventory.py --out lab/reports/inventory/<hostname>.json
    uv run python lab/inventory.py --pretty          # JSON to stdout
    uv run python lab/inventory.py --timeout 120     # longer total run budget

Every section is an isolated probe ``{"ok", "source", "value" | "error"}``; a
missing tool or broken driver is recorded, never fatal. ``problems`` lists what a
human must fix even when the probe itself worked (e.g. a blocked NVIDIA driver).
The whole run is bounded by ``--timeout`` (default 60 s). Exit code 0 even when
probes fail; 2 for usage errors, 1 when the output file cannot be written. A short
summary goes to stderr. No admin rights needed; no secrets collected (docs/LAB.md
§6): no SSIDs, usernames, serial numbers, env vars, or public IPs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _inventory import runner  # noqa: E402
from _inventory.model import JSON  # noqa: E402
from _inventory.probes import collect  # noqa: E402
from _inventory.sanitize import Sanitizer, redaction_source  # noqa: E402
from _inventory.summary import render  # noqa: E402

REPO_ROOT: Final = _LAB_DIR.parent
EXIT_OK: Final = 0
EXIT_WRITE_FAILED: Final = 1
MIN_TOTAL_S: Final = 5.0
MAX_TOTAL_S: Final = 3600.0


def build_document(repo: Path = REPO_ROOT) -> dict[str, JSON]:
    """Collect and sanitise the inventory document."""
    doc = collect(repo)
    doc["redaction"] = redaction_source()
    try:
        home: Path | None = Path.home()
    except (RuntimeError, KeyError):
        home = None
    clean: dict[str, JSON] = Sanitizer(home).tree(doc)
    return clean


def dumps(doc: dict[str, JSON], *, pretty: bool) -> str:
    """Deterministic (sorted keys) UTF-8-safe JSON text ending in a newline."""
    return (
        json.dumps(
            doc, sort_keys=True, ensure_ascii=False, indent=2 if pretty else None
        )
        + "\n"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab/inventory.py",
        description="Write this machine's hardware/software inventory as JSON.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write JSON here (parent dirs are created); default: stdout",
    )
    parser.add_argument("--pretty", action="store_true", help="indent the JSON")
    parser.add_argument(
        "--no-summary", action="store_true", help="do not print the summary to stderr"
    )
    parser.add_argument(
        "--timeout",
        type=_total_seconds,
        default=runner.DEFAULT_TOTAL_S,
        metavar="SECONDS",
        help=(
            "wall-clock budget for the whole run (default %(default)g). Commands "
            "still pending when it runs out are skipped and recorded as errors."
        ),
    )
    return parser


def _total_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not MIN_TOTAL_S <= value <= MAX_TOTAL_S:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_TOTAL_S:g} and {MAX_TOTAL_S:g} seconds"
        )
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)  # exits with 2 on usage errors
    runner.start_budget(args.timeout)
    doc = build_document()
    text = dumps(doc, pretty=args.pretty)
    if args.out is None:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.flush()
    else:
        out: Path = args.out
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            sys.stderr.write(f"inventory: cannot write {out}: {exc}\n")
            return EXIT_WRITE_FAILED
    if not args.no_summary:
        summary = render(doc)
        if args.out is not None:
            summary += f"\nwritten to   : {args.out}"
        sys.stderr.write(summary + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
