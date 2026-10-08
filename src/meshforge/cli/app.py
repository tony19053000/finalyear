"""Typer application behind the ``compute`` console script (roadmap 0.3.12).

``compute`` is an operator's read-only window on the fabric. It talks **only** to
the controller's REST surface (``{[cli] controller_url}/v1``, D15) - never to
PostgreSQL, never over gRPC - which is what keeps ``meshforge.cli`` importing
nothing but ``common`` (and later ``sdk``).

Shape of the commands::

    compute nodes                  list every node (Rich table)
    compute nodes --json           the raw API payload, one line, for scripting
    compute nodes --watch          re-render every [cli] watch_interval_s; Ctrl-C = 0
    compute nodes <node_id>        one node's detail
    compute status                 where the controller is, whether it answers

``nodes`` takes an **optional argument** rather than being a sub-typer: an
operator types ``compute nodes`` far more often than any subcommand, and with a
sub-typer that bare form would have to be a no-argument default subcommand
(``nodes list``), which Typer expresses only through a callback with
``invoke_without_command`` - more machinery, and ``compute nodes <id>`` would then
be ambiguous with a subcommand name. One command with an optional argument keeps
both forms unambiguous: no argument lists, an argument shows that node.

``meshforge.cli.client`` and ``meshforge.cli.render`` are imported **inside** the
commands, not at module scope: importing the client imports ``httpx``, which costs
roughly 150 ms, and ``compute --version`` (ADR-0010's base-install check) needs
neither. Every command that talks to the controller imports them on its first line.

Failure behaviour: an unreachable or timed-out controller prints one typed line on
stderr and exits :attr:`~meshforge.common.exit_codes.ExitCode.UNAVAILABLE` (D18);
a refused request exits ``FAILURE``; bad configuration exits ``CONFIG_ERROR``. No
command ever shows a traceback.
"""

from __future__ import annotations

import json
import ssl
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from rich.console import Console

from meshforge import __version__
from meshforge.cli.settings import CliSettings
from meshforge.common.errors import ConfigError, ExitCode

if TYPE_CHECKING:  # `meshforge.cli.client` pulls in httpx - see `_client` below.
    from meshforge.cli.client import ControllerClient

PROG_NAME = "compute"

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge compute fabric command-line tool.",
    add_completion=False,
    no_args_is_help=True,
)

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Print the controller's raw JSON payload on one line (for scripting).",
    ),
]


def _out() -> Console:
    """stdout console. Rich turns colour off by itself when stdout is not a TTY."""
    return Console(highlight=False, emoji=False, markup=False)


def _echo_json(payload: Mapping[str, Any]) -> None:
    """One unindented JSON object on one line, straight to stdout.

    Not via Rich: a console would wrap and style it, and this output is meant to
    be piped into ``jq``.
    """
    typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=False))


def _die(message: str, code: ExitCode) -> NoReturn:
    """One sanitised line on stderr, then exit. Never a traceback.

    Deliberately ``typer.echo`` and not a Rich console: a console would wrap the
    line at the terminal width (turning a single error into several lines a
    ``grep`` would miss) and would interpret square brackets as markup. The
    message is already one short printable line - :mod:`meshforge.cli.client`
    flattens and truncates anything the server supplied.
    """
    typer.echo(f"{PROG_NAME}: {message}", err=True)
    raise typer.Exit(code)


def _settings() -> CliSettings:
    try:
        return CliSettings.load()
    except ConfigError as exc:
        _die(exc.message, ExitCode.CONFIG_ERROR)


_TOKEN_FILE: ContextVar[Path | None] = ContextVar("compute_token_file", default=None)
"""``compute --token-file PATH`` for this invocation. Set by the root callback
before any subcommand runs (a ContextVar rather than Typer's ``ctx.obj`` so that
``_client(settings)`` keeps the one-argument signature the tests patch)."""


_CA_FILE: ContextVar[Path | None] = ContextVar("compute_ca_file", default=None)
"""``compute --ca PATH`` (V0.7 G3): the fabric CA certificate to trust."""


def _token_file_option() -> Path | None:
    return _TOKEN_FILE.get()


def _client(settings: CliSettings) -> ControllerClient:
    from meshforge.cli.client import ControllerClient
    from meshforge.cli.settings import resolve_token

    try:
        token = resolve_token(settings, _token_file_option())
    except ConfigError as exc:
        _die(exc.message, ExitCode.CONFIG_ERROR)
    return ControllerClient(
        settings.cli.controller_url,
        timeout_s=settings.cli.timeout_s,
        token=token,
        ssl_context=_ssl_context(settings),
    )


def _ssl_context(settings: CliSettings) -> ssl.SSLContext | None:
    """V0.7 G3 (D139): an ``https`` controller is verified against the fabric
    CA (``--ca`` / ``[cli] ca_file`` / found locally) and must be the
    controller (``meshforge-controller`` name), whatever address was typed."""
    from meshforge.cli.settings import resolve_ca
    from meshforge.common.fabric_tls import client_context

    if not settings.cli.controller_url.startswith("https://"):
        return None
    try:
        return client_context(resolve_ca(settings, _CA_FILE.get()))
    except ConfigError as exc:
        _die(exc.message, ExitCode.CONFIG_ERROR)


def _fetch[T](call: Callable[[], T]) -> T:
    """Run one request, turning every typed client failure into an exit."""
    from meshforge.cli.client import ControllerError, exit_code_for

    try:
        return call()
    except ControllerError as exc:
        _die(exc.message, exit_code_for(exc))


