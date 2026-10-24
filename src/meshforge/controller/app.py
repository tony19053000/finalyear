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

``user create NAME [--admin] [--password-stdin] | list | disable NAME | enable
NAME | reset-password NAME`` (V0.7 G4, D141) manage the users ``compute login``
signs in as. They write the database directly (controller host only); a
disabled user or a new password revokes every API token of that user at once.
Passwords are read without echo (or one line from stdin) and never printed.

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

from meshforge.common.exit_codes import ExitCode

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import timedelta

    from pydantic import SecretStr

    from meshforge.controller.auth.users import UserStore
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
node_app = typer.Typer(
    name="node",
    help="Administer fabric nodes (V0.7): revoke a node.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(node_app)
user_app = typer.Typer(
    name="user",
    help="Manage REST users (V0.7): create, list, disable, enable, reset-password.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(user_app)
ca_app = typer.Typer(
    name="ca",
    help="Help browsers trust the fabric CA (V0.8): printed steps, no changes made.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(ca_app)
setup_app = typer.Typer(
    name="setup",
    help="The one-time 'create fabric' page of a new controller (V0.17).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(setup_app)
db_app = typer.Typer(
    name="db",
    help="The managed database (V0.17): status, backup, restore.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(db_app)

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
        from meshforge.common.buildinfo import version_line

        typer.echo(version_line(PROG_NAME))
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


@ca_app.command("trust-helper")
def ca_trust_helper(
    config: ConfigOption = None,
    os_name: Annotated[
        str,
        typer.Option("--os", help="linux, windows, macos or all."),
    ] = "all",
    pem: Annotated[
        bool,
        typer.Option(
            "--pem", help="Print the CA certificate (public) instead, to copy it."
        ),
    ] = False,
) -> None:
    """Print how to make a browser trust the fabric CA (Q1, ADR-0017).

    Reads ca.crt and prints its path, SHA-256 fingerprint and per-OS import
    steps. Changes nothing on this or any other machine.
    """
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging
    from meshforge.controller.ca_trust import OSES, trust_steps
    from meshforge.controller.fabric_ca import CA_CERT_NAME, load_fabric

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    if os_name != "all" and os_name not in OSES:
        typer.echo(f"{PROG_NAME}: --os must be {', '.join(OSES)} or all", err=True)
        raise typer.Exit(ExitCode.USAGE_ERROR)
    try:
        fabric = load_fabric(settings.ca_dir)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    ca_path = settings.ca_dir / CA_CERT_NAME
    if pem:
        typer.echo(ca_path.read_text(encoding="ascii"), nl=False)
        return
    hex_digest = fabric.fingerprint.upper()
    colons = ":".join(hex_digest[i : i + 2] for i in range(0, len(hex_digest), 2))
    typer.echo(
        trust_steps(
            ca_path=str(ca_path),
            fingerprint=colons,
            fabric_id=fabric.fabric_id,
            os_name=os_name,
        )
    )


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
    from meshforge.controller.db_managed.runtime import tool_database_settings
    from meshforge.controller.enrollment.codes import code_address, issue_code
    from meshforge.controller.enrollment.invites import InviteStore, check_ttl
    from meshforge.controller.fabric_ca import load_fabric
    from meshforge.controller.store.engine import create_engine

    check_ttl(ttl_s)
    # Everything that can refuse without side effects first: no CA (D135: an
    # invite needs one), no usable address -> nothing is written.
    fabric = load_fabric(settings.ca_dir)
    host, port = code_address(settings, override=address)
    engine = create_engine(tool_database_settings(settings))
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
    from meshforge.controller.db_managed.runtime import tool_database_settings
    from meshforge.controller.enrollment.invites import InviteStore
    from meshforge.controller.store.engine import create_engine

    engine = create_engine(tool_database_settings(settings))
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


async def _revoke(settings: "ControllerSettings", node_id: str) -> dict[str, object]:
    from meshforge.controller.db_managed.runtime import tool_database_settings
    from meshforge.controller.revocation import RevocationStore
    from meshforge.controller.store.engine import create_engine

    engine = create_engine(tool_database_settings(settings))
    try:
        store = RevocationStore(engine)
        return (await store.revoke_node(node_id, actor="controller-cli")).as_dict()
    finally:
        await engine.dispose()


_REVOKE_HINT = (
    "The running controller cuts its session within "
    "[controller.revocation] poll_interval_s and tells every agent; its jobs are "
    "retried elsewhere. There is no un-revoke: {node_id} stays revoked; the "
    "laptop can come back only under a new identity (`meshforge-agent identity "
    "reset --yes`, then `meshforge-agent join` with a new code)."
)


@node_app.command("revoke")
def node_revoke(
    node_id: Annotated[str, typer.Argument(help="The node to revoke (node_...).")],
    config: ConfigOption = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Revoke a node (V0.7 D138): its certificate is refused on every plane,
    its live session is cut and its jobs are retried elsewhere."""
    import asyncio
    import json

    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    try:
        outcome = asyncio.run(_revoke(settings, node_id))
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    if json_out:
        typer.echo(json.dumps(outcome, indent=2, sort_keys=True))
        return
    if outcome["already_revoked"]:
        typer.echo(f"{node_id} was already revoked")
        return
    serial = outcome["serial"] or "none (never enrolled)"
    typer.echo(f"revoked {node_id} (certificate serial {serial})")
    typer.echo(_REVOKE_HINT.format(node_id=node_id), err=True)


# ------------------------------------------------------------------ users


_STDIN_HELP = (
    "Read the password from the first line of stdin instead of prompting "
    "(for scripts; never pass a password as an argument)."
)


def _read_password(password_stdin: bool) -> "SecretStr":
    import sys

    from pydantic import SecretStr

    if password_stdin:
        line = sys.stdin.readline()
        return SecretStr(line.rstrip("\r\n"))
    value: str = typer.prompt(
        "Password", hide_input=True, confirmation_prompt="Repeat password"
    )
    return SecretStr(value)


def _user_command(
    config: Path | None, action: "Callable[[UserStore], Awaitable[object]]"
) -> object:
    import asyncio

    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")

    async def _run() -> object:
        from meshforge.controller.auth.users import UserStore
        from meshforge.controller.db_managed.runtime import tool_database_settings
        from meshforge.controller.store.engine import create_engine

        engine = create_engine(tool_database_settings(settings))
        try:
            return await action(UserStore(engine))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_run())
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None


async def _hashed(password: "SecretStr") -> str:
    from meshforge.controller.auth.passwords import PasswordHasher

    return await PasswordHasher(max_concurrent=1).hash(password)


@user_app.command("create")
def user_create(
    name: Annotated[str, typer.Argument(help="Login name (lower-case a-z 0-9 . _ -).")],
    admin: Annotated[
        bool,
        typer.Option(
            "--admin", help="An admin: invites, node revoke, every user's jobs."
        ),
    ] = False,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help=_STDIN_HELP)
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Create a REST user (`compute login` signs in as it)."""
    from meshforge.controller.auth.users import UserError, check_user_name
    from meshforge.controller.store.models.auth import UserRole

    try:
        checked = check_user_name(name)
    except UserError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    password = _read_password(password_stdin)
    role = UserRole.ADMIN if admin else UserRole.USER

    async def action(store: "UserStore") -> object:
        return await store.create_user(
            checked, pw_hash=await _hashed(password), role=role, actor="controller-cli"
        )

    record = _user_command(config, action)
    typer.echo(f"created {role.value} {checked} ({getattr(record, 'id', '?')})")
    typer.echo(
        "Log in from any laptop with `compute login --controller https://HOST:47110` "
        "(it needs the fabric's ca.crt, or the CA fingerprint from `fabric show`).",
        err=True,
    )


@user_app.command("list")
def user_list(
    config: ConfigOption = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Every user (never a password or a token)."""
    import json

    async def action(store: "UserStore") -> object:
        return [record.as_dict() for record in await store.list_users()]

    rows = _user_command(config, action)
    assert isinstance(rows, list)  # noqa: S101 - narrowing for mypy
    if json_out:
        typer.echo(json.dumps({"users": rows}, indent=2, sort_keys=True))
        return
    if not rows:
        typer.echo("no users (create the first admin: `user create NAME --admin`)")
        return
    for row in rows:
        state = "disabled" if row["disabled"] else "active"
        typer.echo(f"{row['name']:<24} {row['role']:<6} {state:<9} {row['id']}")


def _set_disabled(name: str, config: Path | None, *, disabled: bool) -> None:
    async def action(store: "UserStore") -> object:
        return await store.set_disabled(name, disabled=disabled, actor="controller-cli")

    result = _user_command(config, action)
    revoked = result[1] if isinstance(result, tuple) else 0
    if disabled:
        typer.echo(f"disabled {name} ({revoked} API token(s) revoked)")
    else:
        typer.echo(f"enabled {name} (they log in again with `compute login`)")


@user_app.command("disable")
def user_disable(
    name: Annotated[str, typer.Argument(help="The user to disable.")],
    config: ConfigOption = None,
) -> None:
    """Disable a user: their API tokens stop working on the next request."""
    _set_disabled(name, config, disabled=True)


@user_app.command("enable")
def user_enable(
    name: Annotated[str, typer.Argument(help="The user to enable.")],
    config: ConfigOption = None,
) -> None:
    """Enable a disabled user (old tokens stay revoked; they log in again)."""
    _set_disabled(name, config, disabled=False)


@user_app.command("reset-password")
def user_reset_password(
    name: Annotated[str, typer.Argument(help="The user whose password to set.")],
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help=_STDIN_HELP)
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Set a new password; every API token of the user is revoked with it."""
    password = _read_password(password_stdin)

    async def action(store: "UserStore") -> object:
        return await store.reset_password(
            name, pw_hash=await _hashed(password), actor="controller-cli"
        )

    result = _user_command(config, action)
    revoked = result[1] if isinstance(result, tuple) else 0
    typer.echo(f"password of {name} reset ({revoked} API token(s) revoked)")


# ------------------------------------------------------------------ setup


@setup_app.command("url")
def setup_url(
    config: ConfigOption = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Print the setup page's link (with its one-time token) for a controller
    that has no fabric yet. Open it in a browser ON THIS MACHINE. Exits 5 when
    the fabric already exists."""
    import json

    from meshforge.common.errors import MeshForgeError
    from meshforge.controller.fabric_ca import fabric_present
    from meshforge.controller.setup.token import ensure_setup_token

    settings = _load_settings(config)
    if settings.security.insecure:
        typer.echo(
            f"{PROG_NAME}: the setup page is for [security] mode = 'mtls'; in "
            "insecure-dev mode run `fabric create` and `user create` instead",
            err=True,
        )
        raise typer.Exit(ExitCode.CONFIG_ERROR)
    if fabric_present(settings.ca_dir):
        typer.echo(
            f"{PROG_NAME}: this controller is already set up (it has a fabric CA); "
            "sign in on its dashboard",
            err=True,
        )
        raise typer.Exit(ExitCode.CONFLICT)
    try:
        token, created = ensure_setup_token(settings.setup_token_path)
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None
    url = f"http://{settings.setup_bind}/setup#token={token.get_secret_value()}"
    port = settings.setup_bind.rsplit(":", 1)[-1]
    if json_out:
        typer.echo(json.dumps({"url": url, "created": created}, sort_keys=True))
        return
    typer.echo(url)
    typer.echo(
        "Open this link in a browser on this machine to create the fabric (it works "
        "once). From another computer: ssh -L "
        f"{port}:127.0.0.1:{port} <this machine>, then open the same link there.",
        err=True,
    )


# ----------------------------------------------------------------- doctor


@app.command("doctor")
def doctor(
    config: ConfigOption = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Machine-readable output.")
    ] = False,
) -> None:
    """Check that this controller can run: bundled PostgreSQL, glibc, ports,
    password files, database files, disk, fabric CA, dashboard. Exits 1 when
    any check fails (warnings do not count)."""
    import json

    from meshforge.common.logging import configure_logging
    from meshforge.controller.doctor import run_checks

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    report = run_checks(settings)
    if json_out:
        typer.echo(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        for line in report.lines():
            typer.echo(line)
    if not report.ok:
        raise typer.Exit(ExitCode.FAILURE)


# --------------------------------------------------------------------- db


def _db_run(config: Path | None, action: str, *args: object) -> dict[str, object]:
    from meshforge.common.errors import MeshForgeError
    from meshforge.common.logging import configure_logging
    from meshforge.controller.db_managed import admin

    settings = _load_settings(config)
    configure_logging("WARNING", fmt=settings.log.format, component="controller")
    try:
        if action == "status":
            return admin.status(settings)
        if action == "backup":
            return admin.backup(settings, *args)  # type: ignore[arg-type]
        return admin.restore(settings, *args)  # type: ignore[arg-type]
    except MeshForgeError as exc:
        typer.echo(f"{PROG_NAME}: {exc}", err=True)
        raise typer.Exit(exc.exit_code) from None


JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


@db_app.command("status")
def db_status(config: ConfigOption = None, json_out: JsonOption = False) -> None:
    """Show the controller's database: mode, running, last good start, backups."""
    import json

    facts = _db_run(config, "status")
    if json_out:
        typer.echo(json.dumps(facts, indent=2, sort_keys=True))
        return
    for key in (
        "mode",
        "url",
        "root",
        "port",
        "running",
        "data_major",
        "bundled_major",
        "disk_free_bytes",
    ):
        if key in facts:
            typer.echo(f"{key + ':':<17} {facts[key]}")
    last = facts.get("last_good_start")
    if isinstance(last, dict):
        typer.echo(
            f"{'last good start:':<17} {last['version']} (revision "
            f"{last['revision']}, {last['at']})"
        )
    backups = facts.get("backups")
    if isinstance(backups, list):
        typer.echo(f"{'backups:':<17} {len(backups)}")
        for item in backups:
            typer.echo(
                f"  {item['modified']}  {item['size_bytes']:>12}  {item['kind']:<11} "
                f"{item['path']}"
            )


@db_app.command("backup")
def db_backup(
    config: ConfigOption = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Dump file (default <db root>/backups/manual-<UTC time>.dump).",
            dir_okay=False,
        ),
    ] = None,
    json_out: JsonOption = False,
) -> None:
    """Dump the database (pg_dump custom format, owner-only file). Works while
    the controller runs; the file is the whole fabric database - keep it safe."""
    import json

    facts = _db_run(config, "backup", out)
    if json_out:
        typer.echo(json.dumps(facts, indent=2, sort_keys=True))
        return
    typer.echo(str(facts["path"]))
    typer.echo(
        f"{facts['size_bytes']} bytes in {facts['seconds']} s ({facts['cluster']})",
        err=True,
    )


@db_app.command("restore")
def db_restore(
    dump: Annotated[
        Path, typer.Argument(help="A dump made by `db backup` or pg_dump -Fc.")
    ],
    config: ConfigOption = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm: the current database is replaced."),
    ] = False,
) -> None:
    """Replace the database with a backup. The controller must be stopped
    (sudo systemctl stop meshforge-controller). Everything newer than the
    backup is lost."""
    if not yes:
        typer.echo(
            f"{PROG_NAME}: this replaces the whole database with {dump}; "
            "add --yes to confirm",
            err=True,
        )
        raise typer.Exit(ExitCode.USAGE_ERROR)
    facts = _db_run(config, "restore", dump)
    typer.echo(f"restored from {facts['restored_from']} in {facts['seconds']} s")
    typer.echo(
        "start the controller again: sudo systemctl start meshforge-controller "
        "(it migrates an older backup on start)",
        err=True,
    )


def _server_lifetime(settings: "ControllerSettings") -> "timedelta":
    from datetime import timedelta

    return timedelta(days=settings.controller.tls.server_cert_days)


def main() -> None:
    """Console-script entry point."""
    import sys

    from meshforge.common.buildinfo import bundle_root
    from meshforge.common.system_cli import apply, current_user
    from meshforge.controller.system_install import CONTROLLER_PROGRAM

    # V0.17 G2 (Q15): on a packaged controller, admin commands re-execute as
    # the service user (its database passwords and CA are owner-only).
    apply(
        sys.argv[1:],
        program=CONTROLLER_PROGRAM,
        installed=CONTROLLER_PROGRAM.installed,
        user=current_user,
        bundled=lambda: bundle_root() is not None,
    )
    app(prog_name=PROG_NAME)
