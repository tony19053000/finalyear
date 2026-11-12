"""Shared plumbing of every ``compute`` command (D202 follow-up).

Settings, the controller client, ``_die``, ``_fetch``, the ``--json`` option and
the NODE_ID check live here, in a **leaf** module: it imports no command module
and not :mod:`meshforge.cli.app`. Each ``*_commands`` module imports it as
``cli`` and looks the helpers up at call time (``cli._client(settings)``), so a
test patches one place - ``meshforge.cli.shared._client`` - and every command,
including ``nodes`` and ``status`` in :mod:`meshforge.cli.app`, sees the patch.

Before this split the helpers lived in :mod:`meshforge.cli.app`, which imports
the command modules at its end to register them; importing a command module
first (``import meshforge.cli.job_commands``) therefore failed with a circular
import. Now :mod:`meshforge.cli.app` depends on the command modules and they
depend only on this module, so any ``meshforge.cli`` module can be imported
first (pinned by ``tests/unit/cli/test_v08_d202_import_order.py``).

``meshforge.cli.client`` (httpx, ~150 ms) and ``meshforge.cli.credentials`` are
imported inside the functions that need them, so ``compute --version`` stays
cheap.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, NoReturn

import typer
from rich.console import Console

from meshforge.cli import render
from meshforge.cli.exits import CliExit
from meshforge.cli.settings import CliSettings
from meshforge.common.errors import ConfigError

if TYPE_CHECKING:  # `meshforge.cli.client` pulls in httpx - see `_client` below.
    from meshforge.cli.client import ControllerClient
    from meshforge.cli.credentials import StoredLogin

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help=(
            "Print the answer as one line of JSON with a schema_version "
            "(for scripts; see docs/CLI.md)."
        ),
    ),
]


def _out() -> Console:
    """The stdout console (:func:`meshforge.cli.render.console`)."""
    return render.console()


def _echo_json(payload: Mapping[str, Any]) -> None:
    """``--json`` output (:func:`meshforge.cli.render.emit_json`)."""
    render.emit_json(payload)


def _die(message: str, code: int) -> NoReturn:
    """One sanitised line on stderr, then exit with ``code`` (a :class:`CliExit`).
    Never a traceback.

    Deliberately a plain line and not a Rich console: a console would wrap the
    line at the terminal width (turning a single error into several lines a
    ``grep`` would miss). :func:`meshforge.cli.render.fail` flattens control
    characters and scrubs anything token-shaped.
    """
    render.fail(message)
    raise typer.Exit(code)


def _settings() -> CliSettings:
    """The CLI settings; with no ``controller_url`` configured, the controller
    ``compute login`` last logged in to (V0.7 G4)."""
    try:
        settings = CliSettings.load()
    except ConfigError as exc:
        _die(exc.message, CliExit.CONFIG_ERROR)
    if "controller_url" in settings.cli.model_fields_set:
        return settings
    stored = _load_stored(settings)
    if stored is None or stored.controller_url == settings.cli.controller_url:
        return settings
    cli = settings.cli.model_copy(update={"controller_url": stored.controller_url})
    return settings.model_copy(update={"cli": cli})


def _load_stored(settings: CliSettings) -> StoredLogin | None:
    from meshforge.cli.credentials import credentials_dir, load_login

    try:
        return load_login(credentials_dir(settings.cli.credentials_dir))
    except ConfigError as exc:
        _die(exc.message, CliExit.CONFIG_ERROR)


def _stored_login(settings: CliSettings) -> StoredLogin | None:
    """The stored login, only if it belongs to the controller being called."""
    stored = _load_stored(settings)
    if stored is None or stored.controller_url != settings.cli.controller_url:
        return None
    return stored


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

    stored = _stored_login(settings)
    try:
        token = resolve_token(settings, _token_file_option(), stored=stored)
    except ConfigError as exc:
        _die(exc.message, CliExit.CONFIG_ERROR)
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
        return client_context(
            resolve_ca(settings, _CA_FILE.get(), stored=_stored_login(settings))
        )
    except ConfigError as exc:
        _die(exc.message, CliExit.CONFIG_ERROR)


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
            CliExit.USAGE_ERROR,
        )


_REVOKED: Final = "revoked"


def _resolve_node_refs(client: Any, values: Sequence[str], option: str) -> list[str]:
    """The CLI's one NODE-by-name resolver (V1.0 G9-B; G11, D452): each value
    -> a node id, in order. A canonical ``node_<ULID>`` is taken as is (a
    revoked node's id too - the controller answers for it). Anything else
    must match exactly one node's display name or hostname
    (case-insensitive) in ``compute nodes``, **ignoring revoked nodes**: a
    laptop that re-joined after a revoke keeps its hostname, and its old
    revoked row must not make the name ambiguous. None or several matches is
    a usage error (never a guess). The node list is fetched once, only when a
    name is given."""
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import validate_node_id

    out: list[str] = []
    nodes: list[Mapping[str, Any]] | None = None
    for value in values:
        try:
            out.append(str(validate_node_id(value)))
            continue
        except InvalidIdError:
            pass
        wanted = value.strip().casefold()
        if not wanted:
            _die(f"{option} needs a node id or name", CliExit.USAGE_ERROR)
        if nodes is None:
            listed = _fetch(client.nodes).get("nodes")
            nodes = [
                n
                for n in listed or []
                if isinstance(n, Mapping) and n.get("state") != _REVOKED
            ]
        matches = sorted(
            {
                str(n.get("node_id"))
                for n in nodes
                if any(
                    isinstance(n.get(key), str)
                    and n[key].strip()
                    and n[key].strip().casefold() == wanted
                    for key in ("display_name", "hostname")
                )
            }
        )
        if not matches:
            _die(
                f"{option} {render.clip(value)!r}: no node has that id, name "
                "or hostname; run `compute nodes` to list them",
                CliExit.USAGE_ERROR,
            )
        if len(matches) > 1:
            _die(
                f"{option} {render.clip(value)!r} matches {len(matches)} nodes "
                f"({', '.join(render.clip(m) for m in matches)}); give the node id",
                CliExit.USAGE_ERROR,
            )
        out.append(matches[0])
    return out


def _checked_campaign_id(value: str) -> str:
    """A CAMPAIGN_ID argument (a path segment, like NODE_ID) is validated here,
    before any request is built (V0.9 G3; moved here in G5c, D248, so every
    ``bench`` command module shares it without importing another)."""
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import IdKind, parse_id

    try:
        parse_id(value, IdKind.CAMPAIGN)
    except InvalidIdError:
        _die(
            "CAMPAIGN_ID is not a canonical camp_<ULID>; run `compute bench campaigns`",
            CliExit.USAGE_ERROR,
        )
    return value