def _checked_node_id(value: str | None) -> str | None:
    """A NODE_ID argument is validated here, before any request is built.

    Without this, ``compute nodes ../healthz`` becomes ``GET /v1/healthz`` (httpx
    collapses the dot segments) and renders a row of dashes, and
    ``compute nodes 'x?limit=9'`` silently sends a query string. Both are usage
    errors, so they exit ``USAGE_ERROR`` rather than reaching the controller. The
    offending value is not echoed: it is argv, and argv reaches a terminal.
    """
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import validate_node_id

    if value is None:
        return None
    try:
        return str(validate_node_id(value))
    except InvalidIdError:
        _die(
            "NODE_ID is not a canonical node_<ULID>; run `compute nodes` to list them",
            ExitCode.USAGE_ERROR,
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
    token_file: Annotated[
        Path | None,
        typer.Option(
            "--token-file",
            help=(
                "File holding the fabric token for job/blob calls (overrides "
                "[cli] token and MESHFORGE_CLI__TOKEN). Never pass the token itself "
                "on the command line."
            ),
            dir_okay=False,
        ),
    ] = None,
    ca: Annotated[
        Path | None,
        typer.Option(
            "--ca",
            help=(
                "The fabric CA certificate (ca.crt) to verify the controller with "
                "(overrides [cli] ca_file; found automatically on a laptop with "
                "an enrolled agent or the controller)."
            ),
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """MeshForge compute fabric command-line tool."""
    _TOKEN_FILE.set(token_file)
    _CA_FILE.set(ca)


@app.command()
def nodes(
    node_id: Annotated[
        str | None,
        typer.Argument(
            metavar="[NODE_ID]",
            help="Show this node's detail instead of the list.",
        ),
    ] = None,
    json_output: JsonOption = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help=(
                "Re-render every cli.watch_interval_s seconds until Ctrl-C "
                "(exits 0). Ignored with --json."
            ),
        ),
    ] = False,
) -> None:
    """List the fabric's nodes, or show one node.

    Three liveness fields are shown on purpose and can legitimately disagree
    (D16): STATE is the database's view (offline at once after a clean agent
    stop, suspect after a dropped connection until the lease runs out), CONN is
    whether this controller process holds the node's stream right now, and AGE
    is how stale the newest resource sample is.
    """
    from meshforge.cli.render import node_detail_table, nodes_table, scores_table

    checked = _checked_node_id(node_id)
    settings = _settings()
    with _client(settings) as client:
        if checked is not None:
            payload = _fetch(lambda: client.node(checked))
            if json_output:
                _echo_json(payload)
            else:
                _out().print(node_detail_table(payload))
                _out().print(scores_table(payload))
            return
        if watch and not json_output:
            _watch(
                lambda: nodes_table(_fetch(client.nodes)),
                interval_s=settings.cli.watch_interval_s,
            )
            return
        payload = _fetch(client.nodes)
        if json_output:
            _echo_json(payload)
        else:
            _out().print(nodes_table(payload))


@app.command()
def status(json_output: JsonOption = False) -> None:
    """Show where the controller is, whether it answers, and what it reports.

    Unlike the other commands this one **reports** unreachability instead of
    failing on it - "is the fabric up?" is the question it exists to answer - but
    it still exits non-zero (UNAVAILABLE) so a script can branch on it.
    """
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.render import status_table

    settings = _settings()
    url = settings.cli.controller_url
    with _client(settings) as client:
        try:
            health = client.health()
            node_payload: Mapping[str, Any] | None = client.nodes()
            detail: str | None = None
            reachable = True
            code = ExitCode.OK
        except ControllerError as exc:
            health, node_payload, detail = {}, None, exc.message
            reachable = False
            code = exit_code_for(exc)

    if json_output:
        _echo_json(
            {
                "controller_url": url,
                "reachable": reachable,
                "detail": detail,
                "live_sessions": health.get("sessions"),
                "nodes": node_payload,
            }
        )
    else:
        _out().print(
            status_table(
                controller_url=url,
                reachable=reachable,
                detail=detail,
                nodes=node_payload,
                sessions=health.get("sessions"),
            )
        )
    if code is not ExitCode.OK:
        raise typer.Exit(code)


def _watch(render: Callable[[], Any], *, interval_s: float) -> None:
    """Re-render on a bounded interval until Ctrl-C, which exits 0.

    Deliberately a clear-and-print loop rather than ``rich.live``: the output is
    one small table, and a full-screen alternate buffer is what makes a watch
    command leave a terminal in a strange state when it is killed.
    """
    console = _out()
    try:
        while True:
            table = render()
            console.clear()
            console.print(table)
            console.print(f"(refreshing every {interval_s:g}s - Ctrl-C to stop)")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        # Not an error: stopping a watch is how it ends. Click would otherwise
        # turn this into "Aborted!" and exit 1.
        console.print("")


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)


# The job commands (V0.4 G5); they look the shared helpers above up at call time.
from meshforge.cli.job_commands import cancel, get, jobs, logs, run  # noqa: E402

for _command in (run, jobs, logs, cancel, get):
    app.command()(_command)

# V0.6 G2: `compute explain` (planner decisions).
from meshforge.cli.explain_commands import explain  # noqa: E402

app.command()(explain)

# V0.6 G4: `compute network` (the measured network matrix).
from meshforge.cli.network_commands import network  # noqa: E402

app.command()(network)

# V0.5 G5: `compute data ...` (named inputs).
from meshforge.cli.data_commands import data_app  # noqa: E402
from meshforge.cli.fabric_commands import fabric_app  # noqa: E402
from meshforge.cli.node_commands import node_app  # noqa: E402

app.add_typer(data_app)
app.add_typer(node_app)
# V0.7 G2: `compute fabric invite|invites|join` (join codes, D135/D151).
app.add_typer(fabric_app)
