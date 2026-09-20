"""Process entry point for ``meshforge-controller``.

``run`` starts the one-process controller (gRPC + REST, ARCHITECTURE §3; roadmap
0.3.6). Everything that needs the ``controller`` extra is imported inside ``run`` so
``--version`` keeps working on a base install.
"""

from pathlib import Path
from typing import Annotated

import typer

from meshforge import __version__
from meshforge.common.exit_codes import ExitCode

PROG_NAME = "meshforge-controller"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge controller (control plane) process.",
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{PROG_NAME} {__version__}")
        raise typer.Exit(ExitCode.OK)


@app.command()
def run(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="TOML settings file (default: $MESHFORGE_CONFIG or <config dir>/config.toml).",
            exists=False,
            dir_okay=False,
        ),
    ] = None,
    allow_any_bind: Annotated[
        bool,
        typer.Option(
            "--allow-any-bind",
            help="Permit a wildcard/public bind address (same as controller.allow_any_bind).",
        ),
    ] = False,
) -> None:
    """Start the controller: migrate the database, serve agents (gRPC) and REST."""
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    try:
        from meshforge.controller.settings import ControllerSettings

        overrides = {"controller": {"allow_any_bind": True}} if allow_any_bind else {}
        settings = ControllerSettings.load(config, **overrides)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    configure_logging(
        settings.log.level, fmt=settings.log.format, component="controller"
    )

    try:
        from meshforge.controller.server import run_controller
    except ImportError as exc:  # controller extra not installed
        typer.echo(
            f"{PROG_NAME}: the controller extra is not installed "
            f"(uv sync --extra controller): {exc}",
            err=True,
        )
        raise typer.Exit(ExitCode.CONFIG_ERROR) from None
    raise typer.Exit(run_controller(settings))


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
