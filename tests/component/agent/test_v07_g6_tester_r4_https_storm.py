"""V0.7 G6 tester r4: cancel storm on a REAL completing TLS handshake.

Existing exit-list tests cancel a plain-HTTP connect or a silent TLS server.
This one runs a real TLS server (self-signed cert) so the handshake can finish
exactly as the cancel lands, with many requests in flight at once, and counts
open sockets by file descriptor (not by ResourceWarning) after the storm.
Runs in a child process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="/proc fds, POSIX"),
]

_PROBE = textwrap.dedent(
    """
    import asyncio, datetime, json, os, random, ssl, sys, tempfile, gc
    import httpx
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from meshforge.agent import http_connect

    MODE, ROUNDS, CONC = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = d + "/c.pem", d + "/k.pem"
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); sctx.load_cert_chain(cp, kp)

    def cctx():
        c = ssl.create_default_context(); c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE; return c

    def transport():
        if MODE == "safe":
            return http_connect.cancel_safe_transport(cctx())
        return httpx.AsyncHTTPTransport(verify=cctx())

    def fds():
        n = 0
        for f in os.listdir("/proc/self/fd"):
            try:
                if os.readlink("/proc/self/fd/" + f).startswith("socket:"):
                    n += 1
            except OSError:
                pass
        return n

    async def answer(reader, writer):
        try:
            await reader.read(4096)
            writer.write(b"HTTP/1.1 200 OK\\r\\ncontent-length: 2\\r\\n\\r\\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def main():
        server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=sctx)
        port = server.sockets[0].getsockname()[1]
        base = fds()
        rnd = random.Random(7)
        ok = cancelled = 0
        async def one():
            async with httpx.AsyncClient(transport=transport(), timeout=3) as c:
                r = await c.get(f"https://127.0.0.1:{port}/")
                return r.status_code
        for _ in range(ROUNDS):
            tasks = [asyncio.ensure_future(one()) for _ in range(CONC)]
            await asyncio.sleep(rnd.random() * 0.01)
            for t in tasks:
                if rnd.random() < 0.7:
                    for _ in range(rnd.randint(0, 5)):
                        await asyncio.sleep(0)
                    t.cancel()
            for t in tasks:
                try:
                    if await t == 200: ok += 1
                except asyncio.CancelledError:
                    cancelled += 1
                except Exception:
                    pass
        for _ in range(200):
            if not getattr(http_connect, "_ORPHANS", None):
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        pending = len(getattr(http_connect, "_ORPHANS", ()))
        server.close()
        await asyncio.sleep(0.2)
        return ok, cancelled, pending, fds() - base

    ok, cancelled, pending, leaked = asyncio.run(main())
    gc.collect()
    print(json.dumps({"ok": ok, "cancelled": cancelled, "pending": pending, "leaked_fds": leaked}))
    """
)


def _run(mode: str, rounds: int, conc: int) -> dict[str, int]:
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", _PROBE, mode, str(rounds), str(conc)],
        capture_output=True,
        text=True,
        timeout=250,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    result: dict[str, int] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_https_cancel_storm_leaks_no_socket_and_requests_still_succeed() -> None:
    result = _run("safe", 150, 20)
    assert result["leaked_fds"] <= 0, result
    assert result["pending"] == 0, result
    assert result["ok"] > 0, result  # the storm did not just cancel everything
    assert result["cancelled"] > 0, result


def test_control_default_transport_leaks_under_the_same_storm() -> None:
    """The storm reaches the race: without the fix sockets are left open."""
    assert _run("default", 25, 20)["leaked_fds"] > 0
