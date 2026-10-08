"""Process entry point for ``meshforge-controller``.

``meshforge-controller [--config FILE] [--allow-any-bind]`` starts the one-process
controller (gRPC + REST, ARCHITECTURE §3; roadmap 0.3.6) - the bare form every
service unit and test harness already uses, kept as the *default* action.

``meshforge-controller token show|rotate [--config FILE]`` manages the fabric
token (D44, ADR-0014): ``show`` prints it (creating it on first use), ``rotate``
replaces it; a running controller picks the new one up within a second. The token
goes to **stdout only** - logs go to stderr at WARNING, so nothing that captures
logs ever sees it.

``meshforge-controller fabric create [--force]`` makes the fabric CA and the
controller's server certificate (V0.7 G1, D134); ``fabric show [--json]`` prints
the fabric id, the CA fingerprint (full and the 128-bit join-code form) and the
expiry dates. Both run on the controller host only - they need the CA key file.
Nothing secret is ever printed by them.

``fabric invite [--ttl] [--replace NODE_ID] [--address HOST:PORT] [--json]``
(V0.7 G2, D135) prints one single-use join code - the only secret this tool
prints, on stdout, once; the facts go to stderr. ``fabric invites [--revoke
ID]`` lists or revokes invites (never their codes). Both need the database.

Everything that needs the ``controller`` extra is imported inside the commands so
``--version`` keeps working on a base install.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from meshforge import __version__
from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from datetime import timedelta

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
fabric_app = typer.Typer(
    name="fabric",
    help="Create or show the fabric CA (V0.7): the root of every node's identity.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(fabric_app)

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


_FORCE_WARNING = (
    "WARNING: the fabric CA was replaced. Every node certificate issued by the old "
    "CA is now worthless: every laptop must re-join with a new code, and a running "
    "controller must be restarted."
)


def _fabric_facts(settings: "ControllerSettings") -> dict[str, object]:
    """What `fabric show` prints (raises MeshForgeError when there is no CA)."""
    from meshforge.common import pki
    from meshforge.controller.fabric_ca import (
        FabricCaError,
        load_fabric,
        load_server_cert,
        recover_interrupted,
    )

    recover_interrupted(settings.ca_dir)  # D149c: repair a crash mid-replace
    fabric = load_fabric(settings.ca_dir)
    facts: dict[str, object] = {
        "fabric_id": fabric.fabric_id,
        "ca_fingerprint_sha256": fabric.fingerprint,
        "join_fingerprint": fabric.short_fingerprint,
        "ca_not_before": fabric.cert.not_valid_before_utc.isoformat(),
        "ca_not_after": fabric.cert.not_valid_after_utc.isoformat(),
        "ca_dir": str(settings.ca_dir),
        "server_cert": None,
        "tls_enforced": not settings.security.insecure,
    }
    if settings.tls_dir.exists():
        try:
            server = load_server_cert(settings.tls_dir)
            pki.verify_issued_by(server.cert, fabric.cert)
            sans = pki.cert_sans(server.cert)  # may be malformed (D149b)
        except (FabricCaError, pki.PkiError) as exc:
            facts["server_cert"] = {"problem": exc.message}
        else:
            facts["server_cert"] = {
                "fingerprint_sha256": pki.cert_fingerprint(server.cert),
                "not_after": server.cert.not_valid_after_utc.isoformat(),
                "sans": sans,
                "renewal_due": pki.renewal_due(server.cert),
            }
    return facts


def _print_facts(facts: dict[str, object]) -> None:
    from meshforge.common.pki import format_fingerprint

    typer.echo(f"fabric id:         {facts['fabric_id']}")
    typer.echo(
        f"join fingerprint:  {format_fingerprint(str(facts['join_fingerprint']))}"
    )
    typer.echo(
        f"CA SHA-256:        {format_fingerprint(str(facts['ca_fingerprint_sha256']))}"
    )
    typer.echo(
        f"CA valid:          {facts['ca_not_before']} .. {facts['ca_not_after']}"
    )
    server = facts["server_cert"]
    if not isinstance(server, dict):
        typer.echo(
            "server cert:       not issued yet (issued at the next controller start)"
        )
    elif "problem" in server:
        typer.echo(
            f"server cert:       unusable ({server['problem']}); re-issued at the next "
            "controller start"
        )
    else:
        sans = ", ".join(str(s) for s in server["sans"])
        due = (
            " (renewal due: re-issued at the next start)"
            if server["renewal_due"]
            else ""
        )
        typer.echo(f"server cert:       valid until {server['not_after']}{due}")
        typer.echo(f"server cert SANs:  {sans}")
    typer.echo(
        "not enforced yet:  no listener uses TLS before V0.7 G3; agents cannot join "
        "before V0.7 G2"
    )


@fabric_app.command("create")
def fabric_create(
    config: ConfigOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Replace an existing CA. Every node must then re-join.",
        ),
    ] = False,
) -> None:
    """Create the fabric CA and the controller's server certificate.

    Refuses when a CA exists (exit 5) unless --force. Run it once, on the
    controller host, before pairing any laptop.
    """
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging
    from meshforge.controller.fabric_ca import (
        create_fabric,
        ensure_server_cert,
        fabric_present,
    )

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    replacing = force and fabric_present(settings.ca_dir)
    try:
        fabric = create_fabric(settings.ca_dir, force=force)
        ensure_server_cert(
            settings.tls_dir,
            fabric,
            hosts=settings.server_cert_hosts,
            lifetime=_server_lifetime(settings),
        )
        facts = _fabric_facts(settings)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    if replacing:
        typer.echo(_FORCE_WARNING, err=True)
    _print_facts(facts)


@fabric_app.command("show")
def fabric_show(
    config: ConfigOption = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Print the fabric id, CA fingerprints, validity and the server certificate."""
    import json

    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    try:
        facts = _fabric_facts(settings)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    if json_out:
        typer.echo(json.dumps(facts, indent=2, sort_keys=True))
    else:
        _print_facts(facts)


