# ruff: noqa: T201
"""V0.7 G6 tester r3: wheel URLs over https with REAL uv - with and without
``--hash``, userinfo, and an https URL that redirects to plain http.

The redirect case is an observation about D163 ("a wheel fetched over plain
http could be replaced on the way"): the gate only reads the line.
"""

from __future__ import annotations

import hashlib
import http.server
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from mf_envs_support import make_wheel
from test_v07_g6_tester_r2_requirements import _self_signed

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

if not default_uv_argv():
    pytest.skip("the uv wheel is not installed (agent extra)", allow_module_level=True)

WHEEL_NAME = "mfdemo-1.0-py3-none-any.whl"


class Hits:
    def __init__(self) -> None:
        self.https: list[tuple[str, str | None]] = []
        self.http: list[str] = []


@pytest.fixture
def servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str, Path, Hits]]:
    wheel = make_wheel(tmp_path / "www", "mfdemo", "1.0")
    hits = Hits()

    class Plain(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            hits.http.append(self.path)
            body = wheel.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    plain = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Plain)
    threading.Thread(target=plain.serve_forever, daemon=True).start()

    class Secure(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            hits.https.append((self.path, self.headers.get("Authorization")))
            if self.path.startswith("/redirect/"):
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{plain.server_port}/{WHEEL_NAME}"
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = wheel.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    ca, cert, key = _self_signed(tmp_path / "tls")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    secure = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Secure)
    secure.socket = context.wrap_socket(secure.socket, server_side=True)
    threading.Thread(target=secure.serve_forever, daemon=True).start()
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    yield (
        f"https://127.0.0.1:{secure.server_port}",
        f"http://127.0.0.1:{plain.server_port}",
        wheel,
        hits,
    )
    for srv in (plain, secure):
        srv.shutdown()
        srv.server_close()


async def _install(tmp_path: Path, text: str, tag: str) -> str:
    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / f"envs-{tag}",
            free_disk_floor_bytes=0,
            no_index=True,
            only_binary=True,
        )
    )
    out: list[bytes] = []

    async def sink(_stream: str, data: bytes) -> None:
        out.append(data)

    try:
        built = await builder.ensure_target(
            requirements=text.encode(), deadline=1e18, on_output=sink
        )
    except EnvError as exc:
        return f"{exc.reason}: {exc} | {b''.join(out).decode(errors='replace')[-1500:]}"
    return f"installed:{sorted(p.name for p in built.path.iterdir())}"


async def test_https_wheel_installs_with_and_without_hash_and_wrong_hash_fails(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    secure, _plain, wheel, hits = servers
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    # fix round 3 (D164 T-18): an unhashed wheel URL is refused at the gate now,
    # so this line carries the hash too (assertion unchanged); the refusal is
    # tests/component/envs/test_v07_g6_fix3_wheel_url_real_uv.py.
    plain_install = await _install(
        tmp_path, f"mfdemo @ {secure}/{WHEEL_NAME} --hash=sha256:{digest}\n", "a"
    )
    assert plain_install.startswith("installed:"), plain_install
    hashed = await _install(
        tmp_path, f"mfdemo @ {secure}/{WHEEL_NAME} --hash=sha256:{digest}\n", "b"
    )
    assert hashed.startswith("installed:"), hashed
    bad = await _install(
        tmp_path, f"mfdemo @ {secure}/{WHEEL_NAME} --hash=sha256:{'0' * 64}\n", "c"
    )
    assert not bad.startswith("installed:"), bad
    assert hits.http == []


async def test_userinfo_in_an_https_wheel_url_is_accepted_and_sent_as_basic_auth(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    secure, _plain, wheel, hits = servers
    url = secure.replace("https://", "https://alice:s3cret@")
    # fix round 3 (D164 T-18): the hash was added (a wheel URL must carry it)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    got = await _install(
        tmp_path, f"mfdemo @ {url}/{WHEEL_NAME} --hash=sha256:{digest}\n", "u"
    )
    print("userinfo outcome:", got, hits.https)
    assert got.startswith("installed:"), got
    assert any(auth and auth.startswith("Basic ") for _p, auth in hits.https)


async def test_an_https_url_that_redirects_to_plain_http_is_not_followed(
    tmp_path: Path, servers: tuple[str, str, Path, Hits]
) -> None:
    """D163: a wheel fetched over plain http could be replaced on the way. The
    gate reads only the line; this asks what the installer does."""
    secure, _plain, _wheel, hits = servers
    got = await _install(tmp_path, f"mfdemo @ {secure}/redirect/{WHEEL_NAME}\n", "r")
    print("redirect outcome:", got, "plain-http hits:", hits.http)
    assert hits.http == [], f"uv followed https -> http and fetched {hits.http}: {got}"
