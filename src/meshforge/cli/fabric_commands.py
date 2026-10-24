"""``compute fabric invite|invites|join`` (V0.7 G2, D135, D136, D151).

* ``invite [--ttl 15m] [--replace NODE_ID] [--json]`` - ``POST
  /v1/fabric/invites``: prints the single-use join code on stdout (once; the
  controller keeps only its hash) and its facts on stderr.
* ``invites [--revoke INVITE_ID] [--json]`` - list (never codes) or revoke.
* ``join [CODE] [--force]`` - on a laptop that runs an agent, the same as
  ``meshforge-agent join``: ``compute`` may not import the agent
  (lint-imports), so it runs the installed ``meshforge-agent`` and hands it the
  code on **stdin** (never in its argv, which other local users can read). With
  no agent installed it says so and exits ``CONFIG_ERROR``.

``invite``/``invites`` need an admin login (``compute login``; the V0.7 G4
re-gate of D151 - the legacy fabric token only while the controller accepts it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit
from meshforge.common.errors import MeshForgeError

__all__ = ["agent_command", "fabric_app"]

fabric_app = typer.Typer(
    name="fabric",
    help="Invite laptops into the fabric and join one (V0.7).",
    no_args_is_help=True,
    rich_markup_mode=None,
)

_AGENT = "meshforge-agent"


def _ttl(text: str | None) -> int | None:
    from meshforge.common.join_code import parse_ttl

    if text is None:
        return None
    try:
        return parse_ttl(text)
    except ValueError as exc:
        cli._die(f"--ttl: {exc}", CliExit.USAGE_ERROR)


@fabric_app.command("invite")
def invite(
    ttl: Annotated[
        str | None,
        typer.Option(
            "--ttl", help="How long the code works: 90s, 15m, 2h (1 min - 24 h)."
        ),
    ] = None,
    replace: Annotated[
        str | None,
        typer.Option(
            "--replace",
            metavar="NODE_ID",
            help="Let this code re-key an already enrolled node.",
        ),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Make a single-use join code for one new laptop."""
    from meshforge.common.ids import validate_node_id

    if replace is not None:
        try:
            validate_node_id(replace)
        except MeshForgeError:
            cli._die("--replace needs a node id (node_...)", CliExit.USAGE_ERROR)
    ttl_s = _ttl(ttl)
    settings = cli._settings()
    with cli._client(settings) as client:
        issued = cli._fetch(
            lambda: client.create_invite(ttl_s=ttl_s, replace_node_id=replace)
        )
    if json_output:
        cli._echo_json(issued)
        return
    render.line(
        render.clip(issued.get("code", ""))
    )  # the code itself: capped, not scrubbed
    for label, key in (
        ("invite:    ", "invite_id"),
        ("controller:", "address"),
        ("CA pin:    ", "ca_pin"),
        ("expires:   ", "expires_at"),
        ("replaces:  ", "replace_node_id"),
    ):
        value = issued.get(key)
        if value:
            render.line(f"{label} {_printable(value)}", err=True)
    render.line(
        "On the new laptop: meshforge-agent join   (paste the code when asked)",
        err=True,
    )


def _printable(value: Any) -> str:
    """A server value for one line: flattened by the renderer, capped."""
    return render.flat(value)[:200]


@fabric_app.command("invites")
def invites(
    revoke: Annotated[
        str | None,
        typer.Option("--revoke", metavar="INVITE_ID", help="Revoke this invite."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """List invites (newest first; codes are never shown again) or revoke one."""
    from meshforge.common.ids import validate_invite_id

    if revoke is not None:
        try:
            validate_invite_id(revoke)
        except MeshForgeError:
            cli._die("--revoke needs an invite id (inv_...)", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        if revoke is not None:
            row = cli._fetch(lambda: client.revoke_invite(revoke))
            if json_output:
                cli._echo_json(row)
            else:
                render.line(f"revoked {render.echo(row.get('id'))}")
            return
        payload = cli._fetch(client.invites)
    if json_output:
        cli._echo_json(payload)
        return
    rows = [r for r in payload.get("invites") or [] if isinstance(r, dict)]
    if not rows:
        render.line("no invites")
        return
    table = render.table(
        "INVITE", "STATE", "EXPIRES", "USED BY", "REPLACES", title="invites"
    )
    for row in rows:
        table.add_row(
            render.cell(row.get("id")),
            render.cell(row.get("state")),
            render.cell(row.get("expires_at")),
            render.cell(row.get("used_by_node")),
            render.cell(row.get("replace_node_id")),
        )
    render.show(table)


def agent_command() -> list[str] | None:
    """How to run this laptop's ``meshforge-agent``, or ``None`` if none is
    installed: this bundle's own launcher first (V0.17 SP3 F5 - an installed
    ``compute`` must drive the agent it was shipped with), then ``PATH``, then
    next to this interpreter in its environment."""
    from meshforge.common.buildinfo import bundle_command

    own = bundle_command(_AGENT)
    if own is not None:
        return [str(own)]
    found = shutil.which(_AGENT)
    if found:
        return [found]
    suffix = ".exe" if os.name == "nt" else ""
    beside = Path(sys.executable).with_name(_AGENT + suffix)
    if beside.is_file():
        return [str(beside)]
    return None


@fabric_app.command("join")
def join(
    code: Annotated[
        str | None,
        typer.Argument(
            metavar="[CODE]",
            help="The join code; omit it to be asked (keeps it out of shell "
            "history and the process list).",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace this laptop's existing enrolment."),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help="The agent's TOML settings file.", dir_okay=False
        ),
    ] = None,
) -> None:
    """Join this laptop's agent to a fabric (runs `meshforge-agent join`)."""
    command = agent_command()
    if command is None:
        cli._die(
            "no MeshForge agent is installed on this laptop, so there is nothing "
            "to join: install it (see docs/OPERATIONS.md) and run "
            "`meshforge-agent join` - `compute` itself needs no certificate",
            CliExit.CONFIG_ERROR,
        )
    if code is None:
        code = typer.prompt("join code", hide_input=True)
    argv = [*command, "join", "-"]
    if force:
        argv.append("--force")
    if config is not None:
        argv += ["--config", str(config)]
    result = subprocess.run(  # noqa: S603 - our own agent binary, fixed arguments
        argv, input=code.strip() + "\n", text=True, check=False
    )
    raise typer.Exit(result.returncode)
