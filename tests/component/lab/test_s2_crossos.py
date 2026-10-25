"""Component tests for the S2 spike (roadmap 0.2.6): real sockets, real gRPC,
real subprocesses on loopback. Everything here is deterministic and OS/speed
independent -- no test depends on this machine's real WSL/OS state, hardware,
or absolute network speed; timing budgets are generous and gaps/holds are kept
small (sub-second to a few seconds) to keep the suite fast.

Attack surfaces this file targets (tester brief for 0.2.6):
* beat scheduling: negotiated vs default interval, short/long intervals,
  beats during/after idle, interval > hold;
* ``S2GrpcServer.stop()``/``S2HttpServer.stop()`` idempotency: never-started,
  double-stop, stop-during-start, open->stop->open on the same port;
* the ``add_insecure_port`` ``RuntimeError`` -> ``OSError`` translation on a
  busy port, and that the HTTP listener is not leaked when gRPC fails to bind;
* killing the server at each phase of a client run;
* two servers/two clients at once; SIGTERM during an active bidi session;
* the server-side ``MAX_HOLD_S`` cap;
* every documented exit code, the WSL refusal + ``--allow-wsl`` override
  (via a faked environment, never this machine's real state);
* no home path / hostname secret in the report; two links/directions never
  merged; a ``TBD`` never silently upgraded.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import grpc
import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
SCRIPT: Final = LAB_DIR / "spikes" / "s2_crossos.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes import s2_crossos as cli  # noqa: E402
from spikes._harness.envelope import load_dir, validate, write_envelope  # noqa: E402
from spikes._s2 import client as probe  # noqa: E402
from spikes._s2 import grpcsrv  # noqa: E402
from spikes._s2.httpsrv import S2HttpServer  # noqa: E402
from spikes._s2.protocol import METHOD_DOWNLOAD  # noqa: E402
from spikes._s2.run import ClientConfig, run_client  # noqa: E402
from spikes._s2.server import S2Server, ServerLimits  # noqa: E402
from spikes._s2.verdict import direction_verdict  # noqa: E402

pytestmark = pytest.mark.timeout(180)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------- in-process server
@pytest.fixture
def server() -> Iterator[S2Server]:
    srv = S2Server(
        "127.0.0.1",
        0,
        0,
        ServerLimits(duration_s=120, idle_s=60, io_timeout_s=5),
        name="srv",
    )
    srv.open()
    assert srv.grpc is not None
    grpc_server = srv.grpc
    thread = srv.start_thread()
    # start_thread() only launches serve(); wait for the background thread to
    # have actually called grpc_server.start()/http.start() before handing the
    # server to a test that may itself call .stop() (see the documented
    # start()/stop() race: calling stop() before start() has really run is a
    # different, *legitimate* code path from racing it, which has its own
    # dedicated defect test below).
    deadline = time.monotonic() + 10
    while not grpc_server._started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert grpc_server._started, "server did not start in time"
    yield srv
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive(), "server thread leaked"


def _endpoint(srv: S2Server, **kw: Any) -> probe.Endpoint:
    assert srv.http is not None and srv.grpc is not None
    base: dict[str, Any] = {
        "host": "127.0.0.1",
        "http_port": srv.http.port,
        "grpc_port": srv.grpc.port,
        "connect_timeout_s": 3.0,
        "io_timeout_s": 5.0,
    }
    base.update(kw)
    return probe.Endpoint(**base)


# ====================================================================== beats
def test_beats_use_negotiated_interval_not_default(server: S2Server) -> None:
    """The fixed defect: the beat due-time used to be latched to
    ``DEFAULT_BEAT_S`` (1.0s) before ``hello`` negotiated a shorter interval,
    so a short session carried zero beats. With ``ping_interval_s=0.05`` and
    a hold well under 1.0s, at least one beat must still arrive."""
    ep = _endpoint(server)
    sample = probe.grpc_session(
        ep, keepalive="off", hold_s=0.4, ping_interval_s=0.05, idle_s=0.1
    )
    assert sample["survived"] == 1
    assert sample["beats"] >= 1, sample


def test_many_beats_with_very_short_negotiated_interval(server: S2Server) -> None:
    ep = _endpoint(server)
    sample = probe.grpc_session(
        ep, keepalive="off", hold_s=1.0, ping_interval_s=0.05, idle_s=0.1
    )
    assert sample["survived"] == 1
    # ~1.0s of hold at a 0.05s beat interval: several beats, not just one
    assert sample["beats"] >= 5, sample


def test_zero_beats_when_negotiated_interval_exceeds_hold_plus_idle(
    server: S2Server,
) -> None:
    """A beat interval longer than the whole session must simply mean no
    beat fires -- not a hang, not a crash, not a spuriously dropped session."""
    ep = _endpoint(server)
    sample = probe.grpc_session(
        ep, keepalive="off", hold_s=0.2, ping_interval_s=30.0, idle_s=0.1
    )
    assert sample["survived"] == 1
    assert sample["beats"] == 0, sample


def test_no_beats_during_the_quiet_window(server: S2Server) -> None:
    """Beats stop the moment ``go_quiet`` is acknowledged; a long idle gap
    with a short beat interval must not leak beats into the counted idle
    time (the idle test measures silence, not near-silence)."""
    ep = _endpoint(server)
    sample = probe.grpc_session(
        ep, keepalive="off", hold_s=0.2, ping_interval_s=0.05, idle_s=0.6
    )
    assert sample["survived"] == 1
    # beats only from the ~0.2s active phase, not the ~0.6s idle phase
    assert sample["beats"] <= 6, sample


# ============================================================ server cap (MAX_HOLD_S)
def test_server_hold_cap_closes_session_before_client_idle_completes(
    monkeypatch: pytest.MonkeyPatch, server: S2Server
) -> None:
    """``--idle-gaps`` close to ``MAX_HOLD_S``: the server force-closes any
    session older than its cap. Scaled down (0.5s) so the test stays fast;
    the mechanism under test (``time.monotonic() - started > MAX_HOLD_S`` in
    ``_write_responses``) is exercised identically at any scale."""
    monkeypatch.setattr(grpcsrv, "MAX_HOLD_S", 0.5)
    ep = _endpoint(server, io_timeout_s=3.0)
    sample = probe.grpc_session(
        ep, keepalive="off", hold_s=0.3, ping_interval_s=0.1, idle_s=0.6
    )
    assert sample["survived"] == 0, sample
    assert sample["failure"] is not None
    # a clean server-side end of stream is a known death time too. The cap
    # fires ~0.5 s after the session began, but the server's handler only
    # returns once its request-reader thread wakes (the client's post-gap
    # ping, 0.6 s in), so the client sees the end right after that ping -
    # and the numbers must say ~0.6 s, never 0.6 s + the 3 s io timeout the
    # old bookkeeping added when the waiter gave up.
    assert sample["alive_s"] is not None and sample["alive_s"] < 1.5, sample
    assert sample["idle_survived_s"] is not None, sample
    assert 0.0 <= sample["idle_survived_s"] <= 0.6, sample


# ================================================= S2GrpcServer.stop() contract
def test_grpc_server_stop_without_start_does_not_hang_and_frees_port() -> None:
    counters_log: list[str] = []
    srv = grpcsrv.S2GrpcServer(
        "127.0.0.1",
        0,
        name="x",
        log=counters_log.append,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    port = srv.port
    started = time.monotonic()
    srv.stop()
    assert time.monotonic() - started < 15
    # idempotent
    srv.stop()
    # the port must be free again
    with socket.socket() as probe_sock:
        probe_sock.bind(("127.0.0.1", port))


def test_grpc_server_stop_is_idempotent_after_start() -> None:
    srv = grpcsrv.S2GrpcServer(
        "127.0.0.1",
        0,
        name="x",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    srv.start()
    srv.stop()
    srv.stop()  # must not raise or hang


def test_grpc_server_stop_during_start_is_safe() -> None:
    """(Was a defect.) ``S2GrpcServer.stop()`` is documented "Idempotent" and
    must tolerate being called from another thread while a legitimate
    ``start()`` is in flight (the nominated "stop-during-start" attack
    surface). ``_started``/``_stopped`` used to be plain booleans with no
    lock: a caller's ``start()`` racing ``stop()``'s own internal
    ``self.start()`` (called to "start-to-stop" a never-started server) let
    both observe ``self._started is False`` and both call the underlying
    ``grpc.Server.start()`` concurrently, which grpcio forbids -
    ``ValueError("Cannot start already-started server!")`` propagated out of
    ``stop()``. Both are now serialised by a lock, so neither call may raise,
    in either order. Reproduced reliably before the fix (no sleep between
    spawning the racing thread and calling ``stop()``); if grpcio internals
    ever make this non-deterministic, run the whole suite with
    ``pytest --count=20`` (or a shell loop) to catch it.
    """
    failures: list[BaseException] = []
    for _ in range(5):
        srv = grpcsrv.S2GrpcServer(
            "127.0.0.1",
            0,
            name="x",
            log=lambda _m: None,
            touch=lambda: None,
            note_error=lambda _m: None,
        )
        errors: list[BaseException] = []

        def _start(
            srv: grpcsrv.S2GrpcServer = srv, errors: list[BaseException] = errors
        ) -> None:
            try:
                srv.start()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_start)
        t.start()
        try:
            srv.stop()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        t.join(timeout=10)
        assert not t.is_alive()
        failures += errors
    assert not failures, (
        f"start()/stop() raised during a race in "
        f"{len(failures)} of 5 attempts: {failures[0]!r} -- see docstring above"
    )


def test_grpc_server_open_stop_open_reuses_the_same_port() -> None:
    port = _free_port()
    first = grpcsrv.S2GrpcServer(
        "127.0.0.1",
        port,
        name="a",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    assert first.port == port
    first.stop()
    second = grpcsrv.S2GrpcServer(
        "127.0.0.1",
        port,
        name="b",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    assert second.port == port
    second.stop()


def test_grpc_server_busy_port_raises_oserror_not_runtimeerror() -> None:
    """grpcio 1.84 raises ``RuntimeError`` on a taken address; the fixed bug
    (item 4) translates it to ``OSError`` so ``cmd_serve`` prints a clean
    message instead of a traceback. A *plain* listening socket (no
    ``SO_REUSEPORT``, like grpc's own listeners now that ``grpc.so_reuseport``
    is 0) is what conflicts and exercises this path."""
    port = _free_port()
    with socket.socket() as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        with pytest.raises(OSError) as excinfo:
            grpcsrv.S2GrpcServer(
                "127.0.0.1",
                port,
                name="dup",
                log=lambda _m: None,
                touch=lambda: None,
                note_error=lambda _m: None,
            )
        assert not isinstance(excinfo.value, RuntimeError)


def test_second_grpc_server_on_a_live_port_is_refused() -> None:
    """(Was a defect.) grpc's own listening sockets use ``SO_REUSEPORT`` by
    default: a *second*, fully independent ``S2GrpcServer`` bound to a port an
    earlier one was already using -- the exact "operator forgot to stop the
    previous `serve`" scenario -- used to bind and start **without any
    error**, after which the kernel load-balanced incoming connections between
    the two unrelated server objects, i.e. a silently split, non-reproducible
    lab result. ``server_options()`` now sets ``grpc.so_reuseport = 0``, so
    the second bind fails and ``_bind`` reports it as the documented "cannot
    listen" ``OSError`` (S2.md exit code ``1``).
    """
    port = _free_port()
    first = grpcsrv.S2GrpcServer(
        "127.0.0.1", port, name="first",
        log=lambda _m: None, touch=lambda: None, note_error=lambda _m: None,
    )  # fmt: skip
    first.start()
    try:
        with pytest.raises(OSError) as excinfo:
            grpcsrv.S2GrpcServer(
                "127.0.0.1", port, name="second",
                log=lambda _m: None, touch=lambda: None, note_error=lambda _m: None,
            )  # fmt: skip
        assert not isinstance(excinfo.value, RuntimeError)
        assert "cannot listen" in str(excinfo.value)
    finally:
        first.stop()


# ================================================== S2HttpServer.stop() contract
def test_http_server_stop_without_start_does_not_hang() -> None:
    srv = S2HttpServer(
        "127.0.0.1",
        0,
        name="x",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    port = srv.port
    done = threading.Event()

    def _stop() -> None:
        srv.stop()
        done.set()

    t = threading.Thread(target=_stop, daemon=True)
    started = time.monotonic()
    t.start()
    t.join(timeout=15)
    assert done.is_set(), "S2HttpServer.stop() hung with a never-started server"
    assert time.monotonic() - started < 15
    with socket.socket() as probe_sock:
        probe_sock.bind(("127.0.0.1", port))


def test_http_server_stop_is_idempotent_and_double_stop_safe() -> None:
    srv = S2HttpServer(
        "127.0.0.1",
        0,
        name="x",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    srv.start()
    srv.stop()
    srv.stop()


def test_open_failure_on_grpc_does_not_leak_the_http_listener() -> None:
    """``S2Server.open()``: if the gRPC bind fails, the HTTP listener it
    already opened must be closed, not leaked for the process's life. Uses a
    plain (non-``SO_REUSEPORT``) socket to genuinely occupy the gRPC port,
    since that is the simplest way to occupy the port from the test process
    (two grpc listeners now conflict too -- see the dedicated test above)."""
    port = _free_port()
    with socket.socket() as blocker:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        http_port = _free_port()
        srv = S2Server("127.0.0.1", http_port, port, ServerLimits(), name="victim")
        with pytest.raises(OSError):
            srv.open()
    # the http port must be free again: a plain bind to it must now succeed
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", http_port))


# ============================================================ kill mid-phase
def _config(server: S2Server, **kw: Any) -> ClientConfig:
    assert server.http is not None and server.grpc is not None
    base: dict[str, Any] = {
        "server": "127.0.0.1",
        "http_port": server.http.port,
        "grpc_port": server.grpc.port,
        "link": "ethernet",
        "reps": 5,
        "warmup": 0,
        "blob_bytes": 4096,
        "hold_s": 0.3,
        "ping_interval_s": 0.05,
        "idle_gaps_s": (0.2,),
        "keepalives": ("off",),
        "connect_timeout_s": 3.0,
        "io_timeout_s": 5.0,
        "pause_s": 0.0,
        "name": "killer-client",
    }
    base.update(kw)
    return ClientConfig(**base)


def test_kill_server_between_cases_skips_remaining_and_reports_diagnosis(
    server: S2Server,
) -> None:
    """Kill the server right as the second planned case starts: the run must
    finish quickly, be incomplete, and list the skipped cases with a fix."""
    seen: list[str] = []

    def log(line: str) -> None:
        seen.append(line)
        if line.startswith("grpc-unary:"):
            server.stop()
            assert server.grpc is not None
            server.grpc.stop()
            assert server.http is not None
            server.http.stop()

    started = time.monotonic()
    run = run_client(_config(server), log=log)
    assert time.monotonic() - started < 30
    assert not run.complete
    assert run.errors, "peer loss must be recorded as a run-level error"
    assert "skipped cases" in run.errors[0]["message"]
    assert run.errors[0]["fix"]


def test_kill_server_during_idle_gap_records_survived_zero(server: S2Server) -> None:
    """Killing the server *during the idle gap* (after the active hold phase
    has already completed cleanly) is exactly the scenario ``grpc_session``
    is documented to turn into a result, not an exception: "a stream that
    dies during the idle phase is *the* result... returned as a sample
    (survived 0) with the diagnosis attached, not raised"."""
    ep = _endpoint(server, io_timeout_s=8.0)
    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _run() -> None:
        try:
            result["sample"] = probe.grpc_session(
                ep, keepalive="off", hold_s=0.2, ping_interval_s=0.05, idle_s=3.0
            )
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    time.sleep(0.6)  # past the 0.2s hold phase, now inside the idle gap
    assert server.grpc is not None
    # grace 0: live RPCs are cancelled *now*, so the death time is known to
    # within the scheduler's jitter (the default 1 s grace would legitimately
    # put it ~1.4 s into the gap - correct, but a looser bound below)
    server.grpc.stop(grace_s=0.0)
    assert server.http is not None
    server.http.stop()
    t.join(timeout=20)
    assert not t.is_alive(), "grpc_session hung after the server died"
    assert not error, error
    sample = result["sample"]
    assert sample["survived"] == 0
    assert sample["failure"] is not None
    # (Reviewer BLOCKING 2.) The stream died ~0.4 s into a 3 s gap. The
    # numbers must say *that*, not "3 s idle + 8 s io timeout" (when the
    # client finally noticed) - or be absent (None) if the death was never
    # reported, never a fabricated number.
    idle_survived = sample["idle_survived_s"]
    alive = sample["alive_s"]
    # stop(grace_s=0) cancels the RPC, so the reader sees an RpcError and the
    # death time *is* known: a None here would mean failed_at was never stamped
    assert idle_survived is not None and idle_survived < 1.0, sample
    assert alive is not None and alive < 2.0, sample


