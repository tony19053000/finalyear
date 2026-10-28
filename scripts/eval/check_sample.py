"""Re-run the analysis + report on the committed sample and compare hashes
(D419: "a CI job re-runs analysis on a committed small sample and checks the
hashes").

    uv run --group evaluation python scripts/eval/check_sample.py [--no-figures] [--update]

The sample is ``scripts/eval/sample/raw`` (a small synthetic campaign in the
exact raw format the runner writes: 2 families x 2 sizes x 3 arms x 3
repetitions of pure noise, so its verdicts must be ``not_separable``). The
expected SHA-256 of every output - ``analysis/analysis.json``,
``analysis/tables/*.csv``, ``analysis/SHA256SUMS``, ``report.md`` and
``figures/*.svg`` - is ``scripts/eval/sample/expected.sha256``.

``--no-figures`` checks the analysis files only (no matplotlib needed: the
unit tier runs this on every OS). ``--update`` rewrites the expected file
after an intended change of the analysis or the report (say why in the
commit). Figure bytes hold for the locked matplotlib version only.

Exit codes: 0 every hash matches; 1 a mismatch (each listed); 2 an error.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze import run_analysis
from evalkit.canonical import sha256_file, write_text
from evalkit.raw import RawError
from make_report import make_report

__all__ = ["EXPECTED", "SAMPLE", "compute_hashes", "main"]

SAMPLE = Path(__file__).resolve().parent / "sample"
EXPECTED = SAMPLE / "expected.sha256"


def compute_hashes(out: Path, *, figures: bool) -> dict[str, str]:
    """Analyse the sample into ``out``; ``{relative path: sha256}``."""
    run_analysis([SAMPLE / "raw"], out / "analysis")
    if figures:
        make_report(
            out / "analysis" / "analysis.json",
            out / "report.md",
            figures_dir=out / "figures",
        )
    return {
        path.relative_to(out).as_posix(): sha256_file(path)
        for path in sorted(out.rglob("*"))
        if path.is_file()
    }


def _read_expected() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in EXPECTED.read_text("utf-8").splitlines():
        if line.strip():
            digest, name = line.split("  ", 1)
            out[name] = digest
    return out


def _matplotlib_version() -> str:
    """The installed matplotlib version (figure bytes depend on it)."""
    try:
        import matplotlib
    except ModuleNotFoundError:
        return "not installed"
    return str(matplotlib.__version__)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="check_sample.py", description=__doc__.split("\n")[0]
    )
    p.add_argument("--no-figures", action="store_true")
    p.add_argument("--update", action="store_true")
    args = p.parse_args(argv)
    figures = not args.no_figures
    if args.update and not figures:
        print("check_sample: --update needs the figures too", file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="mf-eval-sample-") as tmp:
            got = compute_hashes(Path(tmp), figures=figures)
    except ModuleNotFoundError as exc:
        print(f"check_sample: {exc} (uv sync --group evaluation)", file=sys.stderr)
        return 2
    except (OSError, RawError, ValueError) as exc:
        print(f"check_sample: {exc}", file=sys.stderr)
        return 2
    if args.update:
        write_text(EXPECTED, "".join(f"{d}  {n}\n" for n, d in sorted(got.items())))
        print(f"wrote {EXPECTED} ({len(got)} files)")
        return 0
    expected = _read_expected()
    if not figures:
        expected = {k: v for k, v in expected.items() if k.startswith("analysis/")}
    problems = [
        f"{name}: expected {digest[:16]}, got {str(got.get(name))[:16]}"
        for name, digest in sorted(expected.items())
        if got.get(name) != digest
    ]
    problems += [
        f"{name}: not in {EXPECTED.name}" for name in sorted(set(got) - set(expected))
    ]
    if problems:
        print("sample analysis is NOT reproducible:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        if figures:
            print(
                f"  (matplotlib {_matplotlib_version()}; figure bytes hold for "
                "the version in uv.lock only)",
                file=sys.stderr,
            )
        return 1
    print(f"sample reproduces: {len(expected)} files match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
