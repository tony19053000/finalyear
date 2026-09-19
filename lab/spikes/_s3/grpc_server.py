"""gRPC sender: server-streaming download and client-streaming upload of bytes.

A spike must not change the shipped contracts, so no ``.proto`` file is added
(that would be a V0.3+ change to ``proto/``). Instead the service is registered
with gRPC's **generic handler** API and identity (bytes) serializers, so the
messages on the wire are exactly the payload chunks. A real ``bytes`` field in a
protobuf message would add a tag plus a varint length (a handful of bytes per
message), which is far below the resolution of this measurement - what we are
measuring is gRPC's HTTP/2 framing, flow control and copies, not protobuf.

Requests and results are UTF-8 JSON in a single ``bytes`` message, matching the
raw-TCP control protocol, so all three transports answer the same questions.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent import futures
from typing import Any, Final

import grpc

from spikes._s3 import payload
from spikes._s3.context import ServerContext
from spikes._s3.protocol import (
    GRPC_SERVICE,
    Message,
    ProtocolError,
    grpc_target,
    is_lan_address,
    peer_host,
    transfer_request,
)

MAX_WORKERS: Final = 8
_GRACE_S: Final = 2.0


def identity_bytes(value: bytes) -> bytes:
    return value


def encode(msg: Message) -> bytes:
    return json.dumps(msg, allow_nan=False).encode("utf-8")


def decode(data: bytes) -> Message:
    """A control message; :class:`ProtocolError` for anything else."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"request is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("request is not a JSON object")
    return msg


class TransportService:
    """``Download`` (unary -> stream) and ``Upload`` (stream -> unary)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    def _check_peer(self, context: grpc.ServicerContext) -> None:
        host = peer_host(context.peer())
        if host is None or not is_lan_address(host):
            self.ctx.count("refused_peer")
            self.ctx.say(f"grpc: refused non-LAN peer {context.peer()}")
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "LAN peers only")

    def download(
        self, request: bytes, context: grpc.ServicerContext
    ) -> Iterator[bytes]:
        self._check_peer(context)
        self.ctx.touch()
        try:
            transfer_id, size, chunk = transfer_request(decode(request))
        except ProtocolError as exc:
            self.ctx.count("bad_requests")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise  # pragma: no cover - abort() raises
        self.ctx.count("transfers")
        self.ctx.transfers.begin(transfer_id, "grpc")
        sent = 0
        error: str | None = None
        try:
            for view in payload.iter_chunks(0, size, chunk):
                if self.ctx.stopping.is_set() or not context.is_active():
                    error = (
                        "cancelled" if not context.is_active() else "server stopping"
                    )
                    break
                yield bytes(view)
                sent += len(view)
        except grpc.RpcError as exc:  # pragma: no cover - peer vanished
            error = f"RpcError: {exc}"
        finally:
            self.ctx.transfers.end(transfer_id, num_bytes=sent, error=error)
            self.ctx.touch()

    def upload(
        self, request_iterator: Iterator[bytes], context: grpc.ServicerContext
    ) -> bytes:
        """Receiver view: the first message is the JSON request, then raw chunks."""
        self._check_peer(context)
        self.ctx.touch()
        try:
            header = next(request_iterator)
        except StopIteration:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "empty upload")
            raise  # pragma: no cover - abort() raises
        try:
            transfer_id, size, _chunk = transfer_request(decode(header))
        except ProtocolError as exc:
            self.ctx.count("bad_requests")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise  # pragma: no cover - abort() raises
        self.ctx.count("transfers")
        verifier = payload.Verifier()
        error: str | None = None
        # Window starts before the first chunk, as in the raw-TCP receiver.
        self.ctx.transfers.begin(transfer_id, "grpc")
        try:
            for chunk in request_iterator:
                verifier.update(chunk)
                if self.ctx.stopping.is_set():
                    error = "server stopping"
                    break
        except grpc.RpcError as exc:  # pragma: no cover - peer vanished
            error = f"RpcError: {exc}"
        crc_ok = verifier.matches(size)
        self.ctx.transfers.end(
            transfer_id, num_bytes=verifier.received, crc_ok=crc_ok, error=error
        )
        item = self.ctx.transfers.get(transfer_id)
        self.ctx.touch()
        return encode(
            {
                "ok": error is None and crc_ok,
                "error": error
                or (None if crc_ok else "checksum mismatch at the receiver"),
                "stats": item.to_json() if item is not None else None,
            }
        )


def build_grpc_server(
    bind: str, port: int, ctx: ServerContext, *, max_message_bytes: int
) -> tuple[grpc.Server, int]:
    """A bound (not started) gRPC server and the port it listens on.

    Raises ``OSError`` when the port cannot be bound, like the other transports.
    """
    service = TransportService(ctx)
    handlers: dict[str, grpc.RpcMethodHandler[Any, Any]] = {
        "Download": grpc.unary_stream_rpc_method_handler(
            service.download,
            request_deserializer=identity_bytes,
            response_serializer=identity_bytes,
        ),
        "Upload": grpc.stream_unary_rpc_method_handler(
            service.upload,
            request_deserializer=identity_bytes,
            response_serializer=identity_bytes,
        ),
    }
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=MAX_WORKERS),
        handlers=[grpc.method_handlers_generic_handler(GRPC_SERVICE, handlers)],
        options=[
            ("grpc.max_receive_message_length", max_message_bytes),
            ("grpc.max_send_message_length", max_message_bytes),
            ("grpc.max_concurrent_streams", ctx.limits.max_connections),
            ("grpc.so_reuseport", 0),
        ],
    )
    bound = server.add_insecure_port(grpc_target(bind, port))
    if bound == 0:
        raise OSError(f"cannot bind gRPC to {grpc_target(bind, port)}")
    return server, bound


def stop_grpc_server(server: grpc.Server, grace_s: float = _GRACE_S) -> None:
    server.stop(grace_s).wait(timeout=grace_s + 1.0)