def test_still_there_check_after_hello_kill_reports_typed_error(
    server: S2Server,
) -> None:
    """Killing the server before *any* case runs (during ``hello``/http-ping)
    must record a ``hello``-phase error and run no cases at all."""
    assert server.http is not None and server.grpc is not None
    server.grpc.stop()
    server.http.stop()
    started = time.monotonic()
    run = run_client(_config(server))
    assert time.monotonic() - started < 15
    assert not run.complete
    (error,) = run.errors
    assert error["phase"] == "hello"


# =============================================================== concurrency
def test_two_servers_two_clients_at_once_do_not_cross_talk() -> None:
    server_a = S2Server("127.0.0.1", 0, 0, ServerLimits(idle_s=30), name="A")
    server_b = S2Server("127.0.0.1", 0, 0, ServerLimits(idle_s=30), name="B")
    server_a.open()
    server_b.open()
    thread_a = server_a.start_thread()
    thread_b = server_b.start_thread()
    try:
        results: dict[str, Any] = {}

        def _client(tag: str, srv: S2Server) -> None:
            run = run_client(_config(srv, name=f"client-{tag}"))
            results[tag] = run

        t1 = threading.Thread(target=_client, args=("a", server_a))
        t2 = threading.Thread(target=_client, args=("b", server_b))
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)
        assert not t1.is_alive() and not t2.is_alive()
        assert results["a"].complete and results["b"].complete
        assert results["a"].server_name == "A"
        assert results["b"].server_name == "B"
    finally:
        server_a.stop()
        server_b.stop()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)
        assert not thread_a.is_alive() and not thread_b.is_alive()


