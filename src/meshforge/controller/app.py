"""Process entry point for ``meshforge-controller``.

Only ``--version`` works in V0.1; the FastAPI + gRPC event loop arrives in V0.3.
Keep this module free of top-level imports from the ``controller`` extra so it
runs on a base install.
"""

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
) -> None:
    """Start the controller."""
    typer.echo(
        f"{PROG_NAME}: the controller is not yet implemented (planned for V0.3).",
        err=True,
    )
    raise typer.Exit(ExitCode.NOT_IMPLEMENTED)


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
