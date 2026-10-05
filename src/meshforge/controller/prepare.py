"""``compute node prepare``: the controller side (V0.5 G5, roadmap 0.5.8).

``POST /v1/nodes/{id}/prepare`` sends ``PrepareEnv`` to a connected node and
returns a ``request_id``; the agent answers with ``PrepareEnvStatus`` reports
(see :mod:`meshforge.agent.envs.node_prepare`) which :class:`PrepareTracker`
keeps **in memory** (bounded: the newest :data:`MAX_TRACKED` requests) for
``GET /v1/nodes/{id}/prepare/{request_id}``. Nothing is persisted: a controller
restart forgets the requests (the agent still finishes the work and the next
request finds it built - ``hit``); the CLI then says the status is unknown.

Reports are hostile input: a status for a request this controller did not send
to *that* node is dropped; items are bounded and every string is printable and
length-capped before it is kept.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

if TYPE_CHECKING:
    from meshforge.controller.session.live import LiveSession
    from meshforge.controller.session.manager import SessionManager

__all__ = ["MAX_TRACKED", "PrepareRecord", "PrepareTracker"]

log = get_logger(__name__)

MAX_TRACKED: Final = 256
MAX_ITEMS: Final = 8 + 32 + 1
_KINDS: Final = frozenset({"image", "env", "request"})
_STATES: Final = frozenset({"pending", "running", "hit", "pulled", "built", "failed"})
_SHORT: Final = 64
_DETAIL: Final = 300


def _text(value: str, limit: int) -> str | None:
    if len(value) > limit or not value.isprintable():
        return None
    return value


@dataclass(slots=True)
class PrepareRecord:
    request_id: str
    node_id: str
    created_at: dt.datetime
    images: list[str]
    envs: int
    items: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    updated_at: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        failed = [i for i in self.items if i.get("state") == "failed"]
        return {
            "request_id": self.request_id,
            "node_id": self.node_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": None
            if self.updated_at is None
            else self.updated_at.isoformat(),
            "images": list(self.images),
            "envs": self.envs,
            "done": self.done,
            "ok": self.done and not failed,
            "items": list(self.items),
        }


class PrepareTracker:
    """See module docstring."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._records: OrderedDict[str, PrepareRecord] = OrderedDict()
        self.dropped = 0

    def start(
        self,
        node_id: str,
        *,
        images: list[str],
        envs: list[agent_pb2.EnvSpecRequest],
    ) -> PrepareRecord | None:
        """Send ``PrepareEnv``; the record, or None when the node is not live."""
        request_id = str(new_command_id()).removeprefix("cmd_")
        sent = self._manager.send_command(
            node_id,
            "prepare_env",
            agent_pb2.PrepareEnv(request_id=request_id, base_images=images, envs=envs),
        )
        if sent is None:
            return None
        record = PrepareRecord(
            request_id=request_id,
            node_id=node_id,
            created_at=dt.datetime.now(dt.UTC),
            images=list(images),
            envs=len(envs),
        )
        self._records[request_id] = record
        while len(self._records) > MAX_TRACKED:
            self._records.popitem(last=False)
        log.info(
            "controller.prepare.sent",
            node_id=node_id,
            request_id=request_id,
            images=images,
            envs=len(envs),
        )
        return record

    def request_gc(self, node_id: str) -> bool:
        """``GcRequest{run_now}`` to a live node (V0.5 G5); False = not live."""
        sent = self._manager.send_command(
            node_id, "gc", agent_pb2.GcRequest(run_now=True)
        )
        if sent is not None:
            log.info("controller.gc.node_requested", node_id=node_id)
        return sent is not None

    def get(self, node_id: str, request_id: str) -> PrepareRecord | None:
        record = self._records.get(request_id)
        return record if record is not None and record.node_id == node_id else None

    async def on_message(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        self.on_status(live, msg.prepare_env_status)

    def on_status(self, live: LiveSession, status: agent_pb2.PrepareEnvStatus) -> None:
        record = self._records.get(status.request_id)
        if record is None or record.node_id != live.node_id:
            self._drop(live, "unknown_request")
            return
        if len(status.items) > MAX_ITEMS:
            self._drop(live, "too_many_items")
            return
        items: list[dict[str, Any]] = []
        for item in status.items:
            if item.kind not in _KINDS or item.state not in _STATES:
                self._drop(live, "bad_item")
                return
            fields = {
                "name": _text(item.name, _SHORT),
                "failure_type": _text(item.failure_type, _SHORT),
                "reason": _text(item.reason, _SHORT),
                "detail": _text(item.detail, _DETAIL),
                "key": _text(item.key, 80),
            }
            if any(v is None for v in fields.values()):
                self._drop(live, "bad_item_text")
                return
            items.append(
                {
                    "kind": item.kind,
                    "state": item.state,
                    **{k: (v or None) for k, v in fields.items()},
                    "size_bytes": int(item.size_bytes),
                    "duration_s": round(max(0.0, float(item.duration_s)), 3)
                    if item.duration_s == item.duration_s
                    else 0.0,
                }
            )
        if record.done and not status.done:
            return  # an older report replayed after the final one
        record.items = items
        record.done = record.done or status.done
        record.updated_at = dt.datetime.now(dt.UTC)
        if status.done:
            log.info(
                "controller.prepare.done",
                node_id=live.node_id,
                request_id=record.request_id,
                states=[i["state"] for i in items],
            )

    def _drop(self, live: LiveSession, reason: str) -> None:
        self.dropped += 1
        log.warning(
            "controller.prepare.status_dropped",
            node_id=live.node_id,
            session_id=live.session_id,
            reason=reason,
        )