# ================================================================ CLI process
def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(SCRIPT), *args]


def _run(*args: str, env: dict[str, str] | None = None, timeout: float = 60) -> Any:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        _cmd(*args),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
        check=False,
    )


def _client_argv(
    http_port: int, grpc_port: int, out_dir: Path, *extra: str
) -> list[str]:
    return [
        "client", "--server", "127.0.0.1", "--http-port", str(http_port),
        "--grpc-port", str(grpc_port), "--link", "ethernet", "--reps", "5",
        "--warmup", "0", "--blob-bytes", "4096", "--hold", "0.3",
        "--ping-interval", "0.05", "--idle-gaps", "0.2", "--keepalive", "off",
        "--connect-timeout", "3", "--io-timeout", "5", "--pause", "0",
        "--out-dir", str(out_dir), "--no-inbound-rule", "--name", "cli-client",
        *extra,
    ]  # fmt: skip


@contextlib.contextmanager
def _serve_process(tmp: Path, name: str, *extra: str) -> Iterator[dict[str, int]]:
    ready = tmp / f"{name}.ports.json"
    log = tmp / f"{name}.log"
    argv = _cmd(
        "serve", "--bind", "127.0.0.1", "--http-port", "0", "--grpc-port", "0",
        "--name", name, "--idle", "30", "--io-timeout", "5",
        "--ready-file", str(ready), *extra,
    )  # fmt: skip
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            ports: dict[str, int] | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if ready.exists():
                    with contextlib.suppress(json.JSONDecodeError, OSError):
                        ports = json.loads(ready.read_text())
                        if ports:
                            break
                time.sleep(0.05)
            assert ports, log.read_text(encoding="utf-8", errors="replace")
            yield ports
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=15)
    assert proc.poll() is not None, "serve process leaked"


