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

``grpcio`` is a core dependency, but the spike must never *crash* without it: the
import is optional, :data:`GRPC_IMPORT_ERROR` records why it failed, and the
server then runs raw TCP + HTTP only while every gRPC case on the client is a
typed ``grpc_unavailable`` failure.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent import futures
from typing import Any, Final

from spikes._s3 import payload
from spikes._s3.context import ServerContext
from spikes._s3.protocol import (
    GRPC_SERVICE,
    FailureType,
    Message,
    ProtocolError,
    TransferError,
    grpc_target,
    is_lan_address,
    peer_host,
    transfer_request,
)

#: ``None`` when grpcio imported; otherwise the reason (a short, path-free text).
GRPC_IMPORT_ERROR: str | None = None
try:
    import grpc
except ImportError as _exc:  # pragma: no cover - grpcio is a core dependency
    GRPC_IMPORT_ERROR = f"grpcio is not installed ({type(_exc).__name__}: {_exc})"

MAX_WORKERS: Final = 8
_GRACE_S: Final = 2.0


def grpc_available() -> bool:
    return GRPC_IMPORT_ERROR is None


def grpc_version() -> str | None:
    """Installed grpcio version, ``None`` without grpcio."""
    return None if GRPC_IMPORT_ERROR else str(grpc.__version__)


def require_grpc(doing: str) -> None:
    """Raise the typed, non-fatal ``grpc_unavailable`` failure without grpcio."""
    if GRPC_IMPORT_ERROR is not None:
        raise TransferError(
            FailureType.GRPC_UNAVAILABLE, f"{doing}: {GRPC_IMPORT_ERROR}"
        )


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
        except GeneratorExit:
            # the peer cancelled or vanished: gRPC closes the response generator
            error = "cancelled by the peer"
            raise
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


class GrpcHost:
    """A bound gRPC server plus the worker pool it runs on (stopped together).

    ``grpc.server`` does not own its executor, so stopping the server alone
    would leave the pool's threads alive; :meth:`stop` shuts both down and
    joins, which is what "no leftover threads" tests check.
    """

    def __init__(
        self, server: grpc.Server, executor: futures.ThreadPoolExecutor, port: int
    ) -> None:
        self.server = server
        self.executor = executor
        self.port = port

    def start(self) -> None:
        self.server.start()

    def stop(self, grace_s: float = _GRACE_S) -> None:
        self.server.stop(grace_s).wait(timeout=grace_s + 1.0)
        self.executor.shutdown(wait=True, cancel_futures=True)


def build_grpc_server(
    bind: str, port: int, ctx: ServerContext, *, max_message_bytes: int
) -> GrpcHost:
    """A bound (not started) gRPC server on the port it listens on.

    Raises ``OSError`` when the port cannot be bound, like the other transports,
    and :class:`TransferError` (``grpc_unavailable``) without grpcio.
    """
    require_grpc("grpc server")
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
    executor = futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS, thread_name_prefix="s3-grpc"
    )
    server = grpc.server(
        executor,
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
        executor.shutdown(wait=False)
        raise OSError(f"cannot bind gRPC to {grpc_target(bind, port)}")
    return GrpcHost(server, executor, bound)
