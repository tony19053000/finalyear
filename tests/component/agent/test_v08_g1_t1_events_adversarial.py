"""V0.8 G1 tester r1: ``/v1/events/ws`` (D172) attacked - resume under load,
slow and abrupt clients, subscriber cap, bad query values, audience rules.

Real PostgreSQL, a real HTTPS controller, real WebSocket clients. Events that
need no database change are published on the controller's own bus.
"""

# ruff: noqa: ASYNC110, SIM105

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from meshforge.common.ids import new_job_id
from meshforge.controller.events.bus import Audience, EventType
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
            "stream_recheck_interval_s": 0.2,
        },
        jobs={"dispatch_interval_s": 60.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, runtime: ControllerRuntime) -> dict[str, str]:
    return {
        "admin": await create_user(engine, "admin", ADMIN_PW, admin=True),
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        yield client


def _url(rt: ControllerRuntime, query: str = "") -> str:
    return f"wss://127.0.0.1:{rt.http_port}/v1/events/ws{query}"


async def _recv(ws: ClientConnection, within: float = 10.0) -> dict[str, Any]:
    frame = json.loads(await asyncio.wait_for(ws.recv(), within))
    assert isinstance(frame, dict)
    return frame


async def _quiet(ws: ClientConnection, within: float = 0.6) -> list[dict[str, Any]]:
    out = []
    try:
        while True:
            out.append(await _recv(ws, within))
    except TimeoutError:
        return out


def _rss_kb() -> int:
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * 4


def _bus(rt: ControllerRuntime) -> Any:
    assert rt.events is not None
    return rt.events


async def _job(rt: ControllerRuntime, owner: str) -> str:
    record, _ = await rt.jobs.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash="h" * 64,
        idempotency_key=None,
        owner_user_id=owner,
    )
    return record.id


# ------------------------------------------------------------- audiences