def _no_home(text: str) -> None:
    home = str(Path.home())
    assert home not in text
    user = Path.home().name
    if len(user) >= 4:
        # The hostname is kept on purpose and may contain the user name
        # (CI: user `runner`, host `runnervm...`): blank it out first.
        text = text.lower().replace(socket.gethostname().lower(), "")
        assert user not in text, user


def test_cli_serve_client_report_end_to_end(tmp_path: Path) -> None:
    out_dir = tmp_path / "raw"
    with _serve_process(tmp_path, "cli-server") as ports:
        code = cli.main(_client_argv(ports["http_port"], ports["grpc_port"], out_dir))
        assert code == cli.EXIT_OK
    docs, skipped = load_dir(out_dir)
    assert skipped == [] and len(docs) == 1
    (doc,) = docs
    validate(doc, source="test")
    assert doc["spike"] == "S2" and doc["link"] == "ethernet"
    assert doc["extra"]["complete"] is True and doc["errors"] == []
    # (Reviewer BLOCKING 3.) Not a measurement, and labelled as such.
    outbound = doc["extra"]["outbound_only"]
    assert outbound["client_listens"] is False
    assert outbound["basis"] == "by construction, not measured"
    assert "client_listeners_opened" not in outbound
    raw_text = (out_dir / str(doc["_source"])).read_text(encoding="utf-8")
    _no_home(raw_text)
    # (Reviewer BLOCKING 1.) This *is* a loopback run: the verdict must say so.
    dv = direction_verdict(doc)
    assert dv.same_host is True
    assert dv.cross_os is False
    assert dv.outbound_only.value == "TBD"
    assert "loopback" in dv.outbound_only.detail

    report = tmp_path / "S2.md"
    rc = cli.main(
        [
            "report", "--results", str(out_dir), "--out", str(report),
            "--hosts", "L1=cli-client,L2=cli-server",
        ]
    )  # fmt: skip
    assert rc == cli.EXIT_OK
    text = report.read_text(encoding="utf-8")
    _no_home(text)
    assert "S2 cross-OS reachability" in text
    assert "| L1 -> L2 |" in text
    row = next(line for line in text.splitlines() if line.startswith("| L1 -> L2 |"))
    assert "| Ethernet ⚠ same machine |" in row
    assert "TBD — loopback path crosses no firewall" in row
    assert "- **L1 -> L2**: not a cross-OS result: same machine (loopback)" in text
    assert "same OS on both sides" in text
    # the reverse direction was never run: must show TBD, never a guess
    assert "| L2 -> L1 |" in text
    tbd_row = next(
        line for line in text.splitlines() if line.startswith("| L2 -> L1 |")
    )
    assert "yes" not in tbd_row
    assert tbd_row.count("TBD") >= 4
    # determinism
    rc2 = cli.main(
        [
            "report", "--results", str(out_dir), "--out", str(report),
            "--hosts", "L1=cli-client,L2=cli-server",
        ]
    )  # fmt: skip
    assert rc2 == cli.EXIT_OK
    assert report.read_text(encoding="utf-8") == text


