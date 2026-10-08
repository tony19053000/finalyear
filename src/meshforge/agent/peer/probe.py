"""Measure a link when the controller asks (V0.6 G4, D111): the ``ProbeRequest``
("ProbeLink") handler.

The controller picks a target node and sends this node its peer endpoint and a
probe token (kind ``probe``, holder = target, <= 60 s). This node then, on **one**
keep-alive connection to ``http://<endpoint>/v1/probe``:

1. ``rtt_samples`` (3) requests for 0 bytes - the median round trip is the RTT
   (the first one also opens the connection and is not counted when there are
   more than one);
2. one request for ``bytes`` (4 MiB) zero bytes, timed; if it took less than
   ``fast_s`` (0.5 s) one more for ``max_bytes`` (8 MiB) - the last timed
   download is the throughput.

It answers ``NetProbeResult`` through the report path (buffered, replayed after
a reconnect, acked). A failure is a result too (``ok = false``, a stable
``error`` code, the bytes actually received) so the controller can free the
pair and count it.

**Idempotent by ``probe_id``** (ARCHITECTURE §13 command audit): a probe id
seen before is never measured again - a finished one re-sends its result, one
still running is ignored. **One probe at a time** per agent process; a second
one is answered ``busy`` at once. Every request is bounded by
``[agent.probe] request_timeout_s`` and the **whole probe** by ``deadline_s``
(default 2 x that): a target that keeps each read just inside the request
timeout but drips its body cannot hold the one probe slot - the probe ends as
a failure, ``error = "deadline"`` (tester r1 T-9). Sizes are capped at 8 MiB
here as well (the controller and the target's server cap them too). The token
is never logged.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx

from meshforge.agent.http_tls import AgentHttpTls, peer_extensions
from meshforge.agent.settings import ProbeSettings
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["MAX_PROBE_BYTES", "ProbeService"]

log = get_logger(__name__)

MAX_PROBE_BYTES: Final = 8 * 1024 * 1024
_MAX_RTT_SAMPLES: Final = 10
_REMEMBERED: Final = 256
_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ENDPOINT_RE: Final = re.compile(
    r"(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):(\d{1,5})"
)
_READ: Final = 64 * 1024


@dataclass(slots=True)
class _Probe:
    probe_id: str
    target: str
    result: agent_pb2.NetProbeResult | None = None


class _Failed(Exception):  # noqa: N818 - internal control flow
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProbeService:
    """See module docstring. One per agent process (event-loop owned)."""

    def __init__(
        self,
        settings: ProbeSettings,
        send_report: Callable[[str, Any], Awaitable[str]],
        *,
        node_id: str,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
        http_tls: AgentHttpTls | None = None,
    ) -> None:
        """``http_tls`` (mtls, V0.7 G3): probes are HTTPS with this node's
        certificate and the peer must be ``target_node_id`` (D139)."""
        self._http_tls = http_tls
        self._cfg = settings
        self._send = send_report
        self._node_id = node_id
        self._clock = clock
        self._transport = transport
        self._probes: OrderedDict[str, _Probe] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self.measured = 0
        self.refused = 0

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------- command

    def handle(self, msg: agent_pb2.ProbeRequest) -> None:
        """A ``ProbeRequest`` (synchronous, before its Ack)."""
        probe_id = msg.probe_id
        if not _ID_RE.fullmatch(probe_id):
            self.refused += 1
            log.warning("agent.probe.refused", node_id=self._node_id, reason="bad_id")
            return
        known = self._probes.get(probe_id)
        if known is not None:
            if known.result is not None:  # a replayed command: say it again
                self._spawn(
                    self._report(known.result), f"agent-probe-report:{probe_id}"
                )
            return
        probe = _Probe(probe_id, msg.target_node_id)
        self._probes[probe_id] = probe
        while len(self._probes) > _REMEMBERED:
            self._probes.popitem(last=False)
        refusal = self._refusal(msg)
        if refusal is not None:
            self.refused += 1
            probe.result = self._failure(msg, refusal, 0, 0.0)
            log.info(
                "agent.probe.refused",
                node_id=self._node_id,
                probe_id=probe_id,
                target_node_id=msg.target_node_id,
                reason=refusal,
            )
            self._spawn(self._report(probe.result), f"agent-probe-report:{probe_id}")
            return
        self._running = True
        self._spawn(self._run(probe, msg), f"agent-probe:{probe_id}")

    def _refusal(self, msg: agent_pb2.ProbeRequest) -> str | None:
        if not self._cfg.enabled:
            return "disabled"
        if self._running:
            return "busy"
        if not _ENDPOINT_RE.fullmatch(msg.endpoint) or not msg.token:
            return "bad_request"
        port = int(msg.endpoint.rsplit(":", 1)[1])
        if not 1 <= port <= 65535:
            return "bad_request"
        if (
            msg.bytes > MAX_PROBE_BYTES
            or msg.max_bytes > MAX_PROBE_BYTES
            or msg.rtt_samples > _MAX_RTT_SAMPLES
            or not 0.0 <= msg.fast_s <= 60.0
        ):
            return "bad_request"
        return None

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error(
                "agent.probe.task_failed",
                node_id=self._node_id,
                cause=type(task.exception()).__name__,
            )

    async def _report(self, result: agent_pb2.NetProbeResult) -> None:
        await self._send("net_probe_result", result)

    @staticmethod
    def _failure(
        msg: agent_pb2.ProbeRequest, code: str, received: int, took: float
    ) -> agent_pb2.NetProbeResult:
        return agent_pb2.NetProbeResult(
            probe_id=msg.probe_id,
            target_node_id=msg.target_node_id,
            ok=False,
            bytes=received,
            duration_s=round(took, 4),
            error=code,
        )

    # ----------------------------------------------------------------- run

    async def _run(self, probe: _Probe, msg: agent_pb2.ProbeRequest) -> None:
        started = self._clock()
        counter = _Counter()
        try:
            rtt_ms, bw = await self._measure(msg, counter)
        except _Failed as exc:
            result = self._failure(
                msg, exc.code, counter.bytes, self._clock() - started
            )
        except asyncio.CancelledError:
            self._running = False
            raise
        except Exception:  # a bug must still answer, or the pair waits a timeout
            log.exception("agent.probe.failed_unexpectedly", node_id=self._node_id)
            result = self._failure(
                msg, "internal_error", counter.bytes, self._clock() - started
            )
        else:
            self.measured += 1
            result = agent_pb2.NetProbeResult(
                probe_id=msg.probe_id,
                target_node_id=msg.target_node_id,
                ok=True,
                rtt_ms=round(rtt_ms, 3),
                bw_bytes_s=round(bw, 1),
                bytes=counter.bytes,
                duration_s=round(self._clock() - started, 4),
            )
        finally:
            self._running = False
        probe.result = result
        log.info(
            "agent.probe.done",
            node_id=self._node_id,
            probe_id=msg.probe_id,
            target_node_id=msg.target_node_id,
            ok=result.ok,
            error=result.error or None,
            rtt_ms=result.rtt_ms,
            bw_bytes_s=result.bw_bytes_s,
            bytes=result.bytes,
        )
        await self._report(result)

    async def _measure(
        self, msg: agent_pb2.ProbeRequest, counter: _Counter
    ) -> tuple[float, float]:
        timeout = self._cfg.request_timeout_s
        headers = {"authorization": f"Bearer {msg.token}"}
        scheme = "https" if self._http_tls is not None else "http"
        base = f"{scheme}://{msg.endpoint}/v1/probe"
        transport = self._transport
        extensions: dict[str, Any] = {}
        if self._http_tls is not None:
            transport = transport or self._http_tls.transport()
            extensions = peer_extensions(msg.target_node_id)
        samples = max(1, msg.rtt_samples)
        try:
            async with (
                asyncio.timeout(self._cfg.effective_deadline_s),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    headers=headers,
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
            ):
                # one extra warm-up request opens the connection (not counted)
                rtts = [
                    await self._get(client, base, 0, counter, extensions)
                    for _ in range(samples + (1 if samples > 1 else 0))
                ]
                if samples > 1:
                    rtts = rtts[1:]
                rtt_ms = statistics.median(rtts) * 1000.0
                size = min(msg.bytes, MAX_PROBE_BYTES)
                if size == 0:
                    return rtt_ms, 0.0
                took = await self._get(client, base, size, counter, extensions)
                big = min(msg.max_bytes, MAX_PROBE_BYTES)
                if took < msg.fast_s and big > size:
                    size = big
                    took = await self._get(client, base, size, counter, extensions)
        except TimeoutError:  # the whole-probe deadline (asyncio.timeout)
            raise _Failed("deadline") from None
        except httpx.TimeoutException:
            raise _Failed("timeout") from None
        except httpx.HTTPError:
            raise _Failed("unreachable") from None
        return rtt_ms, size / max(took, 1e-6)

    async def _get(
        self,
        client: httpx.AsyncClient,
        base: str,
        size: int,
        counter: _Counter,
        extensions: dict[str, Any] | None = None,
    ) -> float:
        """Time one ``GET ?bytes=size`` to its last body byte."""
        started = self._clock()
        async with client.stream(
            "GET", base, params={"bytes": str(size)}, extensions=extensions or {}
        ) as resp:
            if resp.status_code != 200:
                raise _Failed(f"http_{resp.status_code}")
            got = 0
            async for chunk in resp.aiter_bytes(_READ):
                got += len(chunk)
                counter.bytes += len(chunk)
                if got > size:
                    raise _Failed("oversized")
        if got != size:
            raise _Failed("short")
        return self._clock() - started


@dataclass(slots=True)
class _Counter:
    bytes: int = 0
