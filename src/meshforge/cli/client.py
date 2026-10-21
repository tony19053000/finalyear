"""Thin typed HTTP client for the controller's read-only ``/v1`` surface (0.3.12).

Why the payloads stay plain ``dict``: ``meshforge.cli`` may import only ``common``
and ``sdk`` (lint-imports), so it cannot reuse
``meshforge.controller.api.models``. Re-declaring those Pydantic models here would
create a second copy of the contract that nothing can compare against the first -
it would drift silently, and the renderer is the only consumer. So the client
validates the *envelope* (is this JSON? is it an object? did it fail?) and hands
the body through as a mapping; the renderer reads the handful of fields it needs
defensively and ``--json`` passes the controller's bytes through unchanged. A field
added server-side therefore appears in ``--json`` immediately and is ignored by the
table until someone renders it.

Every failure leaves this module as one :class:`ControllerError` carrying a
:class:`~meshforge.common.errors.FailureType` and a short, safe message - no
traceback, no httpx type, no server-side driver text.
:func:`~meshforge.cli.exits.exit_code_for` (re-exported here) maps it to the
process exit code (``UNAVAILABLE`` for unreachable/timed out, D18).
"""

from __future__ import annotations

import contextlib
import json
import os
import ssl
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Final, Self

import httpx
from pydantic import SecretStr

from meshforge.cli.exits import exit_code_for
from meshforge.common.errors import FailureType, MeshForgeError

__all__ = ["ControllerClient", "ControllerError", "exit_code_for"]

_MAX_ERROR_MESSAGE = 200
"""Server-supplied text is truncated: it reaches a terminal, and V0.3's REST
surface is unauthenticated, so it is not fully trusted input."""

_UNREACHABLE_HINT: Final = (
    " - is it running and is the address right? `compute status` shows which "
    "address is used ([cli] controller_url, or the one `compute login` stored)"
)
_TIMEOUT_HINT: Final = (
    " - it may be overloaded or the network slow; retry, or raise [cli] timeout_s"
)
_NOT_FOUND_HINT: Final = (
    " - check the id or name (`compute jobs`, `compute nodes`, `compute data ls`...)"
)
_FORBIDDEN_HINT: Final = " - your user is not allowed to do this; ask a fabric admin"
_RETRY_HINT: Final = " - retry in a moment"
"""V0.8 G6 (D178): every client error says what failed, why, and what to do."""

_TRANSFER_TIMEOUT_S: Final = 300.0
"""Bundle/output transfers and log downloads: bigger than one small JSON call."""


class ControllerError(MeshForgeError):
    """The CLI could not get an answer it could use from the controller."""

    default_failure_type = FailureType.NETWORK_ERROR


def _clean(text: object) -> str:
    """One short printable line: control characters and newlines cannot reach a TTY."""
    if not isinstance(text, str):
        return ""
    # V0.8 G6 T-9: no secret masking here (a marker made now would be mangled
    # by the error line's own scrub); `compute secret set` registers the
    # flattened spellings of its value too, so the masking in render.fail
    # still recognises a multi-line secret after this flattening.
    flat = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return flat[:_MAX_ERROR_MESSAGE]


