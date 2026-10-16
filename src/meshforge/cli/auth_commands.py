"""``compute login | logout | whoami | tokens`` (V0.7 G4, D141).

* ``login [--controller URL] [--ca FILE | --fingerprint FP] [--user NAME]
  [--password-stdin]`` - verifies the controller **before** sending the password:
  against ``--ca`` (or a CA already known on this laptop), or - first contact -
  by reading the CA from the TLS handshake and accepting it only if its full
  SHA-256 fingerprint equals ``--fingerprint`` (``meshforge-controller fabric
  show`` prints it) and it issued the controller's certificate; a mismatch
  stops with nothing sent. The password is read without echo (or one line
  from stdin), never from argv. The token and ``ca.crt`` are stored privately
  (:mod:`meshforge.cli.credentials`); every later command uses them.
* ``logout`` - revokes the stored token on the controller, then forgets it.
* ``whoami`` - who the controller says you are.
* ``tokens list [--all]`` / ``tokens revoke TOKEN_ID`` - your API tokens.

No token, password or key ever reaches stdout, stderr or a log line.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final
from urllib.parse import urlsplit

import typer
from pydantic import SecretStr

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit
from meshforge.common.errors import ConfigError, FailureType

if TYPE_CHECKING:
    from cryptography import x509

    from meshforge.cli.client import ControllerClient
    from meshforge.cli.settings import CliSettings

__all__ = ["login", "logout", "tokens_app", "whoami"]

_HANDSHAKE_TIMEOUT_S: Final = 10.0
_DEFAULT_HTTPS_PORT: Final = 443

tokens_app = typer.Typer(
    name="tokens",
    help="List or revoke your API tokens (V0.7).",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def _url(settings: CliSettings, controller: str | None) -> str:
    from meshforge.cli.settings import normalise_controller_url

    if controller is None:
        return settings.cli.controller_url
    try:
        return normalise_controller_url(controller)
    except ConfigError as exc:
        cli._die(f"--controller: {exc.message}", CliExit.USAGE_ERROR)


def _pinned_from_handshake(url: str, fingerprint: str) -> x509.Certificate:
    """Read the CA the controller presents and keep it only if it matches."""
    from meshforge.common.tls_pin import pinned_ca, presented_chain

    parts = urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or _DEFAULT_HTTPS_PORT
    cleaned = fingerprint.replace(" ", "").replace(":", "").lower()
    if len(cleaned) != 64:
        cli._die(
            "--fingerprint needs the CA's full SHA-256 fingerprint (64 hex "
            "characters; `meshforge-controller fabric show` prints it)",
            CliExit.USAGE_ERROR,
        )
    try:
        presented = presented_chain(host, port, timeout_s=_HANDSHAKE_TIMEOUT_S)
    except ssl.SSLError as exc:
        cli._die(
            f"{host}:{port} did not complete a TLS handshake "
            f"({exc.reason or type(exc).__name__}); is this the controller's REST "
            "address (https://HOST:47110)?",
            CliExit.UNAVAILABLE,
        )
    except (OSError, RuntimeError) as exc:
        cli._die(
            f"cannot read the controller's certificate at {host}:{port} "
            f"({type(exc).__name__})",
            CliExit.UNAVAILABLE,
        )
    ca = pinned_ca(presented, cleaned, require_full=True)
    if ca is None:
        cli._die(
            "the controller at this address does not hold the fabric CA with "
            "that fingerprint (wrong address, another fabric, or someone in "
            "between). Nothing was sent; check the address and the fingerprint",
            CliExit.AUTH,
        )
    assert ca is not None  # noqa: S101 - _die does not return
    return ca


def _trusted_ca(
    settings: CliSettings, url: str, ca: Path | None, fingerprint: str | None
) -> x509.Certificate | None:
    """The CA to verify ``url`` with (None for an insecure-dev http URL)."""
    from meshforge.cli.settings import resolve_ca
    from meshforge.common import pki

    if not url.startswith("https://"):
        if ca is not None or fingerprint is not None:
            cli._die(
                "--ca/--fingerprint need an https:// controller URL",
                CliExit.USAGE_ERROR,
            )
        return None
    if ca is not None:
        try:
            cert = pki.read_cert(ca, what="fabric CA certificate")
        except pki.PkiError as exc:
            cli._die(f"--ca: {exc.message}", CliExit.CONFIG_ERROR)
        if fingerprint is not None and not pki.fingerprint_matches(
            cert, fingerprint, require_full=True
        ):
            cli._die(
                "--ca does not have the --fingerprint you gave; nothing was sent",
                CliExit.AUTH,
            )
        return cert
    if fingerprint is not None:
        return _pinned_from_handshake(url, fingerprint)
    try:
        return resolve_ca(settings, stored=cli._stored_login(settings))
    except ConfigError as exc:
        cli._die(exc.message, CliExit.CONFIG_ERROR)


def _read_password(password_stdin: bool) -> SecretStr:
    if password_stdin:
        return SecretStr(sys.stdin.readline().rstrip("\r\n"))
    value: str = typer.prompt("Password", hide_input=True)
    return SecretStr(value)


def _client_for(
    settings: CliSettings,
    url: str,
    ca: x509.Certificate | None,
    token: SecretStr | None,
) -> ControllerClient:
    from meshforge.cli.client import ControllerClient
    from meshforge.common.fabric_tls import client_context

    return ControllerClient(
        url,
        timeout_s=settings.cli.timeout_s,
        token=token,
        ssl_context=client_context(ca) if ca is not None else None,
    )


def login(
    controller: Annotated[
        str | None,
        typer.Option(
            "--controller",
            help="Controller REST URL, e.g. https://192.168.1.20:47110 "
            "(default: [cli] controller_url).",
        ),
    ] = None,
    ca: Annotated[
        Path | None,
        typer.Option(
            "--ca",
            help="The fabric's ca.crt to verify the controller with.",
            dir_okay=False,
        ),
    ] = None,
    fingerprint: Annotated[
        str | None,
        typer.Option(
            "--fingerprint",
            help="First contact: the CA's full SHA-256 fingerprint (from "
            "`meshforge-controller fabric show`); the CA is read from the "
            "handshake and kept only if it matches.",
        ),
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", "-u", help="Your user name.")
    ] = None,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read the password from the first line of stdin (scripts).",
        ),
    ] = False,
    token_name: Annotated[
        str,
        typer.Option("--token-name", help="A label for this token (e.g. laptop)."),
    ] = "compute",  # noqa: S107 - a label, not a secret
) -> None:
    """Log in to the controller and keep an API token for later commands."""
    from meshforge.cli.client import ControllerError, exit_code_for
    from meshforge.cli.credentials import credentials_dir, save_login

    if password_stdin and user is None:
        # stdin carries the password; the name cannot be prompted for too.
        cli._die("--password-stdin needs --user NAME", CliExit.USAGE_ERROR)
    settings = cli._settings()
    url = _url(settings, controller)
    trusted = _trusted_ca(settings, url, ca or cli._CA_FILE.get(), fingerprint)
    name = user if user is not None else typer.prompt("User")
    password = _read_password(password_stdin)
    with _client_for(settings, url, trusted, None) as client:
        try:
            answer = client.login(name.strip(), password, token_name=token_name)
        except ControllerError as exc:
            if exc.failure_type is FailureType.UNAUTHORIZED:
                cli._die("wrong user name or password", CliExit.AUTH)
            cli._die(exc.message, exit_code_for(exc))
    token = answer.get("token")
    who = answer.get("user")
    if not isinstance(token, str) or not isinstance(who, dict):
        cli._die("the controller sent an unexpected login answer", CliExit.FAILURE)
    try:
        stored = save_login(
            credentials_dir(settings.cli.credentials_dir),
            controller_url=url,
            token=SecretStr(token),
            token_id=_str(answer.get("token_id")),
            user=_str(who.get("name")) or name,
            role=_str(who.get("role")) or "?",
            expires_at=_str(answer.get("expires_at")),
            ca_cert=trusted,
        )
    except ConfigError as exc:
        cli._die(exc.message, CliExit.CONFIG_ERROR)
    render.line(
        f"logged in to {url} as {render.echo(stored.user)} ({render.echo(stored.role)})"
    )
    if stored.expires_at:
        render.line(f"the login lasts until {render.echo(stored.expires_at)}", err=True)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def logout() -> None:
    """Revoke the stored API token on the controller and forget it here."""
    from meshforge.cli.client import ControllerError
    from meshforge.cli.credentials import credentials_dir, forget_login

    settings = cli._settings()
    stored = cli._stored_login(settings)
    directory = credentials_dir(settings.cli.credentials_dir)
    if stored is None:
        forget_login(directory)
        render.line("not logged in")
        return
    note = "the token was revoked on the controller"
    if stored.token_id is not None:
        with cli._client(settings) as client:
            try:
                client.revoke_token(stored.token_id)
            except ControllerError as exc:
                if exc.failure_type is not FailureType.UNAUTHORIZED:
                    note = (
                        f"could not revoke the token on the controller ({exc.message}); "
                        "it stops working when it expires"
                    )
                else:
                    note = "the token no longer worked on the controller"
    forget_login(directory)
    render.line(f"logged out of {stored.controller_url}")
    render.line(note, err=True)


def whoami(json_output: cli.JsonOption = False) -> None:
    """Who the controller says you are, and until when your login lasts."""
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(client.whoami)
    if json_output:
        cli._echo_json(payload)
        return
    user = payload.get("user")
    user = user if isinstance(user, dict) else {}
    # T-2: every server string capped like a table cell.
    clip = render.echo  # T-2 cap, T-6 scrub
    line = f"{clip(user.get('name', '?'))} ({clip(user.get('role', '?'))})"
    if payload.get("legacy"):
        line += " - the legacy fabric token (switch to `compute login`)"
    render.line(line)
    if payload.get("token_expires_at"):
        render.line(
            f"token {clip(payload.get('token_id'))} until "
            f"{clip(payload['token_expires_at'])}"
        )


@tokens_app.command("list")
def tokens_list(
    all_users: Annotated[
        bool, typer.Option("--all", help="Every user's tokens (admin only).")
    ] = False,
    json_output: cli.JsonOption = False,
) -> None:
    """Your API tokens, newest first (never the token itself)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.tokens(all_users=all_users))
    if json_output:
        cli._echo_json(payload)
        return
    rows = payload.get("tokens")
    if not isinstance(rows, list) or not rows:
        render.line("no tokens")
        return
    table = render.table(
        "", "TOKEN", "USER", "KIND", "STATE", "NAME", "EXPIRES", title="API tokens"
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        table.add_row(
            "*" if row.get("current") is True else "",
            render.cell(row.get("id")),
            render.cell(row.get("user")),
            render.cell(row.get("kind")),
            render.cell(row.get("state")),
            render.cell(row.get("name")),
            render.cell(row.get("expires_at")),
        )
    render.show(table)
    render.line("* = the token this command used", err=True)


@tokens_app.command("revoke")
def tokens_revoke(
    token_id: Annotated[str, typer.Argument(help="The token to revoke (tok_...).")],
) -> None:
    """Revoke one of your API tokens (an admin: anyone's)."""
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import validate_api_token_id

    try:
        checked = str(validate_api_token_id(token_id))
    except InvalidIdError:
        cli._die("TOKEN_ID is not a token id (tok_...)", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.revoke_token(checked))
    if payload.get("already_revoked"):
        render.line(f"{checked} was already revoked")
    else:
        render.line(f"revoked {checked}")
