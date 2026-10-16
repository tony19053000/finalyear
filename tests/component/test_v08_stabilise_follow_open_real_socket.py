"""D156 stabilisation: the real ``websockets`` asyncio client against a real
socket that accepts the connection and never answers the HTTP upgrade (a
controller whose loop is stuck). ``compute``'s follow must give up after
``open_timeout_s`` - not hang, not retry silently - with a typed ``timeout``
that names the budget."""

from __future__ import annotations

import io
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from meshforge.cli.client import ControllerError
from meshforge.cli.follow import follow_logs
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.component, pytest.mark.timeout(30)]


@pytest.fixture
def silent_server() -> Iterator[int]:
    """Accepts TCP connections, reads whatever comes, never answers."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    held: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            held.append(conn)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()
        for conn in held:
            conn.close()


def test_a_controller_that_never_answers_the_upgrade_times_out_typed(
    silent_server: int,
) -> None:
    started = time.monotonic()
    with pytest.raises(ControllerError) as caught:
        follow_logs(
            f"ws://127.0.0.1:{silent_server}/v1/jobs/job_x/logs/ws?stream=both",
            auth_header="Bearer t",
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=0.5,
        )
    elapsed = time.monotonic() - started
    assert caught.value.failure_type is FailureType.TIMEOUT
    assert "did not open within 0.5s" in caught.value.message
    assert 0.4 <= elapsed < 10.0, elapsed