class ControllerClient:
    """Synchronous client over ``{controller_url}/v1``.

    Synchronous on purpose: ``compute`` makes one request per render, so an event
    loop would buy nothing and cost a traceback path on Ctrl-C.
    """

    def __init__(
        self,
        controller_url: str,
        *,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
        token: SecretStr | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """``ssl_context`` (V0.7 G3): the fabric client context
        (:func:`~meshforge.common.fabric_tls.client_context`) an ``https``
        controller is verified with; ``None`` = plain HTTP (insecure-dev)."""
        self.controller_url = controller_url.rstrip("/")
        self.ssl_context = ssl_context
        self.timeout_s = timeout_s
        self.last_telemetry_schema: int | None = None
        """V0.10 G8: the telemetry schema of the last export (its header)."""
        headers = {"accept": "application/json"}
        if token is not None:
            # D44: the header only - never a query parameter, never logged
            # (httpx logs the request line, not headers; our redaction masks
            # "Bearer ..." anyway).
            headers["authorization"] = f"Bearer {token.get_secret_value()}"
        self._client = httpx.Client(
            base_url=self.controller_url,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
            headers=headers,
            verify=ssl_context if ssl_context is not None else True,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------- endpoints

    def health(self) -> Mapping[str, Any]:
        return self._get("/healthz")

    def nodes(self) -> Mapping[str, Any]:
        return self._get("/v1/nodes")

    def node(self, node_id: str) -> Mapping[str, Any]:
        return self._get(f"/v1/nodes/{node_id}")

    def sessions(self, *, limit: int | None = None) -> Mapping[str, Any]:
        params = {} if limit is None else {"limit": str(limit)}
        return self._get("/v1/sessions", params=params)

    # ------------------------------------------------------ jobs (V0.4 G5)

    def has_blob(self, hex_digest: str) -> bool:
        path = f"/v1/blobs/{hex_digest}"
        response = self._send("HEAD", path)
        if response.status_code == httpx.codes.NOT_FOUND:
            return False
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._envelope_error(response, None, path)
        return True

    def put_blob(self, hex_digest: str, path_on_disk: Path) -> Mapping[str, Any]:
        """Upload a file under its digest (streamed, with Content-Length)."""
        size = path_on_disk.stat().st_size
        with path_on_disk.open("rb") as fh:
            return self._request(
                "PUT",
                f"/v1/blobs/{hex_digest}",
                content=_file_chunks(fh),
                headers={
                    "content-length": str(size),
                    "content-type": "application/octet-stream",
                },
                timeout=_TRANSFER_TIMEOUT_S,
            )

    def download_blob(self, hex_digest: str, dest: Path) -> int:
        """Stream a blob into ``dest`` (the caller verifies the digest)."""
        path = f"/v1/blobs/{hex_digest}"
        try:
            with self._client.stream(
                "GET", path, timeout=_TRANSFER_TIMEOUT_S
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    response.read()
                    raise self._envelope_error(response, _json_or_none(response), path)
                size = 0
                with dest.open("xb") as out:
                    for chunk in response.iter_raw(1024 * 1024):
                        out.write(chunk)
                        size += len(chunk)
                return size
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, path) from exc

    def export_telemetry(
        self,
        dest: Path,
        *,
        campaign: str | None = None,
        since: str | None = None,
        table: str | None = None,
    ) -> tuple[int, int | None]:
        """V0.9 G1: stream ``GET /v1/telemetry/export`` into ``dest`` (created,
        never overwritten); ``(bytes, rows)``. V0.10 G8: ``table`` ``chunks``
        = the chunk records; :attr:`last_telemetry_schema` = the server's
        telemetry schema version of the file (None = not said)."""
        path = "/v1/telemetry/export"
        params = {
            k: v
            for k, v in (("campaign", campaign), ("since", since), ("table", table))
            if v is not None
        }
        try:
            with self._client.stream(
                "GET", path, params=params, timeout=_TRANSFER_TIMEOUT_S
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    response.read()
                    raise self._envelope_error(response, _json_or_none(response), path)
                rows = response.headers.get("x-meshforge-telemetry-rows")
                schema = response.headers.get("x-meshforge-telemetry-schema", "")
                self.last_telemetry_schema = (
                    int(schema) if schema.isdigit() and len(schema) < 6 else None
                )
                size = _download_new_file(response, dest)
                return size, int(rows) if rows and rows.isdigit() else None
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, path) from exc

    # V0.9 G3 benchmark campaigns (D216, admin)
    def bench_create(self, spec: str) -> Mapping[str, Any]:
        """``POST /v1/bench/campaigns`` with the spec's YAML text."""
        return self._request("POST", "/v1/bench/campaigns", json_body={"spec": spec})

    def bench_campaigns(self, *, limit: int = 100) -> Mapping[str, Any]:
        return self._get("/v1/bench/campaigns", params={"limit": str(limit)})

    def bench_campaign(self, campaign_id: str) -> Mapping[str, Any]:
        return self._get(f"/v1/bench/campaigns/{campaign_id}")

    def bench_runs(
        self,
        campaign_id: str,
        *,
        state: str | None = None,
        after: int = 0,
        limit: int = 1000,
    ) -> Mapping[str, Any]:
        params = {"after": str(after), "limit": str(limit)}
        if state:
            params["state"] = state
        return self._get(f"/v1/bench/campaigns/{campaign_id}/runs", params=params)

    # V0.9 G5 labels (D218)
    def bench_labels(self, campaign_id: str) -> Mapping[str, Any]:
        """``GET /v1/bench/campaigns/{id}/labels`` (JSON)."""
        return self._get(f"/v1/bench/campaigns/{campaign_id}/labels")

    def bench_quality(self, campaign_id: str) -> Mapping[str, Any]:
        """``GET /v1/bench/campaigns/{id}/quality`` (V0.9 G5c)."""
        return self._get(f"/v1/bench/campaigns/{campaign_id}/quality")

    def bench_labels_parquet(
        self, campaign_id: str, dest: Path
    ) -> tuple[int, int | None]:
        """Stream ``GET /v1/bench/campaigns/{id}/labels?format=parquet`` into
        ``dest`` (created, never overwritten); ``(bytes, rows)``."""
        path = f"/v1/bench/campaigns/{campaign_id}/labels"
        try:
            with self._client.stream(
                "GET", path, params={"format": "parquet"}, timeout=_TRANSFER_TIMEOUT_S
            ) as response:
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    response.read()
                    raise self._envelope_error(response, _json_or_none(response), path)
                rows = response.headers.get("x-meshforge-label-rows")
                size = _download_new_file(response, dest)
                return size, int(rows) if rows and rows.isdigit() else None
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, path) from exc

    def bench_submit(self, campaign_id: str, count: int) -> Mapping[str, Any]:
        """``POST /v1/bench/campaigns/{id}/submit?count=k``."""
        return self._request(
            "POST",
            f"/v1/bench/campaigns/{campaign_id}/submit",
            params={"count": str(count)},
            timeout=_TRANSFER_TIMEOUT_S,
        )

    # V0.9 G5 campaign runner controls (D246, admin)
    def bench_control(
        self,
        campaign_id: str,
        verb: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """``POST /v1/bench/campaigns/{id}/{start|pause|resume|cancel}``."""
        if verb not in ("start", "pause", "resume", "cancel"):
            raise ValueError(f"unknown campaign control {verb!r}")
        kwargs: dict[str, Any] = {}
        if body:
            kwargs["json_body"] = dict(body)
        return self._request(
            "POST", f"/v1/bench/campaigns/{campaign_id}/{verb}", **kwargs
        )

    def bench_status(self, campaign_id: str) -> Mapping[str, Any]:
        """``GET /v1/bench/campaigns/{id}/status``."""
        return self._get(f"/v1/bench/campaigns/{campaign_id}/status")

    def submit_job(
        self, body: Mapping[str, Any], *, idempotency_key: str
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/jobs",
            json_body=dict(body),
            headers={"idempotency-key": idempotency_key},
        )

    # V0.5 G5 node prepare (0.5.8)
    def start_prepare(self, node_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request(
            "POST", f"/v1/nodes/{node_id}/prepare", json_body=dict(body)
        )

    def node_gc(self, node_id: str) -> Mapping[str, Any]:
        return self._request("POST", f"/v1/nodes/{node_id}/gc")

    # V0.7 G3 node revocation (D138; fabric token = interim admin, D151)
    def revoke_node(self, node_id: str) -> Mapping[str, Any]:
        return self._request("POST", f"/v1/nodes/{node_id}/revoke")

    def prepare_status(self, node_id: str, request_id: str) -> Mapping[str, Any]:
        return self._get(f"/v1/nodes/{node_id}/prepare/{request_id}")

    # V0.5 G5 named inputs (D92)
    def push_input(self, name: str, tree_id: str, *, pinned: bool) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/v1/data",
            json_body={"name": name, "tree_id": tree_id, "pinned": pinned},
        )

    def inputs(self) -> Mapping[str, Any]:
        return self._get("/v1/data")

    def input_versions(self, name: str) -> Mapping[str, Any]:
        return self._get(f"/v1/data/{name}")

    def pin_input(self, name: str, version: int, *, pinned: bool) -> Mapping[str, Any]:
        return self._request(
            "PATCH", f"/v1/data/{name}/{version}", json_body={"pinned": pinned}
        )

    def remove_input(self, name: str, version: int) -> Mapping[str, Any]:
        return self._request("DELETE", f"/v1/data/{name}/{version}")

    def jobs(self, *, state: str | None = None, limit: int = 50) -> Mapping[str, Any]:
        params = {"limit": str(limit)}
        if state:
            params["state"] = state
        return self._get("/v1/jobs", params=params)

    def job(self, job_id: str) -> Mapping[str, Any]:
        return self._get(f"/v1/jobs/{job_id}")

    def split_progress(self, job_id: str) -> Mapping[str, Any]:
        """V0.10 G7: ``GET /v1/jobs/{id}/split`` (a split job's progress)."""
        return self._get(f"/v1/jobs/{job_id}/split")

    def cancel(self, job_id: str) -> Mapping[str, Any]:
        return self._request("POST", f"/v1/jobs/{job_id}/cancel")

    def decisions(
        self, job_id: str, *, attempt: int | None = None, limit: int = 50
    ) -> Mapping[str, Any]:
        """V0.6 G2: ``GET /v1/jobs/{id}/decisions``."""
        params = {"limit": str(limit)}
        if attempt is not None:
            params["attempt"] = str(attempt)
        return self._get(f"/v1/jobs/{job_id}/decisions", params=params)

    def network(self) -> Mapping[str, Any]:
        """V0.6 G4: ``GET /v1/network`` (the measured network matrix)."""
        return self._get("/v1/network")

    # V0.7 G2 join codes (D135, D151: fabric token = interim admin)
    def create_invite(
        self, *, ttl_s: int | None, replace_node_id: str | None
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        if replace_node_id is not None:
            body["replace_node_id"] = replace_node_id
        return self._request("POST", "/v1/fabric/invites", json_body=body)

    def invites(self) -> Mapping[str, Any]:
        return self._get("/v1/fabric/invites")

    def revoke_invite(self, invite_id: str) -> Mapping[str, Any]:
        return self._request("POST", f"/v1/fabric/invites/{invite_id}/revoke")

    # ------------------------------------------------- users (V0.7 G4, D141)

    def login(
        self, name: str, password: SecretStr, *, token_name: str
    ) -> Mapping[str, Any]:
        """``POST /v1/auth/login``; the password goes in the body only."""
        return self._request(
            "POST",
            "/v1/auth/login",
            json_body={
                "name": name,
                "password": password.get_secret_value(),
                "token_name": token_name,
            },
        )

    def whoami(self) -> Mapping[str, Any]:
        return self._get("/v1/auth/whoami")

    def tokens(self, *, all_users: bool = False) -> Mapping[str, Any]:
        params = {"all": "true"} if all_users else None
        return self._get("/v1/auth/tokens", params=params)

    def revoke_token(self, token_id: str) -> Mapping[str, Any]:
        return self._request("DELETE", f"/v1/auth/tokens/{token_id}")

    def put_secret(self, name: str, value: bytes) -> Mapping[str, Any]:
        """V0.7 G6: the value is the request body (never a URL or a header)."""
        return self._request(
            "PUT",
            f"/v1/secrets/{name}",
            content=value,
            headers={"content-type": "application/octet-stream"},
        )

    def secrets(self) -> Mapping[str, Any]:
        return self._get("/v1/secrets")

    def delete_secret(self, name: str) -> Mapping[str, Any]:
        return self._request("DELETE", f"/v1/secrets/{name}")

    def logs(self, job_id: str, *, stream: str) -> tuple[bytes, Mapping[str, str]]:
        """The persisted log bytes and the response headers."""
        path = f"/v1/jobs/{job_id}/logs"
        response = self._send(
            "GET", path, params={"stream": stream}, timeout=_TRANSFER_TIMEOUT_S
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._envelope_error(response, _json_or_none(response), path)
        return response.content, dict(response.headers)

    @property
    def auth_header(self) -> str | None:
        """For the WebSocket handshake (header only, never the URL)."""
        value = self._client.headers.get("authorization")
        return str(value) if value else None

    # -------------------------------------------------------------- internals

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                path,
                params=dict(params or {}),
                json=json_body,
                content=content,
                headers=dict(headers or {}),
                timeout=self.timeout_s if timeout is None else timeout,
            )
        except httpx.HTTPError as exc:
            raise self._transport_error(exc, path) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        return self._decode(self._send(method, path, **kwargs), path)

    def _transport_error(self, exc: httpx.HTTPError, path: str) -> ControllerError:
        if isinstance(exc, httpx.TimeoutException):
            return ControllerError(
                f"the controller at {self.controller_url} did not answer in time"
                + _TIMEOUT_HINT,
                failure_type=FailureType.TIMEOUT,
                details={"path": path},
            )
        return ControllerError(
            f"cannot reach the controller at {self.controller_url} "
            f"({type(exc).__name__})" + _UNREACHABLE_HINT,
            failure_type=FailureType.NETWORK_ERROR,
            details={"path": path},
        )

    def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        try:
            response = self._client.get(path, params=dict(params or {}))
        except httpx.TimeoutException as exc:
            raise ControllerError(
                f"the controller at {self.controller_url} did not answer within "
                f"{self.timeout_s:g}s" + _TIMEOUT_HINT,
                failure_type=FailureType.TIMEOUT,
                details={"path": path},
            ) from exc
        except httpx.HTTPError as exc:
            # ConnectError, ConnectTimeout's siblings, DNS failures, protocol
            # errors: all "no usable answer from the endpoint". The httpx message
            # can be long and platform-specific, so only its type is kept.
            raise ControllerError(
                f"cannot reach the controller at {self.controller_url} "
                f"({type(exc).__name__})" + _UNREACHABLE_HINT,
                failure_type=FailureType.NETWORK_ERROR,
                details={"path": path},
            ) from exc
        return self._decode(response, path)

    def _decode(self, response: httpx.Response, path: str) -> Mapping[str, Any]:
        body: Any = None
        try:
            body = json.loads(response.text) if response.content else None
        except ValueError:
            body = None

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._envelope_error(response, body, path)
        if not isinstance(body, dict):
            raise ControllerError(
                f"the controller answered {path} with a body that is not a JSON "
                "object; is this URL really a MeshForge controller?",
                failure_type=FailureType.INTERNAL_ERROR,
                details={"status": response.status_code},
            )
        return body

    def _envelope_error(
        self, response: httpx.Response, body: Any, path: str
    ) -> ControllerError:
        """Turn the typed ``{"error": {...}}`` envelope into a typed local error."""
        code, message = "", ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code, message = _clean(error.get("code")), _clean(error.get("message"))
        status = response.status_code
        # T-3: no envelope (a reverse proxy, an older controller) -> the HTTP
        # status alone decides auth / not found, so a script still gets 3 / 8.
        failure_type = (
            _failure_type(code)
            if code
            else _STATUS_FALLBACK.get(status, _failure_type(""))
        )
        detail = message or f"HTTP {status}"
        if failure_type is FailureType.UNAUTHORIZED and status == 403:
            return ControllerError(
                f"the controller refused the request (403): {detail}" + _FORBIDDEN_HINT,
                failure_type=FailureType.UNAUTHORIZED,
                details={"status": status, "path": path, "code": code},
            )
        if failure_type is FailureType.UNAUTHORIZED:
            return ControllerError(
                "the controller refused the request (401): you are not logged in, "
                "or your login expired or was revoked - run `compute login`",
                failure_type=FailureType.UNAUTHORIZED,
                details={"status": status, "path": path, "code": code},
            )
        if failure_type is FailureType.NETWORK_ERROR:
            # 503: the controller is up but its database is not - to a caller this
            # is still "the fabric cannot answer right now", hence UNAVAILABLE.
            return ControllerError(
                f"the controller cannot answer right now: {detail}" + _RETRY_HINT,
                failure_type=FailureType.NETWORK_ERROR,
                details={"status": status, "path": path},
            )
        hint = _NOT_FOUND_HINT if failure_type is FailureType.NOT_FOUND else ""
        details: dict[str, Any] = {"status": status, "path": path, "code": code or None}
        rejections = _rejections(body)
        if rejections:
            details["rejections"] = rejections
        return ControllerError(
            f"the controller refused the request ({status}): {detail}{hint}",
            failure_type=failure_type,
            details=details,
        )


_STATUS_FALLBACK: Final = {
    401: FailureType.UNAUTHORIZED,
    403: FailureType.UNAUTHORIZED,
    404: FailureType.NOT_FOUND,
}
"""What a refusal without the typed envelope means, by HTTP status (V0.8 G6
T-3). Only statuses with one unambiguous meaning; everything else stays
``INTERNAL_ERROR`` (exit 1), so an unreadable 5xx is never retried forever."""

_MAX_REJECTIONS: Final = 64


def _rejections(body: Any) -> list[dict[str, str]]:
    """V0.6 D104: ``error.rejections`` of a ``no_feasible_plan`` - every value
    flattened by :func:`_clean`, at most :data:`_MAX_REJECTIONS` entries."""
    error = body.get("error") if isinstance(body, dict) else None
    listed = error.get("rejections") if isinstance(error, dict) else None
    if not isinstance(listed, list):
        return []
    keys = ("node_id", "reject_code", "reject_text")
    return [
        {key: _clean(item.get(key)) for key in keys}
        for item in listed[:_MAX_REJECTIONS]
        if isinstance(item, dict)
    ]


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return json.loads(response.text) if response.content else None
    except ValueError:
        return None


def _file_chunks(fh: BinaryIO) -> Iterator[bytes]:
    while chunk := fh.read(1024 * 1024):
        yield chunk


def _failure_type(code: str) -> FailureType:
    """The envelope's ``code`` is a :class:`FailureType` value.

    Anything else (an empty body, a proxy's own error page, a future code this
    build does not know) becomes ``INTERNAL_ERROR``, which exits ``FAILURE`` - not
    ``UNAVAILABLE``, because a polling script must stop rather than hammer an
    endpoint that is answering something it cannot use. A new ``FailureType`` is a
    contract change and is not invented here.
    """
    try:
        return FailureType(code)
    except ValueError:
        return FailureType.INTERNAL_ERROR


_CREATE_NEW: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)


def _download_new_file(response: httpx.Response, dest: Path) -> int:
    """Stream ``response`` into ``dest``, which this call **creates**
    (``O_CREAT | O_EXCL``: an existing file - or a symlink, even a dangling
    one - raises :class:`FileExistsError` and is never touched). On any failure
    after the create (network, disk, Ctrl-C) the half-written file is removed -
    only if ``dest`` is still the file this call created. The byte count."""
    fd = os.open(dest, _CREATE_NEW, 0o644)
    mine = os.fstat(fd)
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in response.iter_bytes(1024 * 1024):
                out.write(chunk)
                size += len(chunk)
    except BaseException:
        with contextlib.suppress(OSError):
            now = os.lstat(dest)
            if (now.st_dev, now.st_ino) == (mine.st_dev, mine.st_ino):
                dest.unlink()  # never leave a half-written dataset behind
        raise
    return size
