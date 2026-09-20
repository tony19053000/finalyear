"""Process entry point for ``meshforge-agent`` (roadmap 0.3.8 E, ADR-0013).

Commands::

    meshforge-agent run [--config FILE] [--controller HOST:PORT]
    meshforge-agent identity show [--config FILE] [--json]
    meshforge-agent identity reset --yes [--config FILE]

Everything beyond ``--version``/``--help`` is imported inside the command so the
module runs on a base install (no agent extra). ``run`` itself needs only core
dependencies when ``controller_address`` is configured; mDNS discovery needs the
``agent`` extra (``zeroconf``) and says so if it is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from meshforge import __version__
from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from meshforge.agent.settings import AgentSettings

PROG_NAME = "meshforge-agent"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge node agent process.",
    add_completion=False,
    no_args_is_help=True,
)
identity_app = typer.Typer(
    name="identity",
    help="Inspect or reset this node's identity (node_id + keypair, ADR-0013).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(identity_app, name="identity")

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="TOML settings file (default: $MESHFORGE_CONFIG or <config dir>/config.toml).",
        exists=False,
        dir_okay=False,
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{PROG_NAME} {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """MeshForge node agent process."""


def _load_settings(config: Path | None, **overrides: object) -> AgentSettings:
    from meshforge.agent.settings import AgentSettings
    from meshforge.common.errors import MeshForgeError

    try:
        return AgentSettings.load(config, **overrides)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None


def _quiet_logging(settings: AgentSettings) -> None:
    """For the ``identity`` commands: stdout is the result, logs go to stderr and
    only warnings and worse (an unconfigured structlog would print to stdout)."""
    from meshforge.common.logging import configure_logging

    configure_logging("WARNING", fmt=settings.log.format, component="agent")


@app.command()
def run(
    config: ConfigOption = None,
    controller: Annotated[
        str | None,
        typer.Option(
            "--controller",
            help="Controller host:port (same as agent.controller_address); "
            "omit to discover it via mDNS.",
        ),
    ] = None,
) -> None:
    """Start the node agent: connect to the controller and stay connected."""
    from meshforge.common.logging import configure_logging

    overrides: dict[str, object] = {}
    if controller is not None:
        overrides["agent"] = {"controller_address": controller}
    settings = _load_settings(config, **overrides)
    configure_logging(settings.log.level, fmt=settings.log.format, component="agent")

    from meshforge.agent.runtime import run_agent

    raise typer.Exit(run_agent(settings))


@identity_app.command("show")
def identity_show(
    config: ConfigOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Print node_id, session_epoch and the key fingerprint (never the key)."""
    from meshforge.agent.identity import load_or_create
    from meshforge.common.errors import MeshForgeError

    settings = _load_settings(config)
    _quiet_logging(settings)
    try:
        identity = load_or_create(settings.identity_dir)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    facts = {
        "node_id": str(identity.node_id),
        "session_epoch": identity.session_epoch,
        "key_fingerprint_sha256": identity.fingerprint,
        "path": str(identity.path),
    }
    if as_json:
        typer.echo(json.dumps(facts, indent=2, sort_keys=True))
    else:
        for key, value in facts.items():
            typer.echo(f"{key}: {value}")


@identity_app.command("reset")
def identity_reset(
    config: ConfigOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm: the node gets a NEW node_id; the controller forgets the old one.",
        ),
    ] = False,
) -> None:
    """Delete the identity files; the next `run` mints a fresh node_id."""
    from meshforge.agent.identity import reset
    from meshforge.common.errors import MeshForgeError

    settings = _load_settings(config)
    _quiet_logging(settings)
    if not yes:
        typer.echo(
            f"{PROG_NAME}: refusing to reset {settings.identity_dir} without --yes "
            "(this node would get a new node_id)",
            err=True,
        )
        raise typer.Exit(ExitCode.USAGE_ERROR)
    try:
        reset(settings.identity_dir)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    typer.echo(f"identity reset: {settings.identity_dir}")


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
