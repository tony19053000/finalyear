"""Run (or resume) a V1.0 evaluation campaign on a real fabric (D417/D419).

    uv run python scripts/eval/run_campaign.py --spec CAMPAIGN.yaml --raw DIR \\
        [--profile-hook "sudo lab/eval_profile.sh {profile} {env}"] \\
        [--origin NODE_ID] [--no-seed] [--poll-s 1] [--max-runs N] [--plan]

Talks to the controller exactly like ``compute`` does (the same settings,
login and fabric CA: ``compute login`` first). Run it on the **origin**
laptop - the one whose agent is the Local candidate (B0 pins jobs to it);
its node id is read from this machine's agent unless ``--origin`` is given.

``--profile-hook`` is needed when the arms need more than one controller
profile (``evalkit.arms``): ``{profile}`` is replaced by the profile name,
``{toml}`` / ``{env}`` by the path of its ``[controller.planner]`` TOML
drop-in / systemd environment file (both written to ``DIR/profiles``). The
command must make the controller run with it and exit 0; the runner then
waits until every node is back ONLINE. On the installed Linux controller
``lab/eval_profile.sh`` does that (a systemd drop-in + restart; ``sudo
lab/eval_profile.sh --clear`` removes it after the campaign).

``--plan`` prints the schedule (block order) and exits without contacting the
controller. Interrupt with Ctrl-C at any time; the same command resumes.

Exit codes: 0 every planned run recorded; 1 runs remain (``--max-runs``);
2 bad arguments or campaign file; 3 the campaign stopped (profile hook,
fabric not ready, raw folder of another campaign).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit.build import build_info
from evalkit.fabric import CommandSwitcher, HttpFabric
from evalkit.raw import RawError
from evalkit.runner import Options, RunnerError, run_campaign
from evalkit.schedule import schedule
from evalkit.spec import SpecError, load_spec

REPO = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_campaign.py", description=__doc__.split("\n")[0]
    )
    p.add_argument("--spec", type=Path, required=True, help="the campaign YAML file")
    p.add_argument("--raw", type=Path, required=True, help="the raw data folder")
    p.add_argument(
        "--profile-hook", default=None, help="command; {profile} {toml} {env}"
    )
    p.add_argument("--origin", default=None, help="the Local laptop's node id")
    p.add_argument("--no-seed", action="store_true", help="never seed the local cache")
    p.add_argument("--poll-s", type=float, default=1.0)
    p.add_argument("--ready-timeout-s", type=float, default=600.0)
    p.add_argument("--max-runs", type=int, default=None)
    p.add_argument("--plan", action="store_true", help="print the schedule only")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = load_spec(args.spec.read_text("utf-8"))
    except (OSError, SpecError) as exc:
        print(f"campaign file: {exc}", file=sys.stderr)
        return 2
    if args.plan:
        for run in schedule(spec):
            print(f"{run.order:5d} b{run.block:03d} {run.profile:13s} {run.run_id}")
        return 0
    from meshforge.cli import shared
    from meshforge.cli.job_commands import _own_node_id, _seed

    settings = shared._settings()
    origin = (
        args.origin or spec.origin or _own_node_id(settings.data_dir, required=False)
    )
    if origin is None:
        print(
            "no origin node: run on a laptop with an enrolled agent, or give "
            "--origin / origin: in the campaign file",
            file=sys.stderr,
        )
        return 2
    client = shared._client(settings)
    seed = None if args.no_seed else (lambda built: _seed(settings, built))
    fabric = HttpFabric(client, seed=seed)
    options = Options(
        raw_dir=args.raw,
        origin_node_id=origin,
        build={**build_info(REPO), "seeded": seed is not None},
        switcher=None
        if args.profile_hook is None
        else CommandSwitcher(args.profile_hook),
        poll_s=args.poll_s,
        ready_timeout_s=args.ready_timeout_s,
        max_runs=args.max_runs,
    )
    try:
        summary = run_campaign(fabric, spec, options)
    except (RunnerError, RawError) as exc:
        print(f"campaign stopped: {exc}", file=sys.stderr)
        return 3
    finally:
        client.close()
    print(
        f"recorded {summary['executed']} run(s) this session "
        f"{summary['statuses']}; {summary['remaining']} of {summary['planned']} remain"
    )
    return 0 if summary["remaining"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
