# ruff: noqa: PT017, T201  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-5): attack the wheels-only requirements gate with new
lines, on the normaliser and with the REAL uv (build code must never run).

The accepted set is also checked: legitimate specs (extras, markers, ranges,
``--hash``, a wheel URL) still pass, and ``only_binary = false`` still works.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements

pytestmark = [pytest.mark.component]

H = "a" * 64

REFUSED = {
    "no-binary": "--no-binary mfsrc\nmfsrc==1.0\n",
    "no-binary-eq": "--no-binary=mfsrc\nmfsrc==1.0\n",
    "only-binary-none": "--only-binary :none:\nmfsrc==1.0\n",
    "only-binary-none-eq": "--only-binary=:none:\nmfsrc==1.0\n",
    "r": "-r other.txt\n",
    "r-glued": "-rother.txt\nx==1\n",
    "c": "-c cons.txt\nmfsrc==1\n",
    "e": "-e .\n",
    "e-git": "-e git+https://x/y.git#egg=y\n",
    "index-url": "--index-url http://evil/simple\nx==1\n",
    "i": "-i http://evil/simple\nx==1\n",
    "extra-index-url": "--extra-index-url http://evil\nx==1\n",
    "find-links": "--find-links /tmp\nx==1\n",
    "f": "-f /tmp\nx==1\n",
    "trusted-host": "--trusted-host evil\nx==1\n",
    "pre": "--pre\nx==1\n",
    "config-settings": "--config-settings foo=bar\nx==1\n",
    "no-index": "--no-index\nx==1\n",
    "prefer-binary": "--prefer-binary\nx==1\n",
    "after-req": "x==1 --no-binary x\n",
    "after-hash": f"x==1 --hash=sha256:{H} --no-binary x\n",
    "after-hash-space": f"x==1 --hash sha256:{H} --no-binary\n",
    "continuation-opt": "x==1 \\\n --no-binary x\n",
    "continuation-split-flag": "--no-\\\nbinary x\nx==1\n",
    "leading-ws": "  --no-binary x\nx==1\n",
    "leading-tab": "\t--no-binary x\nx==1\n",
    "bom": "\ufeff--no-binary x\nx==1\n",
    "crlf": "--no-binary x\r\nx==1\r\n",
    "cr-only": "x==1\r--no-binary x\r",
    "nbsp": "\u00a0--no-binary x\nx==1\n",
    "vt": "\x0b--no-binary x\n",
    "file-url": "pkg @ file:///tmp/pkg.tar.gz\n",
    "file-whl": "pkg @ file:///tmp/pkg-1-py3-none-any.whl\n",
    "git+https": "pkg @ git+https://x/y.git\n",
    "http-sdist": "pkg @ http://x/pkg-1.tar.gz\n",
    "sdist-with-whl-query": "pkg @ https://x/a.tar.gz?f=.whl\n",
    "sdist-with-whl-fragment": "pkg @ https://x/a.tar.gz#.whl\n",
    "whl-dir-then-sdist": "pkg @ https://x/a.whl/b.tar.gz\n",
    "bare-sdist-url": "https://x/pkg-1.tar.gz\n",
    "bare-path": "./local\n",
    "dot": ".\n",
    "abs": "/tmp/x\n",
    "windows-path": "C:\\x\\pkg.tar.gz\n",
    "ftp": "pkg @ ftp://x/a.tar.gz\n",
    "url-in-marker": "pkg; extra == 'https://x'\n",
    "double-dash": "--\nx==1\n",
    "trailing-double-dash": "x==1 --\n",
    "install-option": "x==1 --install-option=foo\n",
}

