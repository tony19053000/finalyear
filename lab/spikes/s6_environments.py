"""Spike S6 (roadmap 0.2.10): execution environments on one laptop - is Docker
usable here, does a container get the GPU (ARCHITECTURE Q4, ADR-0004), and what
does an environment cost (container start, image pull, PyTorch-CUDA container
start to first CUDA op, uv venv creation cold / warm)?

One command per laptop, then one command anywhere to build the comparison::

    # on every laptop (Docker running; Windows: Docker Desktop with WSL2 backend):
    uv run python lab/spikes/s6_environments.py run
    # smoke test only (2 repetitions, INVALID rows, no PyTorch image):
    uv run python lab/spikes/s6_environments.py run --quick
    # after collecting every host's JSON in one folder:
    uv run python lab/spikes/s6_environments.py report \\
        --results lab/reports/raw/2026-09-20-S6-environments \\
        --out lab/reports/2026-09-20-S6-environments.md --labels L1=host1,L2=host2

``run`` measures this machine with ``--warmup`` + ``--reps`` repetitions per
case and writes one result envelope to
``lab/reports/raw/<today>-S6-environments/<host>.json``. Standard library plus
the ``docker`` and ``uv`` command lines; torch is not needed on the host. The
``docker-gpu-torch`` case pulls a ~3.7 GB image once (``--skip-gpu-torch`` to
opt out; it warns before pulling). Every temp dir and container is removed at
the end, also on Ctrl-C.

Exit codes: ``0`` every non-skipped case complete; ``1`` cannot write the
output; ``2`` usage error; ``3`` results written but incomplete (a typed
failure - see ``errors`` in the JSON; cases skipped by a flag do not count);
``130`` interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

_LAB_DIR: Final = Path(__file__).resolve().parents[1]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from spikes._harness import MIN_VALID_REPS  # noqa: E402
from spikes._harness.envelope import (  # noqa: E402
    RAW_ROOT,
    ResultCollisionError,
    load_dir,
    raw_dir,
    raw_result_path,
    write_envelope,
)
from spikes._harness.report import (  # noqa: E402
    FORCE_HELP,
    TEMPLATE_PATH,
    ReportMergeError,
    TemplateError,
    render_report,
    write_report,
)
from spikes._harness.reps import JSON  # noqa: E402
from spikes._s6 import SPIKE, TOPIC  # noqa: E402
from spikes._s6.docker import GPU_TORCH_IMAGE  # noqa: E402
from spikes._s6.report import render_environments  # noqa: E402
from spikes._s6.run import RunConfig, run_environments  # noqa: E402
from spikes._s6.verdict import host_verdict  # noqa: E402

EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_INTERRUPTED: Final = 130
MIN_BUDGET_S: Final = 10.0
MAX_BUDGET_S: Final = 24 * 3600.0
_DAY_DIR_RE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_IMAGE_RE: Final = re.compile(r"^[A-Za-z0-9._/:@-]{1,256}$")


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------ argument types
def _number(
    lo: float, hi: float, *, integer: bool = False
) -> Callable[[str], float | int]:
    def parse(text: str) -> float | int:
        try:
            value: float | int = int(text) if integer else float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo:g} and {hi:g}")
        return value

    return parse


def _name(text: str) -> str:
    if not _NAME_RE.match(text):
        raise argparse.ArgumentTypeError("use 1-64 characters from A-Z a-z 0-9 . _ -")
    return text


def _image(text: str) -> str:
    if not _IMAGE_RE.match(text) or text.startswith("-"):
        raise argparse.ArgumentTypeError(f"not an image reference: {text!r}")
    return text


def _labels(text: str) -> dict[str, str]:
    """``L1=host1,L2=host2`` -> ``{"host1": "L1", "host2": "L2"}``."""
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        label, sep, host = part.partition("=")
        if not sep or not label.strip() or not host.strip():
            raise argparse.ArgumentTypeError(f"expected LABEL=HOST, got {part!r}")
        out[host.strip()] = label.strip()
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab/spikes/s6_environments.py",
        description="Spike S6: Docker / GPU-in-container / uv venv costs of this laptop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="measure this machine and write its result JSON")
    run.add_argument(
        "--reps",
        type=_number(1, 1000, integer=True),
        default=0,
        help=f"measured repetitions per case (>= {MIN_VALID_REPS} to be valid; "
        "default 5, quick 2)",
    )
    run.add_argument(
        "--warmup",
        type=_number(0, 100, integer=True),
        default=-1,
        help="warmup repetitions per case, kept in the JSON but not summarised (default 1)",
    )
    run.add_argument(
        "--quick",
        action="store_true",
        help="smoke run: 2 repetitions (every row INVALID, says so) and no gpu-torch case",
    )
    run.add_argument(
        "--skip-docker", action="store_true", help="run only the venv cases"
    )
    run.add_argument(
        "--skip-gpu", action="store_true", help="skip both GPU container cases"
    )
    run.add_argument(
        "--skip-gpu-torch",
        action="store_true",
        help="skip the PyTorch-CUDA image case (no ~3.7 GB download)",
    )
    run.add_argument(
        "--no-network",
        action="store_true",
        help="skip cases that download (docker-pull-cold, venv-cold, venv-warm/start)",
    )
    run.add_argument(
        "--gpu-torch-image",
        type=_image,
        default=GPU_TORCH_IMAGE,
        metavar="IMAGE",
        help="PyTorch-CUDA image for docker-gpu-torch (default %(default)s)",
    )
    run.add_argument(
        "--name", type=_name, help="identity shown in the report (default: hostname)"
    )
    run.add_argument(
        "--out-dir",
        type=Path,
        help=f"result folder (default {RAW_ROOT.name}/<today>-{TOPIC} under lab/reports)",
    )
    run.add_argument(
        "--timeout",
        type=_number(MIN_BUDGET_S, MAX_BUDGET_S),
        default=0.0,
        metavar="SECONDS",
        help="wall-clock budget for the whole run (default 3600, quick 900); cases that "
        "no longer fit are recorded as budget_exhausted",
    )
    run.add_argument(
        "--pause",
        type=_number(0, 60),
        default=0.2,
        metavar="SECONDS",
        help="pause between repetitions (default %(default)g)",
    )

    report = sub.add_parser(
        "report", help="build the comparison report from result JSONs"
    )
    report.add_argument(
        "--results",
        action="append",
        required=True,
        type=Path,
        metavar="DIR",
        help="folder with S6 result JSONs (repeatable)",
    )
    report.add_argument(
        "--out", required=True, type=Path, help="markdown report to write"
    )
    report.add_argument(
        "--force",
        action="store_true",
        help=FORCE_HELP,
    )
    report.add_argument(
        "--labels",
        type=_labels,
        default={},
        help="short names, e.g. L1=hostA,L2=hostB; laptops named here but missing a "
        "result file show up as TBD",
    )
    report.add_argument(
        "--date",
        type=date.fromisoformat,
        help="report date YYYY-MM-DD (default: from the folder name, else today)",
    )
    report.add_argument(
        "--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS
    )
    return parser


# ------------------------------------------------------------------------- run
def print_summary(envelope: dict[str, JSON]) -> None:
    """Loud, human-readable summary on stderr (warnings last, so they are seen)."""
    verdict = host_verdict(envelope, str(envelope["host"]["label"]))
    _say(f"--- S6 environments: {verdict.host} ({verdict.os_text}) ---")
    _say(f"docker: {verdict.docker.cell()}")
    _say(f"gpu-in-container: {verdict.gpu_in_container.cell()}")
    _say(f"torch-cuda-in-container: {verdict.torch_cuda.cell()}")
    _say(f"runtime recommendation (ADR-0004): {verdict.recommendation.cell()}")
    for label, value in verdict.costs.items():
        _say(f"    {label}: {value}")
    _say(verdict.calibration)
    for err in envelope["errors"]:
        _say(f"not measured: {err['failure_type']}: {err['message']}")
    for warning in envelope["extra"].get("warnings") or []:
        _say(f"!! {warning}")


def cmd_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    cfg = RunConfig(
        reps=args.reps,
        warmup=args.warmup,
        quick=args.quick,
        skip_docker=args.skip_docker,
        skip_gpu=args.skip_gpu,
        skip_gpu_torch=args.skip_gpu_torch,
        no_network=args.no_network,
        gpu_torch_image=args.gpu_torch_image,
        name=args.name,
        budget_s=args.timeout,
        pause_s=args.pause,
    )
    if cfg.effective_reps() < MIN_VALID_REPS:
        _say(
            f"warning: {cfg.effective_reps()} repetitions < {MIN_VALID_REPS}: every row "
            "will be INVALID"
        )
    if cfg.quick:
        _say("QUICK MODE: smoke run only, not a measurement of this machine.")
    _say(
        f"{cfg.effective_warmup()} warmup + {cfg.effective_reps()} measured repetitions "
        f"per case, budget {cfg.effective_budget():g} s"
    )
    run = run_environments(cfg, log=_say)
    today = datetime.now(UTC).date()
    out_dir: Path = args.out_dir or raw_dir(TOPIC, today)
    host = str(run.envelope["host"]["label"])
    path = out_dir / raw_result_path(TOPIC, host, today).name
    try:
        write_envelope(run.envelope, path)
    except ResultCollisionError as exc:
        _say(f"run: {exc}")
        return EXIT_IO
    except OSError as exc:
        _say(f"run: cannot write {path}: {exc}")
        return EXIT_IO
    print_summary(run.envelope)
    _say(f"written: {path}")
    return EXIT_OK if run.complete else EXIT_INCOMPLETE


# ---------------------------------------------------------------------- report
def _report_day(dirs: Sequence[Path], explicit: date | None) -> date:
    if explicit is not None:
        return explicit
    for d in dirs:
        match = _DAY_DIR_RE.match(d.name)
        if match:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(match.group(1))
    return datetime.now(UTC).date()


def cmd_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    dirs: list[Path] = args.results
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        parser.error(f"--results folder not found: {', '.join(missing)}")
    envelopes = []
    skipped: list[str] = []
    for directory in dirs:
        docs, bad = load_dir(directory)
        envelopes += [e for e in docs if e.get("spike") == SPIKE]
        skipped += bad
    for reason in skipped:
        _say(f"skipped invalid result file: {reason}")
    if not envelopes:
        _say("warning: no S6 result files found; every cell will be TBD")
    day = _report_day(dirs, args.date).isoformat()
    extra = render_environments(envelopes, args.labels)
    if skipped:
        extra += (
            "\nSkipped result files:\n" + "\n".join(f"- {s}" for s in skipped) + "\n"
        )
    try:
        template = args.template.read_text(encoding="utf-8")
        text = render_report(
            template,
            envelopes,
            topic=TOPIC,
            title="S6 execution environments",
            day=day,
            task="0.2.10",
            spike=SPIKE,
            extra_results=extra,
        )
    except (OSError, TemplateError) as exc:
        _say(f"report: cannot build the report: {exc}")
        return EXIT_IO
    try:
        written = write_report(args.out, text, force=args.force)
    except ReportMergeError as exc:
        _say(f"report: {exc}")
        return EXIT_IO
    except OSError as exc:
        _say(f"report: cannot write {args.out}: {exc}")
        return EXIT_IO
    _say(f"report written: {args.out} ({len(envelopes)} result files)")
    for line in written.messages():
        _say(line)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)  # exits with 2 on usage errors
    try:
        if args.command == "run":
            return cmd_run(args, parser)
        return cmd_report(args, parser)
    except KeyboardInterrupt:
        _say("interrupted (Ctrl-C)")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
