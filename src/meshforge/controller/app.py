"""Process entry point for ``meshforge-controller``.

``meshforge-controller [--config FILE] [--allow-any-bind]`` starts the one-process
controller (gRPC + REST, ARCHITECTURE §3; roadmap 0.3.6) - the bare form every
service unit and test harness already uses, kept as the *default* action.

``meshforge-controller token show|rotate [--config FILE]`` manages the fabric
token (D44, ADR-0014): ``show`` prints it (creating it on first use), ``rotate``
replaces it; a running controller picks the new one up within a second. The token
goes to **stdout only** - logs go to stderr at WARNING, so nothing that captures
logs ever sees it.

Everything that needs the ``controller`` extra is imported inside the commands so
``--version`` keeps working on a base install.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from meshforge import __version__
from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from meshforge.controller.settings import ControllerSettings

PROG_NAME = "meshforge-controller"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge controller (control plane) process.",
    add_completion=False,
)
token_app = typer.Typer(
    name="token",
    help="Show or rotate the fabric token that protects job and blob calls (D44).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(token_app)

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


def _load_settings(config: Path | None, **overrides: object) -> "ControllerSettings":
    from meshforge.common.errors import MeshForgeError
    from meshforge.controller.settings import ControllerSettings

    try:
        return ControllerSettings.load(config, **overrides)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None


@app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    config: ConfigOption = None,
    allow_any_bind: Annotated[
        bool,
        typer.Option(
            "--allow-any-bind",
            help="Permit a wildcard/public bind address (same as controller.allow_any_bind).",
        ),
    ] = False,
) -> None:
    """Start the controller: migrate the database, serve agents (gRPC) and REST."""
    if ctx.invoked_subcommand is not None:
        return
    from meshforge.common.logging import configure_logging

    overrides = {"controller": {"allow_any_bind": True}} if allow_any_bind else {}
    settings = _load_settings(config, **overrides)
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


def _token_command(config: Path | None, *, rotate: bool) -> None:
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging
    from meshforge.controller.fabric_token import load_or_create_token, rotate_token

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    try:
        if rotate:
            token = rotate_token(settings.token_path)
        else:
            token, _created = load_or_create_token(settings.token_path)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    typer.echo(token.get_secret_value())


@token_app.command("show")
def token_show(config: ConfigOption = None) -> None:
    """Print the fabric token (creating it if this controller has none yet).

    Give it to each laptop that submits jobs: `[cli] token` in its config.toml,
    MESHFORGE_CLI__TOKEN, or a file passed with `compute --token-file`.
    """
    _token_command(config, rotate=False)


@token_app.command("rotate")
def token_rotate(config: ConfigOption = None) -> None:
    """Replace the fabric token and print the new one; the old one stops working."""
    _token_command(config, rotate=True)


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
