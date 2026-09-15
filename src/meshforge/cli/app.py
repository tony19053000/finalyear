"""Typer application behind the ``compute`` console script."""

from typing import Annotated

import typer

from meshforge import __version__
from meshforge.common.exit_codes import ExitCode

PROG_NAME = "compute"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge compute fabric command-line tool.",
    add_completion=False,
    no_args_is_help=True,
)


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
    """MeshForge compute fabric command-line tool."""


@app.command()
def status() -> None:
    """Show fabric status (not implemented yet)."""
    typer.echo(
        f"{PROG_NAME} status: not implemented yet - the fabric (controller + agents) "
        "arrives in V0.3 and the full status view in V0.8.",
        err=True,
    )
    raise typer.Exit(ExitCode.NOT_IMPLEMENTED)


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