def test_hosts_flag_case_insensitive_and_duplicate_label_and_empty_label(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "raw"
    with _serve_process(tmp_path, "cli-server") as ports:
        code = cli.main(_client_argv(ports["http_port"], ports["grpc_port"], out_dir))
        assert code == cli.EXIT_OK
    report = tmp_path / "S2.md"
    # mixed-case host matching: real recorded name is "cli-client"/"cli-server"
    rc = cli.main(
        [
            "report", "--results", str(out_dir), "--out", str(report),
            "--hosts", "L1=CLI-CLIENT,L2=CLI-SERVER",
        ]
    )  # fmt: skip
    assert rc == cli.EXIT_OK
    text = report.read_text(encoding="utf-8")
    assert "| L1 -> L2 |" in text

    # a label colliding with a real hostname: still renders without crashing,
    # and never claims a row it did not measure.
    report2 = tmp_path / "S2b.md"
    rc2 = cli.main(
        [
            "report", "--results", str(out_dir), "--out", str(report2),
            "--hosts", "cli-client=cli-client,L2=cli-server",
        ]
    )  # fmt: skip
    assert rc2 == cli.EXIT_OK

    # empty label is rejected by the CLI parser (usage error), not silently accepted
    proc = _run(
        "report", "--results", str(out_dir), "--out", str(tmp_path / "S2c.md"),
        "--hosts", "=cli-server",
    )  # fmt: skip
    assert proc.returncode == cli.EXIT_USAGE, proc.stderr


# =============================================================== WSL refusal
def test_wsl_refusal_exit_4_with_faked_environment(tmp_path: Path) -> None:
    """Fake WSL evidence via environment variable -- never this machine's
    real /proc state (this machine is native Linux, not WSL)."""
    proc = _run(
        "serve", "--bind", "127.0.0.1", "--http-port", "0", "--grpc-port", "0",
        "--idle", "5", "--quiet",
        env={"WSL_DISTRO_NAME": "Ubuntu-Fake"},
        timeout=15,
    )  # fmt: skip
    assert proc.returncode == cli.EXIT_WSL, proc.stderr
    assert "refusing to run inside WSL" in proc.stderr


def test_allow_wsl_overrides_the_refusal_with_faked_environment(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    argv = _cmd(
        "serve", "--bind", "127.0.0.1", "--http-port", "0", "--grpc-port", "0",
        "--idle", "5", "--quiet", "--allow-wsl", "--ready-file", str(ready),
    )  # fmt: skip
    proc = subprocess.Popen(
        argv,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "WSL_DISTRO_NAME": "Ubuntu-Fake"},
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "server did not start with --allow-wsl"
    finally:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate(timeout=15)
    assert "does NOT validate ADR-0009" in err


def test_client_against_dead_server_exits_incomplete_quickly(tmp_path: Path) -> None:
    out_dir = tmp_path / "raw"
    port = _free_port()
    started = time.monotonic()
    code = cli.main(_client_argv(port, port + 1, out_dir))
    assert code == cli.EXIT_INCOMPLETE
    assert time.monotonic() - started < 20
    docs, skipped = load_dir(out_dir)
    assert skipped == [] and len(docs) == 1
    (doc,) = docs
    assert doc["cases"] == {} and doc["extra"]["complete"] is False


def test_client_usage_error_exit_2_bad_link() -> None:
    proc = _run("client", "--server", "127.0.0.1", "--link", "bluetooth")
    assert proc.returncode == cli.EXIT_USAGE, proc.stderr


def test_serve_usage_error_exit_2_wildcard_bind_without_allow_any() -> None:
    wildcard = "0.0.0.0"  # noqa: S104 - asserting the CLI *rejects* this, not binding it
    proc = _run("serve", "--bind", wildcard, "--idle", "5")
    assert proc.returncode == cli.EXIT_USAGE, proc.stderr


def test_ctrl_c_during_client_run_exits_130(tmp_path: Path) -> None:
    """SIGINT during a client's active session must propagate to exit 130
    (the top-level ``main()`` catches it; ``cmd_client`` has no inner catch)."""
    out_dir = tmp_path / "raw"
    with _serve_process(tmp_path, "sigint-server") as ports:
        argv = _cmd(*_client_argv(
            ports["http_port"], ports["grpc_port"], out_dir,
            "--hold", "5", "--idle-gaps", "3",
        ))  # fmt: skip
        proc = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        try:
            _, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=15)
            pytest.fail("client did not exit after SIGINT")
        assert proc.returncode == cli.EXIT_INTERRUPTED, err


def test_serve_sigint_exits_130(tmp_path: Path) -> None:
    """(Was a defect.) Both the module docstring and ``S2.md`` document exit
    code ``130`` for Ctrl-C generically, but ``cmd_serve`` used to catch
    ``KeyboardInterrupt`` around ``server.serve()`` and return ``EXIT_OK``
    (0). An operator following the documented exit-code table when pasting
    back a serve run's result would have misreported a Ctrl-C'd server as a
    clean, non-interrupted completion. ``serve`` now returns 130 like the
    client does.
    """
    ready = tmp_path / "ready.json"
    argv = _cmd(
        "serve", "--bind", "127.0.0.1", "--http-port", "0", "--grpc-port", "0",
        "--idle", "30", "--quiet", "--ready-file", str(ready),
    )  # fmt: skip
    proc = subprocess.Popen(
        argv,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists()
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=15)
    assert proc.returncode == cli.EXIT_INTERRUPTED, (
        f"serve must exit with the documented 130 after SIGINT; got "
        f"{proc.returncode} (err={err!r})"
    )


# ============================================================ resource leaks
def _named_threads() -> set[str]:
    """Thread names, excluding process-lifetime singletons (the gRPC
    ThreadPoolExecutor workers are pooled for the life of the *server*
    object, by design -- only per-session/per-call threads are checked
    here for leaks)."""
    return {
        t.name
        for t in threading.enumerate()
        if t.name.startswith("s2-session") or t.name == "s2-http"
    }


def test_run_client_leaves_no_session_reader_threads_after_success(
    server: S2Server,
) -> None:
    before = _named_threads()
    run_client(_config(server, keepalives=("off", "on"), idle_gaps_s=(0.1,)))
    deadline = time.monotonic() + 5
    while _named_threads() - before and time.monotonic() < deadline:
        time.sleep(0.05)
    leaked = _named_threads() - before
    assert not leaked, leaked


def test_grpc_session_reader_thread_joins_after_close(server: S2Server) -> None:
    ep = _endpoint(server)
    before = {t.ident for t in threading.enumerate() if t.name == "s2-session-client"}
    probe.grpc_session(
        ep, keepalive="off", hold_s=0.2, ping_interval_s=0.05, idle_s=0.1
    )
    deadline = time.monotonic() + 5
    while {
        t.ident for t in threading.enumerate() if t.name == "s2-session-client"
    } - before and time.monotonic() < deadline:
        time.sleep(0.05)
    leaked = {
        t.ident for t in threading.enumerate() if t.name == "s2-session-client"
    } - before
    assert not leaked, "the client's session reader thread did not join after close()"


# ===================================================== tester round 2 (0.2.6)
# Independent verification of the six fix-round defects, from S2.md's success
# criteria rather than from the implementation: real `serve` subprocesses for
# the so_reuseport fix, a heavier start/stop race, the real gRPC Download
# handler fed NaN/Infinity, and a malformed-cases result file through the
# actual `report` CLI end to end.


def test_two_serve_processes_same_grpc_port_second_refused_first_survives(
    tmp_path: Path,
) -> None:
    """(Defect 1, verified at the CLI/subprocess level, not just S2GrpcServer.)
    A stale ``serve`` holding the gRPC port must not be silently joined via
    SO_REUSEPORT: a second, independent ``serve`` process on the same gRPC
    port (different HTTP port, so only the gRPC bind can conflict) must be
    refused with the documented "cannot listen" message and exit 1 -- no
    traceback -- while the first server keeps answering real client traffic,
    and the second process's own HTTP port must be free again immediately
    (no lingering listener from the process that failed to fully start)."""
    grpc_port = _free_port()
    http_port_a = _free_port()
    http_port_b = _free_port()
    ready_a = tmp_path / "a.ready.json"
    log_a = tmp_path / "a.log"
    argv_a = _cmd(
        "serve", "--bind", "127.0.0.1", "--http-port", str(http_port_a),
        "--grpc-port", str(grpc_port), "--name", "first", "--idle", "30",
        "--io-timeout", "5", "--ready-file", str(ready_a),
    )  # fmt: skip
    with log_a.open("wb") as log_file_a:
        proc_a = subprocess.Popen(
            argv_a,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file_a,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not ready_a.exists():
                if proc_a.poll() is not None:
                    break
                time.sleep(0.05)
            assert ready_a.exists(), log_a.read_text(encoding="utf-8", errors="replace")

            proc_b = subprocess.run(
                _cmd(
                    "serve", "--bind", "127.0.0.1", "--http-port", str(http_port_b),
                    "--grpc-port", str(grpc_port), "--name", "second",
                    "--idle", "5", "--io-timeout", "5",
                ),
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )  # fmt: skip
            assert proc_b.returncode == cli.EXIT_IO, proc_b.stderr
            assert "cannot listen" in proc_b.stderr
            assert "Traceback" not in proc_b.stderr

            # the second (failed) process must not leave its own HTTP port bound
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", http_port_b))

            # the first server must still be fully alive and correct
            out_dir = tmp_path / "raw"
            code = cli.main(_client_argv(http_port_a, grpc_port, out_dir))
            assert code == cli.EXIT_OK
        finally:
            if proc_a.poll() is None:
                proc_a.terminate()
                try:
                    proc_a.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc_a.kill()
                    proc_a.wait(timeout=15)
    assert proc_a.poll() is not None, "first serve process leaked"


def test_start_stop_heavy_race_many_threads_many_iterations() -> None:
    """(Defect 2, heavier than the fix-round's 5x1-vs-1 race.) Several threads
    call ``start()`` *and* several call ``stop()`` concurrently, repeated many
    times, to stress the ``_lifecycle`` lock beyond the single-racer case."""
    failures: list[BaseException] = []
    for _ in range(15):
        srv = grpcsrv.S2GrpcServer(
            "127.0.0.1",
            0,
            name="race",
            log=lambda _m: None,
            touch=lambda: None,
            note_error=lambda _m: None,
        )
        errors: list[BaseException] = []

        def _call(method: Any, errors: list[BaseException] = errors) -> None:
            try:
                method()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_call, args=(srv.start,)) for _ in range(4)
        ] + [threading.Thread(target=_call, args=(srv.stop,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert all(not t.is_alive() for t in threads)
        failures += errors
    assert not failures, (
        f"start()/stop() raised in a {8}-thread race in "
        f"{len(failures)} of 15 attempts: {failures[0]!r}"
    )


# ================================================== real gRPC Download + NaN
def _raw_download(
    server: S2Server, request_body: bytes, *, timeout: float = 5.0
) -> tuple[list[bytes], grpc.RpcError | None]:
    assert server.grpc is not None
    channel = grpc.insecure_channel(f"127.0.0.1:{server.grpc.port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        call: grpc.UnaryStreamMultiCallable[bytes, bytes] = channel.unary_stream(
            METHOD_DOWNLOAD
        )
        chunks: list[bytes] = []
        try:
            stream = call(request_body, timeout=timeout)
            chunks = list(stream)
            return chunks, None
        except grpc.RpcError as exc:
            return chunks, exc
    finally:
        channel.close()


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity"])
def test_grpc_download_non_finite_bytes_is_invalid_argument_server_survives(
    server: S2Server, bad_value: str
) -> None:
    """(Defect 4, exercised through the real gRPC handler, not just
    ``blob_size`` in isolation.) A peer that sends ``{"bytes": NaN}`` (the
    stdlib's own ``json`` module happily *decodes*, even though ``encode()``
    refuses to *emit* it with ``allow_nan=False``) must get a clean
    ``INVALID_ARGUMENT`` -- never a crashed handler thread, never a hung
    stream -- and the server must go on serving normally afterwards."""
    # bypass encode()'s allow_nan=False: craft the wire body by hand, exactly
    # as a non-compliant or malicious peer could.
    body = json.dumps({"op": "download", "bytes": json.loads(bad_value)}).encode(
        "utf-8"
    )
    chunks, error = _raw_download(server, body)
    assert chunks == []
    assert error is not None
    assert error.code() == grpc.StatusCode.INVALID_ARGUMENT, error
    # the server must still be completely usable afterwards
    good = json.dumps({"op": "download", "bytes": 16}).encode("utf-8")
    chunks2, error2 = _raw_download(server, good)
    assert error2 is None
    assert sum(len(c) for c in chunks2) == 16


def test_grpc_download_nan_does_not_increment_download_counter() -> None:
    """A rejected, malformed request must not be counted as a served
    download: ``blob_size`` raises before ``self.counters["download"] += 1``
    runs (see ``_download`` in grpcsrv.py)."""
    srv = grpcsrv.S2GrpcServer(
        "127.0.0.1",
        0,
        name="counter-check",
        log=lambda _m: None,
        touch=lambda: None,
        note_error=lambda _m: None,
    )
    srv.start()
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{srv.port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
            call: grpc.UnaryStreamMultiCallable[bytes, bytes] = channel.unary_stream(
                METHOD_DOWNLOAD
            )
            body = json.dumps(
                {"op": "download", "bytes": math.nan}, allow_nan=True
            ).encode("utf-8")
            with pytest.raises(grpc.RpcError) as excinfo:
                list(call(body, timeout=5))
            assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            channel.close()
        assert srv.counters["download"] == 0
    finally:
        srv.stop()


# ======================================= malformed cases through cmd_report
def test_cmd_report_malformed_case_entry_end_to_end(tmp_path: Path) -> None:
    """(Defect 6, through the real ``report`` CLI subcommand end to end, not
    just ``direction_verdict`` in isolation.) A result file that is
    schema-valid overall (``cases`` is a dict) but whose *value* for one case
    id is not an object -- a hand-edited or future-buggier-version file --
    must not crash ``cmd_report``: the report must still be written, still
    contain the other, well-formed direction, and mark the malformed cell
    ``INVALID (malformed)``, never ``yes``."""
    out_dir = tmp_path / "raw"
    with _serve_process(tmp_path, "cli-server") as ports:
        code = cli.main(_client_argv(ports["http_port"], ports["grpc_port"], out_dir))
        assert code == cli.EXIT_OK
    (doc_path,) = out_dir.glob("*.json")
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    # corrupt one case entry the way a hand edit or a buggy writer could
    doc["cases"]["http-ping"] = "not-an-object-anymore"
    doc["cases"]["totally-unexpected-case"] = ["also", "garbage"]
    write_envelope(doc, doc_path)

    report = tmp_path / "S2-malformed.md"
    rc = cli.main(
        [
            "report", "--results", str(out_dir), "--out", str(report),
            "--hosts", "L1=cli-client,L2=cli-server",
        ]
    )  # fmt: skip
    assert rc == cli.EXIT_OK, "a malformed case entry must not turn into a crash"
    text = report.read_text(encoding="utf-8")
    assert "S2 cross-OS reachability" in text
    assert "| L1 -> L2 |" in text
    assert "INVALID (malformed)" in text


# ================================================= reviewer round: N3 / N4
def test_server_close_without_serve_frees_both_ports() -> None:
    """(Reviewer N4.) ``S2Server.close()`` releases both listeners without
    ever serving - the CLI path after a ``--ready-file`` write failure."""
    srv = S2Server("127.0.0.1", 0, 0, name="closer")
    http_port, grpc_port = srv.open()
    srv.close()
    for port in (http_port, grpc_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))  # raises EADDRINUSE if still held
    srv.close()  # idempotent


def test_server_close_after_serve_is_harmless() -> None:
    srv = S2Server("127.0.0.1", 0, 0, ServerLimits(duration_s=60, idle_s=60), name="c")
    http_port, grpc_port = srv.open()
    thread = srv.start_thread()
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive()
    srv.close()
    for port in (http_port, grpc_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))


def test_cli_serve_ready_file_failure_exits_one_and_frees_the_ports(
    tmp_path: Path,
) -> None:
    """(Reviewer N4, at the CLI level.) ``--ready-file`` pointing inside a
    *file* cannot be written: exit 1, and neither port may stay bound."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    http_port, grpc_port = _free_port(), _free_port()
    proc = _run(
        "serve", "--bind", "127.0.0.1", "--http-port", str(http_port),
        "--grpc-port", str(grpc_port), "--quiet", "--idle", "5",
        "--ready-file", str(blocker / "ports.json"),
        timeout=30,
    )  # fmt: skip
    assert proc.returncode == cli.EXIT_IO, proc.stderr
    assert "cannot write --ready-file" in proc.stderr
    for port in (http_port, grpc_port):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))


def test_counters_are_exact_under_concurrent_bumps() -> None:
    """(Reviewer N3.) Counter bumps from many threads never lose an increment
    and ``snapshot()`` hands back a consistent copy."""
    http = S2HttpServer(
        "127.0.0.1", 0, name="h", log=lambda _m: None, touch=lambda: None,
        note_error=lambda _m: None,
    )  # fmt: skip
    grpc_srv = grpcsrv.S2GrpcServer(
        "127.0.0.1", 0, name="g", log=lambda _m: None, touch=lambda: None,
        note_error=lambda _m: None,
    )  # fmt: skip
    try:
        rounds, workers = 2000, 8

        def _http() -> None:
            for _ in range(rounds):
                http.bump("ping")

        def _grpc() -> None:
            for _ in range(rounds):
                grpc_srv._bump("echo")

        threads = [
            threading.Thread(target=t) for t in (_http, _grpc) for _ in range(workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert http.snapshot()["ping"] == rounds * workers
        assert grpc_srv.snapshot()["echo"] == rounds * workers
        assert http.snapshot() is not http.counters
    finally:
        grpc_srv.stop()
        http.stop()


def test_concurrent_pings_are_all_counted(server: S2Server) -> None:
    """Real handler threads, not just the helper: 8 clients x 25 pings."""
    ep = _endpoint(server)
    errors: list[BaseException] = []

    def _client() -> None:
        try:
            for _ in range(25):
                probe.http_ping(ep)
                probe.grpc_unary(ep, "on")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_client) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    counters = server.counters()
    assert counters["http"]["ping"] == 8 * 25
    assert counters["grpc"]["echo"] == 8 * 25
