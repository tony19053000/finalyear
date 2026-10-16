"""Typer application behind the ``compute`` console script (roadmap 0.3.12).

``compute`` is an operator's read-only window on the fabric. It talks **only** to
the controller's REST surface (``{[cli] controller_url}/v1``, D15) - never to
PostgreSQL, never over gRPC - which is what keeps ``meshforge.cli`` importing
nothing but ``common`` (and later ``sdk``).

Shape of the commands::

    compute nodes                  list every node (Rich table)
    compute nodes --json           the API payload + schema_version, one line (docs/CLI.md)
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

Failure behaviour: every expected failure prints one line on stderr (what failed,
why, what to do) and exits with a code from :mod:`meshforge.cli.exits` - the one
mapping (V0.8 G6, D178): 3 auth, 4 bad configuration, 6 controller unreachable
(D18), 8 not found, 1 any other refusal. No command ever shows a traceback; an
unexpected exception (a bug) is one line too, unless ``COMPUTE_DEBUG=1``.

All human output goes through :mod:`meshforge.cli.render` (``NO_COLOR``, TTY
detection, the control-character gate, ``--json`` with ``schema_version``).
Shell completion: ``compute --install-completion`` / ``--show-completion``
(Typer's, for bash, zsh, fish and PowerShell).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from meshforge import __version__
from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.completion import use_safe_installers
from meshforge.cli.exits import CliExit

PROG_NAME = render.PROG_NAME

# `--install-completion` for PowerShell must not change the execution policy
# (meshforge.cli.completion explains why this patches Typer's installer table).
use_safe_installers()

app = typer.Typer(
    name=PROG_NAME,
    help="MeshForge compute fabric command-line tool.",
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


def _version_callback(value: bool) -> None:
    if value:
        render.line(f"{PROG_NAME} {__version__}")
        raise typer.Exit(CliExit.OK)


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
                "File holding a bearer token to send instead of the `compute "
                "login` one (overrides [cli] token and MESHFORGE_CLI__TOKEN). "
                "Never pass a token itself on the command line."
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
    cli._TOKEN_FILE.set(token_file)
    cli._CA_FILE.set(ca)


@app.command()
def nodes(
    node_id: Annotated[
        str | None,
        typer.Argument(
            metavar="[NODE_ID]",
            help="Show this node's detail instead of the list.",
        ),
    ] = None,
    json_output: cli.JsonOption = False,
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

    checked = cli._checked_node_id(node_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        if checked is not None:
            payload = cli._fetch(lambda: client.node(checked))
            if json_output:
                cli._echo_json(payload)
            else:
                render.show(node_detail_table(payload))
                render.show(scores_table(payload))
            return
        if watch and not json_output:
            _watch(
                lambda: nodes_table(cli._fetch(client.nodes)),
                interval_s=settings.cli.watch_interval_s,
            )
            return
        payload = cli._fetch(client.nodes)
        if json_output:
            cli._echo_json(payload)
        else:
            render.show(nodes_table(payload))


@app.command()
def status(json_output: cli.JsonOption = False) -> None:
    """Show where the controller is, whether it answers, and what it reports.

    Unlike the other commands this one **reports** unreachability instead of
    failing on it - "is the fabric up?" is the question it exists to answer - but
    it still exits non-zero (UNAVAILABLE) so a script can branch on it.
    """
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.render import status_table

    settings = cli._settings()
    url = settings.cli.controller_url
    health: Mapping[str, Any] = {}
    node_payload: Mapping[str, Any] | None = None
    detail: str | None = None
    code = CliExit.OK
    with cli._client(settings) as client:
        try:
            health = client.health()
            node_payload = client.nodes()
        except ControllerError as exc:
            detail = render.scrub(exc.message)  # T-1: server text, scrubbed
            code = exit_code_for(exc)
    # V0.8 G6: a controller that answered /healthz but refused the node list
    # (not logged in: exit 3) *is* reachable - only exit 6 means it is not.
    reachable = code is not CliExit.UNAVAILABLE

    if json_output:
        cli._echo_json(
            {
                "controller_url": url,
                "reachable": reachable,
                "detail": detail,
                "live_sessions": health.get("sessions"),
                "nodes": node_payload,
            }
        )
    else:
        render.show(
            status_table(
                controller_url=url,
                reachable=reachable,
                detail=detail,
                nodes=node_payload,
                sessions=health.get("sessions"),
            )
        )
    if code is not CliExit.OK:
        raise typer.Exit(code)


def _watch(render: Callable[[], Any], *, interval_s: float) -> None:
    """Re-render on a bounded interval until Ctrl-C, which exits 0.

    Deliberately a clear-and-print loop rather than ``rich.live``: the output is
    one small table, and a full-screen alternate buffer is what makes a watch
    command leave a terminal in a strange state when it is killed.
    """
    console = cli._out()
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


DEBUG_ENV = "COMPUTE_DEBUG"
"""``COMPUTE_DEBUG=1`` shows the traceback of an unexpected exception (a bug)."""


def main() -> None:
    """Console-script entry point.

    Every expected failure already exits through :func:`meshforge.cli.shared._die`. Anything else
    escaping a command is a bug: it is reported as one line naming the
    exception type (not its text - it may hold a path or a value the user did
    not mean to share) and exits ``FAILURE``; ``COMPUTE_DEBUG=1`` re-raises it
    for the full traceback. Typer's own pretty traceback is off: with locals it
    would print a token held in a local variable.
    """
    try:
        app(prog_name=PROG_NAME)
    except Exception as exc:
        if os.environ.get(DEBUG_ENV, "") not in ("", "0"):
            raise
        render.fail(
            f"internal error ({type(exc).__name__}) - this is a bug in compute "
            f"{__version__}; re-run with {DEBUG_ENV}=1 to see the traceback and "
            "report it"
        )
        sys.exit(CliExit.FAILURE)


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

# V0.7 G4: `compute login|logout|whoami|tokens` (REST users, D141).
from meshforge.cli.auth_commands import login, logout, tokens_app, whoami  # noqa: E402

app.command()(login)
app.command()(logout)
app.command()(whoami)
app.add_typer(tokens_app)

# V0.7 G6: `compute secret set|list|delete` (job secrets, D144).
from meshforge.cli.secret_commands import secret_app  # noqa: E402

app.add_typer(secret_app)

# V0.5 G5: `compute data ...` (named inputs).
from meshforge.cli.data_commands import data_app  # noqa: E402
from meshforge.cli.fabric_commands import fabric_app  # noqa: E402
from meshforge.cli.node_commands import node_app  # noqa: E402

app.add_typer(data_app)
app.add_typer(node_app)
# V0.7 G2: `compute fabric invite|invites|join` (join codes, D135/D151).
app.add_typer(fabric_app)
