"""Test support for the lab kit - used by ``tests/unit/lab/test_fabric_*.py`` only.

* :class:`StubController` - a real ``http.server`` on ``127.0.0.1:0`` serving the
  ``/healthz`` and ``/v1`` shapes the kit reads, from mutable in-memory state.
  :meth:`StubController.stop` really closes the listening socket (the next request
  is refused by the OS) and :meth:`StubController.start` binds the same port again,
  so "the controller went away" is exercised for real, not by a mock that raises.
* :class:`FakeClock` - monotonic + wall time that only moves in ``sleep``, which
  also fires scheduled scenario steps; no test ever really sleeps.
* :class:`ScriptedOperator` - presses "Enter" at scripted offsets after a prompt.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

T0 = 1000.0
WALL0 = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    now: float = T0
    start_wall: datetime = WALL0
    events: list[tuple[float, int, Callable[[], object]]] = field(default_factory=list)
    _seq: int = 0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return self.start_wall + timedelta(seconds=self.now - T0)

    def at(self, t: float, action: Callable[[], object]) -> None:
        """Run ``action`` once the clock reaches absolute monotonic time ``t``."""
        self._seq += 1
        self.events.append((t, self._seq, action))
        self.events.sort(key=lambda e: (e[0], e[1]))

    def after(self, dt: float, action: Callable[[], object]) -> None:
        self.at(self.now + dt, action)

    def sleep(self, seconds: float) -> None:
        self.now = round(self.now + max(seconds, 0.0), 6)
        while self.events and self.events[0][0] <= self.now + 1e-9:
            _, _, action = self.events.pop(0)
            action()


def step(
    clock: FakeClock,
    changes: list[tuple[float, Callable[[], object]]],
    enter: float | None,
) -> Callable[[], float | None]:
    """A :class:`ScriptedOperator` script entry: at the prompt, schedule each
    ``(delay, action)`` relative to now, and press Enter ``enter`` s later."""

    def run() -> float | None:
        for delay, action in changes:
            clock.after(delay, action)
        return enter

    return run


@dataclass
class ScriptedOperator:
    """``script[i]`` runs at the i-th prompt and returns the Enter delay (s) or None."""

    clock: FakeClock
    script: list[Callable[[], float | None]]
    prompts: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    _mark_at: float | None = None

    def prompt(self, text: str) -> None:
        self.prompts.append(text)
        index = len(self.prompts) - 1
        delay = self.script[index]() if index < len(self.script) else None
        self._mark_at = None if delay is None else self.clock.now + delay

    def poll_mark(self) -> float | None:
        if self._mark_at is not None and self.clock.now >= self._mark_at - 1e-9:
            mark, self._mark_at = self._mark_at, None
            return mark
        return None

    def say(self, text: str) -> None:
        self.said.append(text)


def node_doc(
    node_id: str,
    hostname: str,
    *,
    state: str = "online",
    connected: bool = True,
    epoch: int = 1,
    session_id: str | None = None,
    hardware: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "hostname": hostname,
        "os": "linux",
        "platform": "Linux-6.8-x86_64",
        "state": state,
        "connected": connected,
        "last_seen_at": "2026-09-24T10:00:00+00:00",
        "sample_age_s": 0.5,
        "first_seen_at": "2026-09-20T10:00:00+00:00",
        "session_epoch": epoch,
        "last_session_id": session_id or f"sess_{hostname}_{epoch}",
        "agent_version": "0.1.0.dev0",
        "hardware": hardware or {},
    }


class StubController:
    """Serves ``nodes``/``sessions``/``samples`` (guarded by ``lock``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.nodes: dict[str, dict[str, Any]] = {}
        self.sessions: list[dict[str, Any]] = []
        self.samples: dict[str, list[dict[str, Any]]] = {}
        self.requests = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # -------------------------------------------------------------- server
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> StubController:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # keep test output clean
                return

            def do_GET(self) -> None:
                status, body = stub.answer(self.path)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self) -> None:
        """Close the listening socket for real: new connections are refused."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------- answers
    def answer(self, raw_path: str) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(raw_path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        path = parts.path.rstrip("/")
        with self.lock:
            self.requests += 1
            if path == "/healthz":
                return 200, {
                    "status": "ok",
                    "sessions": sum(n["connected"] for n in self.nodes.values()),
                }
            if path == "/v1/nodes":
                nodes = [dict(self.nodes[k]) for k in sorted(self.nodes)]
                return 200, {"nodes": nodes, "count": len(nodes)}
            if path == "/v1/sessions":
                rows = [
                    dict(s)
                    for s in self.sessions
                    if "node_id" not in query or s["node_id"] == query["node_id"]
                ]
                rows = rows[-int(query.get("limit", 50)) :]
                return 200, {"sessions": rows, "count": len(rows)}
            if path.startswith("/v1/nodes/"):
                rest = path[len("/v1/nodes/") :]
                node_id, _, tail = rest.partition("/")
                if node_id not in self.nodes:
                    return 404, {
                        "error": {"code": "not_found", "message": "node not found"}
                    }
                if tail == "resources":
                    live = self.samples.get(node_id, [])[
                        -int(query.get("live_limit", 30)) :
                    ]
                    return 200, {
                        "node_id": node_id,
                        "history": [],
                        "history_count": 0,
                        "live": [dict(s) for s in live],
                        "live_count": len(live),
                    }
                return 200, dict(self.nodes[node_id])
        return 404, {"error": {"code": "not_found", "message": "no route"}}

    # ------------------------------------------------------------ scenario
    def set_node(self, node_id: str, **fields: Any) -> None:
        with self.lock:
            self.nodes[node_id].update(fields)

    def add_samples(
        self, node_id: str, epoch: int, sequences: range | list[int]
    ) -> None:
        with self.lock:
            ring = self.samples.setdefault(node_id, [])
            ring.extend(
                {"sequence": s, "session_epoch": epoch, "age_s": 0.1} for s in sequences
            )

    def add_session(
        self,
        session_id: str,
        node_id: str,
        *,
        epoch: int = 1,
        ended_at: str | None = None,
        end_reason: str | None = None,
    ) -> None:
        with self.lock:
            self.sessions.append(
                {
                    "session_id": session_id,
                    "node_id": node_id,
                    "session_epoch": epoch,
                    "started_at": "2026-09-24T09:00:00+00:00",
                    "ended_at": ended_at,
                    "end_reason": end_reason,
                    "connected": ended_at is None,
                }
            )

    def end_session(self, session_id: str, *, ended_at: str, reason: str) -> None:
        with self.lock:
            for s in self.sessions:
                if s["session_id"] == session_id:
                    s.update(ended_at=ended_at, end_reason=reason, connected=False)