ACCEPTED = {
    "extras": "requests[socks,security]>=2,<3; python_version>'3.8'\n",
    "markers": "numpy>=1.2; sys_platform == 'linux'\n",
    "ranges": "a>=1,<2\nb~=1.4\nc===1.0\nd!=1\n",
    "hash": f"x==1 --hash=sha256:{H}\n",
    "hash-space": f"x==1 --hash sha256:{H}\n",
    "two-hashes-continued": f"x==1 \\\n --hash=sha256:{H} \\\n --hash=sha256:{'b' * 64}\n",
    "comment": "x==1 # --no-binary x\n",
    "comment-line": "# --no-binary x\nx==1\n",
    "wheel-url": "pkg @ https://files.example/pkg-1-py3-none-any.whl\n",
    "marker-with-dashes": "pkg; platform_machine != '--x'\n",
    "local-version": "x==1+local-1\n",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused_lines(name: str) -> None:
    with pytest.raises(EnvError) as exc:
        normalize_requirements(REFUSED[name].encode(), wheels_only=True)
    assert exc.value.reason == "requirements_rejected"
    assert "line" in str(exc.value)


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_legitimate_lines_still_pass(name: str) -> None:
    assert normalize_requirements(ACCEPTED[name].encode(), wheels_only=True)
    # and the escape hatch node accepts everything the wheels-only node does
    assert normalize_requirements(
        ACCEPTED[name].encode(), wheels_only=False
    ) == normalize_requirements(ACCEPTED[name].encode(), wheels_only=True)


@pytest.mark.parametrize("name", ["no-binary", "r", "index-url", "find-links"])
def test_with_the_escape_hatch_off_the_gate_is_open_as_before(name: str) -> None:
    # only_binary = false is the OWNER's choice: the old behaviour (which
    # already refuses -r/-c/-e and local paths) is unchanged
    text = REFUSED[name].encode()
    if name == "no-binary":
        assert normalize_requirements(text, wheels_only=False)
    else:  # these lines were refused (or not) before D162 exactly as now
        try:
            normalize_requirements(text, wheels_only=False)
        except EnvError as exc:
            assert exc.reason == "requirements_rejected"


def test_a_refusal_never_echoes_more_than_the_line_and_is_bounded() -> None:
    big = "--no-binary " + "x" * 10_000 + "\n"
    with pytest.raises(EnvError) as exc:
        normalize_requirements(big.encode(), wheels_only=True)
    assert len(str(exc.value)) < 600


# ------------------------------------------------------------ real uv

if not default_uv_argv():
    pytest.skip("the uv wheel is not installed (agent extra)", allow_module_level=True)

import test_v07_g6_only_binary_real as base  # noqa: E402
from mf_envs_support import make_wheel  # noqa: E402


def _self_signed(folder: Path) -> tuple[Path, Path, Path]:
    """A one-day CA and a certificate for 127.0.0.1 (fix round 2: wheel URLs
    are https only, D163, so the tester's server speaks TLS and uv trusts the
    CA by SSL_CERT_FILE). Returns (ca cert, server cert, server key)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)

    def build(
        subject: str,
        key: ec.EllipticCurvePrivateKey,
        issuer: str,
        issuer_key: ec.EllipticCurvePrivateKey,
        *,
        ca: bool,
    ) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.BasicConstraints(ca=ca, path_length=None), critical=True
            )
        )
        if not ca:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = build("tester r2 test CA", ca_key, "tester r2 test CA", ca_key, ca=True)
    cert = build("127.0.0.1", key, "tester r2 test CA", ca_key, ca=False)
    folder.mkdir(parents=True, exist_ok=True)
    ca_path, cert_path, key_path = (
        folder / "ca.pem",
        folder / "cert.pem",
        folder / "key.pem",
    )
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path


@pytest.fixture
def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path, Path]]:
    """An HTTPS server that serves the SDIST bytes for every path ending in the
    sdist name or '.whl' tricks, and the real wheel for the wheel path."""
    folder = tmp_path / "www"
    marker = tmp_path / "marker.txt"
    sdist = base._make_sdist(folder, marker)
    wheel = make_wheel(folder, "mfdemo", "1.0")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            body = (
                wheel.read_bytes()
                if self.path.endswith("mfdemo-1.0-py3-none-any.whl")
                else sdist.read_bytes()
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    import ssl

    ca_path, cert_path, key_path = _self_signed(tmp_path / "tls")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))  # the tool allowlist keeps it
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"https://127.0.0.1:{httpd.server_port}", marker, sdist
    httpd.shutdown()
    httpd.server_close()


def _builder(root: Path, wheels: Path, *, only_binary: bool = True) -> EnvBuilder:
    return EnvBuilder(
        EnvsConfig(
            root=root,
            free_disk_floor_bytes=0,
            find_links=wheels,
            no_index=True,
            only_binary=only_binary,
        )
    )


async def _try(tmp_path: Path, wheels: Path, text: str, tag: str) -> str | None:
    builder = _builder(tmp_path / f"envs-{tag}", wheels)
    out: list[bytes] = []

    async def sink(_stream: str, data: bytes) -> None:
        out.append(data)

    try:
        built = await builder.ensure_target(
            requirements=text.encode(), deadline=1e18, on_output=sink
        )
    except EnvError as exc:
        return f"{exc.reason}: {exc} | {b''.join(out).decode(errors='replace')[-600:]}"
    return f"installed:{sorted(p.name for p in built.path.iterdir())}"


@pytest.mark.asyncio
async def test_no_accepted_line_runs_build_code_on_real_uv(
    tmp_path: Path, server: tuple[str, Path, Path]
) -> None:
    base_url, marker, sdist = server
    digest = hashlib.sha256(sdist.read_bytes()).hexdigest()
    wheels = tmp_path / "wheels"
    base._make_sdist(wheels, marker)
    lines = {
        # accepted by the gate (path ends in .whl) but the bytes are an sdist
        "hidden-dotwhl": f"mfsrc @ {base_url}/mfsrc-1.0.tar.gz/.whl\n",
        "params": f"mfsrc @ {base_url}/mfsrc-1.0.tar.gz;x.whl\n",
        "pct": f"mfsrc @ {base_url}/mfsrc-1.0.tar.gz%2F.whl\n",
        "upper": f"mfsrc @ {base_url.upper()}/mfsrc-1.0.tar.gz/A.WHL\n",
        "dotdot": f"mfsrc @ {base_url}/a.whl/../mfsrc-1.0.tar.gz\n",
        "named-sdist-with-hash": f"mfsrc==1.0 --hash=sha256:{digest}\n",
        "wheel-named-like-sdist": f"mfsrc @ {base_url}/mfsrc-1.0-py3-none-any.whl\n",
        "bare-name": "mfsrc\n",
        "bare-name-marker": "mfsrc ; python_version >= '3'\n",
    }
    outcomes: dict[str, str | None] = {}
    for tag, text in lines.items():
        outcomes[tag] = await _try(tmp_path, wheels, text, tag)
        assert not marker.exists(), f"build code ran for {tag}: {text!r}"
    print(outcomes)


@pytest.mark.asyncio
async def test_a_wheel_url_and_a_hashed_wheel_still_install(
    tmp_path: Path, server: tuple[str, Path, Path]
) -> None:
    base_url, marker, _sdist = server
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "mfdemo", "1.0")
    got = await _try(
        tmp_path, wheels, f"mfdemo @ {base_url}/mfdemo-1.0-py3-none-any.whl\n", "url"
    )
    assert got is not None and got.startswith("installed:"), got
    whl = wheels / "mfdemo-1.0-py3-none-any.whl"
    h = hashlib.sha256(whl.read_bytes()).hexdigest()
    got = await _try(tmp_path, wheels, f"mfdemo==1.0 --hash=sha256:{h}\n", "hash")
    assert got is not None and got.startswith("installed:"), got
    got = await _try(tmp_path, wheels, "mfdemo>=0.5,<2; python_version>'3.8'\n", "r")
    assert got is not None and got.startswith("installed:"), got
    assert not marker.exists()


@pytest.mark.asyncio
async def test_the_escape_hatch_still_builds(
    tmp_path: Path, server: tuple[str, Path, Path]
) -> None:
    _url, marker, _sdist = server
    wheels = tmp_path / "wheels"
    base._make_sdist(wheels, marker)
    builder = _builder(tmp_path / "envs-hatch", wheels, only_binary=False)
    with contextlib.suppress(EnvError):
        await builder.ensure_target(
            requirements=b"--no-binary mfsrc\nmfsrc==1.0\n", deadline=1e18
        )
    assert marker.exists(), "with only_binary=false the owner's hatch must work"
