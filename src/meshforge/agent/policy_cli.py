"""``meshforge-agent policy show|set|reset`` - the owner's live policy (V0.7 G5, D142).

Local only: it reads the configuration's ``[agent.policy]`` and writes
``<data_dir>/policy.toml``, which the running agent applies within
``reload_interval_s`` and reports to the controller (:mod:`meshforge.agent.policy`).
The new effective policy is validated **before** anything is written, so a bad
value never reaches the agent; the file is replaced atomically and owner-only.

Examples::

    meshforge-agent policy show
    meshforge-agent policy set accept_jobs=true max_ram_bytes=8GiB max_cpu_percent=50
    meshforge-agent policy set allowed_users=alice,bob allow_network=false
    meshforge-agent policy set max_ram_bytes=none      # remove a limit
    meshforge-agent policy reset                       # back to the configuration
    meshforge-agent bench pause                        # = policy set pause_benchmarks=true
    meshforge-agent bench resume

``bench pause|resume`` (V0.9 G5, D219/D246) is the owner's switch for
**benchmark** jobs only: while paused none starts here and a running one is
stopped (``owner_preempted``; the campaign requeues the run). User jobs are not
affected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from meshforge.agent.settings import AgentSettings

__all__ = ["bench_app", "policy_app"]

policy_app = typer.Typer(
    name="policy",
    help="Show or change what this laptop's owner allows (live, no restart).",
    add_completion=False,
    no_args_is_help=True,
)

bench_app = typer.Typer(
    name="bench",
    help="Pause or resume benchmark jobs on this laptop (user jobs unaffected).",
    add_completion=False,
    no_args_is_help=True,
)

_ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="TOML settings file (default: $MESHFORGE_CONFIG or <config dir>/config.toml).",
        exists=False,
        dir_okay=False,
    ),
]
_PROG = "meshforge-agent"


def _settings(config: Path | None) -> AgentSettings:
    from meshforge.agent.main import _LOCAL_ONLY, _load_settings

    return _load_settings(config, **_LOCAL_ONLY)


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(f"{_PROG}: {message}", err=True)
    return typer.Exit(code)


def _view(settings: AgentSettings) -> dict[str, Any]:
    from meshforge.agent.policy import policy_view

    return policy_view(settings)


@policy_app.command("show")
def show(
    config: _ConfigOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Print the effective policy (configuration + live changes)."""
    view = _view(_settings(config))
    if as_json:
        typer.echo(json.dumps(view, sort_keys=True))
    else:
        if view["invalid"]:
            typer.echo(
                f"INVALID - this node accepts nothing until fixed: {view['invalid']}"
            )
        for key, value in view["policy"].items():
            source = "live" if key in view["live_keys"] else "config"
            shown = "no limit" if value is None else value
            if key == "allowed_users" and not value:
                shown = "everyone"
            typer.echo(f"{key}: {shown}  ({source})")
        for key in ("max_cpu_jobs", "max_gpu_jobs"):
            source = "live" if key in view["live_keys"] else "config"
            value = view[key]
            shown = "one per GPU" if value is None and key == "max_gpu_jobs" else value
            typer.echo(f"{key}: {shown}  ({source})")
        for key in view["deprecated_keys"]:
            typer.echo(f"note: [agent.{key}] is deprecated; use [agent.policy]")
    raise typer.Exit(ExitCode.CONFIG_ERROR if view["invalid"] else ExitCode.OK)


@policy_app.command("set")
def set_(
    assignments: Annotated[
        list[str], typer.Argument(help="KEY=VALUE ... (VALUE none removes a limit).")
    ],
    config: _ConfigOption = None,
) -> None:
    """Change the policy live; the running agent applies it within seconds."""
    _apply(config, assignments)


def _apply(config: Path | None, assignments: list[str]) -> None:
    from meshforge.agent.policy import RESTART_KEYS, apply_changes, parse_assignments
    from meshforge.common.errors import MeshForgeError

    settings = _settings(config)
    try:
        changes = parse_assignments(assignments)
        result = apply_changes(settings, changes)
    except MeshForgeError as exc:
        raise _fail(exc.message, ExitCode.USAGE_ERROR) from None
    except OSError as exc:
        raise _fail(
            f"cannot write {exc.filename or 'the live policy file'}: "
            f"{exc.strerror or exc}",
            ExitCode.CONFIG_ERROR,
        ) from None
    typer.echo(
        f"policy saved to {result.path}; a running agent applies it within "
        f"{settings.agent.policy.reload_interval_s:g} s"
    )
    for key in result.restart_keys:
        # turned on now while it was off: a running agent may have started with
        # it off and then never probed these runtimes (fix r1, T-9)
        if changes.get(key) is True:
            typer.echo(
                f"note: if the agent was started with {key} = false it has no "
                f"{RESTART_KEYS[key]} yet - restart it once (the restart reads this "
                "policy)"
            )
    raise typer.Exit(ExitCode.OK)


@bench_app.command("pause")
def bench_pause(config: _ConfigOption = None) -> None:
    """Pause benchmark jobs here: none starts, a running one is stopped."""
    _apply(config, ["pause_benchmarks=true"])


@bench_app.command("resume")
def bench_resume(config: _ConfigOption = None) -> None:
    """Let benchmark jobs run here again."""
    _apply(config, ["pause_benchmarks=false"])


@policy_app.command("reset")
def reset(config: _ConfigOption = None) -> None:
    """Drop every live change (back to the configuration's [agent.policy])."""
    from meshforge.agent.policy import clear_overlay, policy_path

    settings = _settings(config)
    path = policy_path(settings)
    removed = clear_overlay(path)
    typer.echo("live policy removed" if removed else "no live policy to remove")
    raise typer.Exit(ExitCode.OK)