@pytest.mark.security
async def test_alerts_admin_only_and_ownerless_or_foreign_job_events_hidden(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    alice = await login(anon, "alice", ALICE_PW)
    admin = await login(anon, "admin", ADMIN_PW)
    bus = _bus(runtime)
    async with (
        connect(_url(runtime), additional_headers=bearer(alice)) as a,
        connect(_url(runtime), additional_headers=bearer(admin)) as root,
    ):
        await _recv(a)
        await _recv(root)
        bus.publish(
            EventType.ALERT,
            {"kind": "wrong_node", "secret": "S"},
            audience=Audience.ADMIN,
        )
        bus.publish(
            EventType.JOB_STATE,
            {"job_id": "job_bobs", "state": "running"},
            audience=Audience.OWNER,
            owner_user_id=users["bob"],
        )
        bus.publish(
            EventType.ATTEMPT_STATE,
            {
                "attempt_id": "att_b",
                "job_id": "job_bobs",
                "node_id": "n",
                "state": "running",
            },
            audience=Audience.OWNER,
            owner_user_id=users["bob"],
        )
        bus.publish(
            EventType.JOB_STATE,
            {"job_id": "job_orphan", "state": "done"},
            audience=Audience.OWNER,
            owner_user_id=None,
        )
        bus.publish(
            EventType.NODE_STATUS,
            {"node_id": "n", "state": "online", "connected": True},
        )
        alice_saw = await _quiet(a)
        root_saw = await _quiet(root)
    assert [f["type"] for f in alice_saw] == ["node.status"]
    assert "S" not in json.dumps(alice_saw) and "bobs" not in json.dumps(alice_saw)
    assert [f["type"] for f in root_saw] == [
        "alert",
        "job.state",
        "attempt.state",
        "job.state",
        "node.status",
    ]
    # seq is global and increasing; alice sees gaps (filtered events), which is
    # fine - but never a repeated or reordered seq.
    seqs = [f["seq"] for f in root_saw]
    assert seqs == sorted(set(seqs))


@pytest.mark.security
async def test_resume_replays_through_the_same_audience_filter(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    alice = await login(anon, "alice", ALICE_PW)
    bus = _bus(runtime)
    async with connect(_url(runtime), additional_headers=bearer(alice)) as ws:
        hello = await _recv(ws)
    bus.publish(EventType.ALERT, {"kind": "x"}, audience=Audience.ADMIN)
    bus.publish(
        EventType.JOB_STATE,
        {"job_id": "job_bobs", "state": "running"},
        audience=Audience.OWNER,
        owner_user_id=users["bob"],
    )
    mine = bus.publish(
        EventType.JOB_STATE,
        {"job_id": "job_alices", "state": "running"},
        audience=Audience.OWNER,
        owner_user_id=users["alice"],
    )
    q = f"?since={hello['seq']}&epoch={hello['epoch']}"
    async with connect(_url(runtime, q), additional_headers=bearer(alice)) as ws:
        h2 = await _recv(ws)
        assert h2["type"] == "hello" and h2["resumed"] is True
        frames = await _quiet(ws)
    assert [f["seq"] for f in frames] == [mine.seq]
    assert "bobs" not in json.dumps(frames)


# ----------------------------------------------------- resume vs publish race


async def test_reconnect_storm_during_continuous_publishing_loses_and_repeats_nothing(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    bus = _bus(runtime)
    total = 3000
    stop = asyncio.Event()

    async def publisher() -> None:
        for i in range(total):
            bus.publish(
                EventType.NODE_STATUS, {"node_id": "n", "i": i, "connected": True}
            )
            if i % 50 == 0:
                await asyncio.sleep(0.001)
        stop.set()

    reference: list[int] = []
    async with connect(_url(runtime), additional_headers=bearer(token)) as ref:
        first = await _recv(ref)
        base = first["seq"]

        async def collect_reference() -> None:
            while len(reference) < total:
                reference.append((await _recv(ref, 30))["seq"])

        ref_task = asyncio.create_task(collect_reference())
        pub_task = asyncio.create_task(publisher())

        # A client that keeps dropping and resuming (cut after a few frames).
        got: list[int] = []
        last, epoch = base, first["epoch"]
        resyncs = 0
        while len(got) < total and resyncs < 20:
            q = f"?since={last}&epoch={epoch}"
            async with connect(
                _url(runtime, q), additional_headers=bearer(token)
            ) as ws:
                hello = await _recv(ws)
                if hello["type"] == "resync":
                    # Honest "you missed something": refetch = jump to head.
                    resyncs += 1
                    got.extend(range(last + 1, hello["seq"] + 1))  # (refetched)
                    last = hello["seq"]
                    continue
                take = 37
                try:
                    for _ in range(take):
                        frame = await _recv(ws, 5)
                        assert frame["seq"] == last + 1, (frame["seq"], last)  # gap/dup
                        got.append(frame["seq"])
                        last = frame["seq"]
                        if len(got) >= total:
                            break
                except TimeoutError:
                    if stop.is_set() and last - base >= total:
                        break
        await pub_task
        await ref_task
    assert reference == list(range(base + 1, base + 1 + total))
    assert got[: len(set(got))] == sorted(set(got)), "duplicate or reordered seq"
    assert got == list(range(base + 1, base + 1 + len(got)))


async def test_a_bad_query_never_hangs_or_crashes_the_stream(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    for q in (
        "?since=-1",
        "?since=abc",
        "?since=",
        "?since=1.5",
        "?since=99999999999999999999999999",
        "?since=1&epoch=" + "e" * 500,
        "?epoch=",
        "?since=0&since=1",
        "?since=1&epoch=%00%ff",
    ):
        try:
            async with connect(
                _url(runtime, q), additional_headers=bearer(token)
            ) as ws:
                frame = await _recv(ws, 5)
                assert frame["type"] in {"hello", "resync"}, (q, frame)
        except (InvalidStatus, ConnectionClosed):
            pass  # a refused handshake / early close is an acceptable answer
    assert (await anon.get("/healthz")).status_code == 200
    # and a normal client still works
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        assert (await _recv(ws))["type"] == "hello"


async def test_since_in_the_future_and_epoch_mismatch_resync_never_resume(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        hello = await _recv(ws)
    for q, reason in (
        (f"?since={hello['seq'] + 1000}&epoch={hello['epoch']}", "future"),
        ("?since=0&epoch=other", "epoch"),
    ):
        async with connect(_url(runtime, q), additional_headers=bearer(token)) as ws:
            frame = await _recv(ws)
        assert frame["type"] == "resync" and frame["reason"] == reason


# --------------------------------------------------------- bad clients


async def test_a_client_that_never_reads_is_dropped_and_server_memory_is_bounded(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    bus = _bus(runtime)
    healthy_seen = 0
    async with connect(_url(runtime), additional_headers=bearer(token)) as healthy:
        await _recv(healthy)
        slow = await connect(
            _url(runtime),
            additional_headers=bearer(token),
            max_queue=1,
            max_size=None,
            compression=None,
        )
        try:
            await _recv(slow)
            assert len(bus) == 2
            pad = secrets.token_hex(30_000)  # incompressible-ish: 60 KB on the wire

            async def drain() -> None:
                nonlocal healthy_seen
                while True:
                    await healthy.recv()
                    healthy_seen += 1

            drainer = asyncio.create_task(drain())
            started = time.monotonic()
            rss0 = _rss_kb()
            # ~400 MB of traffic offered to a client that does not read.
            for i in range(6000):
                bus.publish(
                    EventType.NODE_METRICS, {"node_id": "n", "pad": pad, "i": i}
                )
                if i % 20 == 0:
                    await asyncio.sleep(0)
                if bus.stats.dropped_subscribers:
                    break
            assert _rss_kb() - rss0 < 250_000, (
                "server memory grew with a client that never reads"
            )
            assert bus.stats.dropped_subscribers >= 1, "slow client was never dropped"
            assert len(bus) == 1, "the dropped follower is still subscribed"
            assert time.monotonic() - started < 120
            # The server closes the slow socket eventually (send timeout 10 s +
            # close wait 5 s) - the connection must not leak.
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if slow.state.name == "CLOSED":
                    break
                try:
                    await asyncio.wait_for(slow.recv(), 1)
                except (ConnectionClosed, TimeoutError):
                    pass
            assert slow.state.name == "CLOSED", "slow client's socket was never closed"
            drainer.cancel()
            await asyncio.gather(drainer, return_exceptions=True)
            assert healthy_seen > 100, "a healthy client starved by the slow one"
        finally:
            await slow.close()


async def test_abrupt_disconnects_free_their_subscriber_slot(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    bus = _bus(runtime)
    socks = []
    for _ in range(15):
        ws = await connect(_url(runtime), additional_headers=bearer(token))
        await _recv(ws)
        socks.append(ws)
    assert len(bus) == 15
    for ws in socks:
        ws.transport.abort()  # RST-ish: no close handshake
    deadline = time.monotonic() + 10
    while len(bus) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert len(bus) == 0, f"{len(bus)} subscribers leaked after abrupt disconnects"
    streams = runtime.live_streams
    assert streams is not None
    assert not getattr(streams, "_streams", None), "live-stream registrations leaked"


async def test_the_subscriber_cap_closes_4029_and_a_slot_frees_up(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    bus = _bus(runtime)
    cap = 256
    opened: list[ClientConnection] = []
    try:
        for _ in range(cap):
            ws = await connect(_url(runtime), additional_headers=bearer(token))
            await _recv(ws)
            opened.append(ws)
        extra = await connect(_url(runtime), additional_headers=bearer(token))
        with pytest.raises(ConnectionClosed) as closed:
            await _recv(extra, 5)
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 4029
        await opened.pop().close()
        deadline = time.monotonic() + 5
        while len(bus) >= cap and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        again = await connect(_url(runtime), additional_headers=bearer(token))
        assert (await _recv(again))["type"] == "hello"
        opened.append(again)
    finally:
        await asyncio.gather(*(w.close() for w in opened), return_exceptions=True)


async def test_client_frames_are_ignored_not_executed(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        await _recv(ws)
        for junk in (
            '{"subscribe":"admin"}',
            "x" * 100_000,
            b"\x00\xff",
            '{"type":"alert"}',
        ):
            await ws.send(junk)
        _bus(runtime).publish(EventType.ALERT, {"kind": "k"}, audience=Audience.ADMIN)
        _bus(runtime).publish(EventType.NODE_STATUS, {"node_id": "n"})
        frames = await _quiet(ws)
    assert [f["type"] for f in frames] == ["node.status"]


async def test_events_are_published_only_after_commit_even_with_a_visible_watcher(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    """End to end: when the frame arrives, the REST API already shows the state."""
    alice = await login(anon, "alice", ALICE_PW)
    async with connect(_url(runtime), additional_headers=bearer(alice)) as ws:
        await _recv(ws)
        job_id = await _job(runtime, users["alice"])
        seen: dict[str, str] = {}
        while "queued" not in seen:
            frame = await _recv(ws, 10)
            if frame["type"] == "job.state" and frame["data"]["job_id"] == job_id:
                got = await anon.get(f"/v1/jobs/{job_id}", headers=bearer(alice))
                assert got.status_code == 200, (
                    "event published before the row was visible"
                )
                seen[frame["data"]["state"]] = got.json()["state"]
        assert seen["queued"] in {"queued", "preparing", "dispatched", "running"}