_INVITE_HINT = (
    "Give this code to the laptop's owner over a channel you trust; it works once, "
    "until {expires}. On that laptop: meshforge-agent join <code>"
)


def _ttl_seconds(text: str | None, default: int) -> int:
    from meshforge.common.join_code import parse_ttl

    if text is None:
        return default
    try:
        return parse_ttl(text)
    except ValueError as exc:
        typer.echo(f"{PROG_NAME}: --ttl: {exc}", err=True)
        raise typer.Exit(ExitCode.USAGE_ERROR) from None


async def _create_invite(
    settings: "ControllerSettings",
    *,
    ttl_s: int,
    replace: str | None,
    address: str | None,
) -> dict[str, object]:
    from meshforge.controller.enrollment.codes import code_address, issue_code
    from meshforge.controller.enrollment.invites import InviteStore, check_ttl
    from meshforge.controller.fabric_ca import load_fabric
    from meshforge.controller.store.engine import create_engine

    check_ttl(ttl_s)
    # Everything that can refuse without side effects first: no CA (D135: an
    # invite needs one), no usable address -> nothing is written.
    fabric = load_fabric(settings.ca_dir)
    host, port = code_address(settings, override=address)
    engine = create_engine(settings.database)
    try:
        new = await InviteStore(engine).create(
            ttl_s=ttl_s, actor="controller-cli", replace_node_id=replace
        )
    finally:
        await engine.dispose()
    return issue_code(new, fabric=fabric, host=host, port=port).as_dict()


@fabric_app.command("invite")
def fabric_invite(
    config: ConfigOption = None,
    ttl: Annotated[
        str | None,
        typer.Option(
            "--ttl",
            help="How long the code works: 90s, 15m, 2h (1 min - 24 h; default "
            "[controller.enroll] invite_ttl_s = 15 min).",
        ),
    ] = None,
    replace: Annotated[
        str | None,
        typer.Option(
            "--replace",
            metavar="NODE_ID",
            help="Let this code re-key an already enrolled node (a reinstalled "
            "laptop that kept its node_id but has a new key).",
        ),
    ] = None,
    address: Annotated[
        str | None,
        typer.Option(
            "--address",
            metavar="HOST:PORT",
            help="Address of the enrolment listener as other laptops reach it "
            "(default: [controller.enroll] advertise, else its bind address).",
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Make a single-use join code for one new laptop (V0.7 D135).

    Runs on the controller host (it reads the CA and the database). The code is
    printed once and never stored; only its hash is.
    """
    import asyncio
    import json

    from meshforge.common.errors import MeshForgeError
    from meshforge.common.ids import validate_node_id
    from meshforge.common.logging import configure_logging

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    if replace is not None:
        try:
            validate_node_id(replace)
        except MeshForgeError:
            typer.echo(f"{PROG_NAME}: --replace needs a node id (node_...)", err=True)
            raise typer.Exit(ExitCode.USAGE_ERROR) from None
    ttl_s = _ttl_seconds(ttl, settings.controller.enroll.invite_ttl_s)
    try:
        issued = asyncio.run(
            _create_invite(settings, ttl_s=ttl_s, replace=replace, address=address)
        )
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    if json_out:
        typer.echo(json.dumps(issued, indent=2, sort_keys=True))
        return
    from meshforge.common.pki import format_fingerprint

    typer.echo(str(issued["code"]))
    typer.echo(f"invite:       {issued['invite_id']}", err=True)
    typer.echo(f"controller:   {issued['address']}", err=True)
    typer.echo(f"CA pin:       {format_fingerprint(str(issued['ca_pin']))}", err=True)
    if issued["replace_node_id"]:
        typer.echo(f"replaces:     {issued['replace_node_id']}", err=True)
    typer.echo(_INVITE_HINT.format(expires=issued["expires_at"]), err=True)


async def _invites(
    settings: "ControllerSettings", revoke: str | None
) -> list[dict[str, object]]:
    from meshforge.controller.enrollment.invites import InviteStore
    from meshforge.controller.store.engine import create_engine

    engine = create_engine(settings.database)
    try:
        store = InviteStore(engine)
        if revoke is not None:
            return [(await store.revoke(revoke, actor="controller-cli")).as_dict()]
        return [record.as_dict() for record in await store.list()]
    finally:
        await engine.dispose()


@fabric_app.command("invites")
def fabric_invites(
    config: ConfigOption = None,
    revoke: Annotated[
        str | None,
        typer.Option(
            "--revoke", metavar="INVITE_ID", help="Revoke this unused invite."
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """List invites (newest first) or revoke one. Codes are never shown again."""
    import asyncio
    import json

    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    try:
        rows = asyncio.run(_invites(settings, revoke))
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    if json_out:
        typer.echo(json.dumps({"invites": rows}, indent=2, sort_keys=True))
        return
    if revoke is not None:
        typer.echo(f"revoked {revoke}")
        return
    if not rows:
        typer.echo("no invites")
        return
    for row in rows:
        used = f" by {row['used_by_node']}" if row["used_by_node"] else ""
        extra = f" replaces {row['replace_node_id']}" if row["replace_node_id"] else ""
        typer.echo(
            f"{row['id']}  {row['state']:<8} expires {row['expires_at']}{used}{extra}"
        )


def _server_lifetime(settings: "ControllerSettings") -> "timedelta":
    from datetime import timedelta

    return timedelta(days=settings.controller.tls.server_cert_days)


def main() -> None:
    """Console-script entry point."""
    app(prog_name=PROG_NAME)
