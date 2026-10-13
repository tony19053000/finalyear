# ruff: noqa: T201  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-5): new requirement lines on the REAL Docker pip path.

An HTTP server on the docker bridge gateway serves the SDIST bytes under
URLs that the gate accepts as "a .whl URL" (path tricks); real pip 25 in the
python image must not build them. Control: the same server hands out the real
wheel under its real name and that installs.
"""

from __future__ import annotations

import http.server
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_v07_g6_only_binary_real as base
from mf_envs_support import make_wheel
from test_v07_g6_fix1_only_binary_docker import (  # noqa: F401
    _containers,
    _job,
    _run,
    _runtime,
    env_images,
    image,
    label,
    package,
    wheelhouse,
)

from meshforge.agent.envs.errors import EnvError  # noqa: F401

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(400),
]


def _gateway() -> str:
    out = subprocess.run(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    gw = out.stdout.strip()
    if not gw:
        pytest.skip("no docker bridge gateway")
    return gw


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    folder = tmp_path / "www"
    marker = Path("/tmp/mf-build-marker.txt")  # inside the build container
    sdist = base._make_sdist(folder, marker)
    wheel = make_wheel(folder, "mfdemo", "1.0")
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            hits.append(self.path)
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

    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)  # noqa: S104
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://{_gateway()}:{httpd.server_port}", folder
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    "tail",
    [
        "mfsrc-1.0.tar.gz/.whl",
        "mfsrc-1.0.tar.gz;x.whl",
        "mfsrc-1.0.tar.gz%2F.whl",
        "x.whl%00.tar.gz",
    ],
)
async def test_docker_pip_never_builds_a_url_that_only_looks_like_a_wheel(
    tmp_path: Path,
    image: str,  # noqa: F811
    wheelhouse: Path,  # noqa: F811
    label: str,  # noqa: F811
    env_images: list[str],  # noqa: F811
    server: tuple[str, Path],
    tail: str,
) -> None:
    url, _ = server
    runtime = _runtime(tmp_path, wheelhouse, label, only_binary=True)
    result, log = await _run(
        runtime, _job(tmp_path, image, f"mfsrc @ {url}/{tail}\n", "mfsrc")
    )
    env_images.append(result.env_hash or "")
    text = log.text() + str(result.detail)
    print("OUTCOME", tail, result.ok, result.reason, text[-300:].replace("\n", " / "))
    assert "IMPORTED True" not in text, text  # SOURCE_BUILT: build code ran
    assert "Building wheel" not in text, text
    assert not result.ok or "IMPORTED" not in text
    assert _containers(label) == []
