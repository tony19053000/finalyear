"""V0.7 G4 (D141): ``compute login | logout | whoami | tokens`` and the stored
credentials - in-process with a mock transport, no socket.

Pinned: the token and ca.crt are stored in private files and never printed;
the password is never an argument; a 401 on login says "wrong user name or
password" and stores nothing; every later command sends the stored token to
the controller it came from (not to another); 401 says "run compute login",
403 says why; ``pinned_ca`` keeps only the fingerprinted CA that issued the
server certificate.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_fabric import ca_file, fabric_ca
from pydantic import SecretStr
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import auth_commands
from meshforge.cli import shared as cli_shared
from meshforge.cli.client import ControllerClient
from meshforge.cli.credentials import (
    CA_FILE_NAME,
    LOGIN_FILE_NAME,
    credentials_dir,
    forget_login,
    load_login,
    save_login,
)
from meshforge.cli.settings import CliSettings, resolve_ca, resolve_token
from meshforge.common import pki
from meshforge.common.api_token import new_api_token
from meshforge.common.config import default_data_dir
from meshforge.common.errors import ConfigError, ExitCode
from meshforge.common.ids import new_fabric_id
from meshforge.common.tls_pin import pinned_ca

pytestmark = pytest.mark.unit

TOKEN = new_api_token().get_secret_value()
PASSWORD = "a-good-password-1"
URL = "https://127.0.0.1:47110"
OTHER_URL = "https://10.9.9.9:47110"
TOKEN_ID = "tok_01J0T0KEN00000000000000000"[:30]


@pytest.fixture
def creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "creds"
    monkeypatch.setenv("MESHFORGE_CLI__CREDENTIALS_DIR", str(directory))
    return directory


def _save(directory: Path, url: str = URL, token: str = TOKEN) -> None:
    save_login(
        directory,
        controller_url=url,
        token=SecretStr(token),
        token_id=TOKEN_ID,
        user="alice",
        role="user",
        expires_at="2027-01-01T00:00:00+00:00",
        ca_cert=fabric_ca().cert,
    )


# ------------------------------------------------------------ credentials


@pytest.mark.security
def test_save_and_load_round_trip_private(creds: Path) -> None:
    _save(creds)
    stored = load_login(creds)
    assert stored is not None
    assert stored.token.get_secret_value() == TOKEN
    assert stored.controller_url == URL and stored.user == "alice"
    assert stored.ca_file == creds / CA_FILE_NAME
    assert TOKEN not in repr(stored) and TOKEN not in str(stored.as_public_dict())
    if os.name == "posix":
        assert stat.S_IMODE((creds / LOGIN_FILE_NAME).stat().st_mode) == 0o600
        assert stat.S_IMODE(creds.stat().st_mode) == 0o700
    assert forget_login(creds) is True
    assert load_login(creds) is None
    assert not (creds / CA_FILE_NAME).exists()
    assert forget_login(creds) is False


@pytest.mark.security
@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_a_login_file_others_can_read_is_refused(creds: Path) -> None:
    _save(creds)
    (creds / LOGIN_FILE_NAME).chmod(0o644)
    with pytest.raises(ConfigError) as caught:
        load_login(creds)
    assert TOKEN not in caught.value.message


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"controller_url": "https://x"}', b'{"token": "abc"}'],
)
def test_a_damaged_login_file_is_a_config_error(creds: Path, body: bytes) -> None:
    _save(creds)
    path = creds / LOGIN_FILE_NAME
    path.write_bytes(body)
    path.chmod(0o600)
    with pytest.raises(ConfigError) as caught:
        load_login(creds)
    assert "compute login" in caught.value.message


def test_default_credentials_dir_is_in_the_config_dir() -> None:
    assert credentials_dir(None).name == "compute"
    assert credentials_dir(Path("/x")) == Path("/x")


# ------------------------------------------------------------ precedence


def test_stored_token_sits_between_explicit_config_and_the_local_fabric_token(
    creds: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(creds)
    stored = load_login(creds)
    # XDG data dir on Linux, %LOCALAPPDATA%\\MeshForge on Windows (cli conftest)
    local = default_data_dir() / "controller" / "fabric.token"
    local.parent.mkdir(parents=True)
    local.write_text("L" * 43)
    plain = CliSettings.load()
    got = resolve_token(plain, stored=stored)
    assert got is not None and got.get_secret_value() == TOKEN
    got = resolve_token(plain)
    assert got is not None and got.get_secret_value() == "L" * 43
    monkeypatch.setenv("MESHFORGE_CLI__TOKEN", "C" * 43)
    got = resolve_token(CliSettings.load(), stored=stored)
    assert got is not None and got.get_secret_value() == "C" * 43


def test_stored_ca_is_used_unless_one_is_named(creds: Path) -> None:
    _save(creds)
    stored = load_login(creds)
    settings = CliSettings.load()
    assert resolve_ca(settings, stored=stored) == fabric_ca().cert
    other = pki.create_ca(new_fabric_id())
    named = creds.parent / "other.crt"
    named.write_bytes(pki.cert_to_pem(other.cert))
    assert resolve_ca(settings, named, stored=stored) == other.cert


def test_settings_follow_the_stored_controller_unless_one_is_configured(
    creds: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save(creds, url=OTHER_URL)
    assert cli_shared._settings().cli.controller_url == OTHER_URL
    assert cli_shared._stored_login(cli_shared._settings()) is not None
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", URL)
    settings = cli_shared._settings()
    assert settings.cli.controller_url == URL
    # The token of OTHER_URL is never sent to URL.
    assert cli_shared._stored_login(settings) is None


# ------------------------------------------------------------ commands


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Handler], None]]:
    """Every client (login's and the shared one) uses ``handler``; the real
    token resolution still decides the Authorization header."""
    made: list[ControllerClient] = []

    def install(handler: Handler) -> None:
        def login_client(
            settings: CliSettings, url: str, ca: Any, token: SecretStr | None
        ) -> ControllerClient:
            client = ControllerClient(
                url,
                timeout_s=5,
                token=token,
                transport=httpx.MockTransport(handler),
            )
            made.append(client)
            return client

        def shared(settings: CliSettings) -> ControllerClient:
            stored = cli_shared._stored_login(settings)
            token = resolve_token(settings, stored=stored)
            return login_client(settings, settings.cli.controller_url, None, token)

        monkeypatch.setattr(auth_commands, "_client_for", login_client)
        monkeypatch.setattr(cli_shared, "_client", shared)

    yield install
    for client in made:
        client.close()


def _login_ok(seen: list[httpx.Request]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/auth/login":
            return httpx.Response(
                200,
                json={
                    "token": TOKEN,
                    "token_id": TOKEN_ID,
                    "expires_at": "2027-01-01T00:00:00+00:00",
                    "user": {"id": "usr_x", "name": "alice", "role": "user"},
                },
            )
        if request.url.path == "/v1/auth/whoami":
            return httpx.Response(
                200,
                json={
                    "user": {"id": "usr_x", "name": "alice", "role": "user"},
                    "legacy": False,
                    "token_id": TOKEN_ID,
                    "token_expires_at": "2027-01-01T00:00:00+00:00",
                },
            )
        if request.url.path.startswith("/v1/auth/tokens"):
            return httpx.Response(
                200,
                json={"id": TOKEN_ID, "state": "revoked", "already_revoked": False},
            )
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "x"}}
        )

    return handler


@pytest.mark.security
def test_login_stores_the_token_privately_and_never_prints_it(
    runner: CliRunner, creds: Path, transport: Callable[[Handler], None]
) -> None:
    seen: list[httpx.Request] = []
    transport(_login_ok(seen))
    result = runner.invoke(
        cli_app.app,
        ["login", "--ca", str(ca_file()), "--user", "alice", "--password-stdin"],
        input=PASSWORD + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "logged in to https://127.0.0.1:47110 as alice (user)" in result.output
    assert TOKEN not in result.output and PASSWORD not in result.output
    body = json.loads(seen[0].content)
    assert body == {"name": "alice", "password": PASSWORD, "token_name": "compute"}
    assert "authorization" not in seen[0].headers  # login sends no token
    assert PASSWORD not in str(seen[0].url)
    stored = load_login(creds)
    assert stored is not None and stored.token.get_secret_value() == TOKEN
    assert (creds / CA_FILE_NAME).read_bytes() == pki.cert_to_pem(fabric_ca().cert)

    # Later commands send the stored token in the header only.
    seen.clear()
    who = runner.invoke(cli_app.app, ["whoami"])
    assert who.exit_code == 0 and "alice (user)" in who.output
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(seen[0].url)

    # logout revokes the token on the controller, then forgets it.
    seen.clear()
    out = runner.invoke(cli_app.app, ["logout"])
    assert out.exit_code == 0, out.output
    assert seen[0].method == "DELETE" and seen[0].url.path.endswith(TOKEN_ID)
    assert load_login(creds) is None


def test_wrong_password_stores_nothing(
    runner: CliRunner, creds: Path, transport: Callable[[Handler], None]
) -> None:
    transport(
        lambda r: httpx.Response(
            401,
            json={"error": {"code": "unauthorized", "message": "wrong"}},
        )
    )
    result = runner.invoke(
        cli_app.app,
        ["login", "--ca", str(ca_file()), "-u", "alice", "--password-stdin"],
        input="nope-nope-nope\n",
    )
    assert result.exit_code == 3  # V0.8 G6: AUTH (was FAILURE)
    assert "wrong user name or password" in result.output
    assert load_login(creds) is None


def test_backoff_answer_is_shown_with_its_reason(
    runner: CliRunner, creds: Path, transport: Callable[[Handler], None]
) -> None:
    transport(
        lambda r: httpx.Response(
            429,
            json={
                "error": {
                    "code": "resource_busy",
                    "message": "too many failed logins for this user; wait",
                    "reason": "rate_limited_user",
                }
            },
            headers={"retry-after": "30"},
        )
    )
    result = runner.invoke(
        cli_app.app,
        ["login", "--ca", str(ca_file()), "-u", "alice", "--password-stdin"],
        input=PASSWORD + "\n",
    )
    assert result.exit_code != 0
    assert "too many failed logins" in result.output
    assert load_login(creds) is None


@pytest.mark.security
def test_there_is_no_password_option(runner: CliRunner, creds: Path) -> None:
    result = runner.invoke(
        cli_app.app, ["login", "--ca", str(ca_file()), "--password", PASSWORD]
    )
    assert result.exit_code == 2  # usage error: passwords never go in argv


def test_401_and_403_messages(
    runner: CliRunner, creds: Path, transport: Callable[[Handler], None]
) -> None:
    transport(
        lambda r: httpx.Response(
            401, json={"error": {"code": "unauthorized", "message": "x"}}
        )
    )
    result = runner.invoke(cli_app.app, ["jobs"])
    assert result.exit_code == 3  # V0.8 G6: AUTH (was FAILURE)
    assert "run `compute login`" in result.output
    transport(
        lambda r: httpx.Response(
            403,
            json={
                "error": {
                    "code": "unauthorized",
                    "message": "this call needs an admin account",
                }
            },
        )
    )
    result = runner.invoke(cli_app.app, ["fabric", "invites"])
    assert "(403): this call needs an admin account" in result.output


def test_tokens_revoke_validates_the_id_before_any_request(
    runner: CliRunner, creds: Path
) -> None:
    result = runner.invoke(cli_app.app, ["tokens", "revoke", "../../x"])
    assert result.exit_code == ExitCode.USAGE_ERROR


@pytest.mark.security
def test_fingerprint_must_be_the_full_form(runner: CliRunner, creds: Path) -> None:
    short = pki.short_fingerprint(fabric_ca().cert)
    result = runner.invoke(
        cli_app.app,
        ["login", "--fingerprint", short, "-u", "a", "--password-stdin"],
        input=PASSWORD + "\n",
    )
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert "64 hex" in result.output


@pytest.mark.security
def test_ca_and_fingerprint_must_agree(runner: CliRunner, creds: Path) -> None:
    other = pki.create_ca(new_fabric_id())
    result = runner.invoke(
        cli_app.app,
        [
            "login",
            "--ca",
            str(ca_file()),
            "--fingerprint",
            pki.cert_fingerprint(other.cert),
            "-u",
            "a",
            "--password-stdin",
        ],
        input=PASSWORD + "\n",
    )
    assert result.exit_code == 3  # V0.8 G6: AUTH (was FAILURE)
    assert "nothing was sent" in result.output


# ------------------------------------------------------------ pinning


@pytest.mark.security
def test_pinned_ca_keeps_only_the_fingerprinted_issuer() -> None:
    ca = fabric_ca()
    server = pki.issue_server_cert(ca, hosts=["127.0.0.1"]).cert
    full = pki.cert_fingerprint(ca.cert)
    assert pinned_ca([server, ca.cert], full, require_full=True) == ca.cert
    # The short (join-code) form is not enough for a pin that lasts.
    assert pinned_ca([server, ca.cert], full[:32], require_full=True) is None
    # Another CA with the right fingerprint cannot exist; a wrong one fails.
    other = pki.create_ca(new_fabric_id())
    assert (
        pinned_ca(
            [server, other.cert], pki.cert_fingerprint(other.cert), require_full=True
        )
        is None
    )  # the presented CA did not issue the server cert
    attacker = pki.issue_server_cert(other, hosts=["127.0.0.1"]).cert
    assert pinned_ca([attacker, ca.cert], full, require_full=True) is None
    assert pinned_ca([], full, require_full=True) is None
    assert pinned_ca([server], full, require_full=True) is None


def test_password_stdin_needs_a_user_name(runner: CliRunner, creds: Path) -> None:
    result = runner.invoke(
        cli_app.app,
        ["login", "--ca", str(ca_file()), "--password-stdin"],
        input=PASSWORD + "\n",
    )
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert PASSWORD not in result.output
