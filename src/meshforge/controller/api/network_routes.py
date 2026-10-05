"""``GET /v1/network`` - the measured network matrix (V0.6 G4, D111).

Read-only and open like the node reads (D14; LAN-bound listener). Answer:

* ``links`` - every ``network_link`` row: ``src_node_id`` downloaded from
  ``dst_node_id``'s peer server at ``bw_bytes_s`` with ``rtt_ms`` (EWMAs over
  ``samples`` probes), ``measured_at``, ``age_s`` and ``stale`` (older than
  ``[controller.network] stale_after_s``: the planner does not use it), plus
  the pair's latest probe failure if one happened since;
* ``controller_links`` - controller -> node bandwidth from transfer history
  (median of recent controller transfers, ``source: history``);
* ``failures`` - pairs whose probes failed and that have no measurement yet;
* ``prober`` - the prober's counters, probes in flight and budget use.

``compute network`` prints it as a matrix.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request

from meshforge.common.errors import FailureType
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.network.links import age_s, is_stale
from meshforge.controller.network.service import NetworkProber
from meshforge.controller.network.store import NetworkStore

__all__ = ["NETWORK_STATE_KEY", "NetworkApi", "network_router"]

NETWORK_STATE_KEY: Final = "meshforge_network"

network_router = APIRouter(prefix="/v1", tags=["network"])


class NetworkApi:
    """What the route needs (on ``app.state``)."""

    def __init__(self, *, store: NetworkStore, prober: NetworkProber) -> None:
        self.store = store
        self.prober = prober


def get_network_api(request: Request) -> NetworkApi:
    api: NetworkApi | None = getattr(request.app.state, NETWORK_STATE_KEY, None)
    if api is None:
        raise ApiError(FailureType.CONFIG_ERROR, "the network matrix is not configured")
    return api


class FailureOut(ApiModel):
    error: str
    at: Utc


class LinkOut(ApiModel):
    src_node_id: str
    dst_node_id: str
    rtt_ms: float
    bw_bytes_s: float
    samples: int
    measured_at: Utc
    age_s: float
    stale: bool
    source: str = "measured"
    last_failure: FailureOut | None = None


class ControllerLinkOut(ApiModel):
    node_id: str
    bw_bytes_s: float
    samples: int
    last_at: Utc
    age_s: float
    source: str = "history"


class PairFailureOut(ApiModel):
    src_node_id: str
    dst_node_id: str
    error: str
    at: Utc


class NetworkOut(ApiModel):
    now: Utc
    stale_after_s: float
    links: list[LinkOut]
    controller_links: list[ControllerLinkOut]
    failures: list[PairFailureOut]
    prober: dict[str, Any]


@network_router.get("/network", response_model=NetworkOut)
async def get_network(
    api: Annotated[NetworkApi, Depends(get_network_api)],
) -> NetworkOut:
    cfg = api.prober.settings
    now = dt.datetime.now(dt.UTC)
    links, controller = await api.store.matrix(
        min_bytes=cfg.transfer_min_bytes, per_node=cfg.transfer_history
    )
    failures = {(s, d): f for s, d, f in api.prober.failure_rows()}
    measured = {(r.src_node_id, r.dst_node_id) for r in links}
    return NetworkOut(
        now=now,
        stale_after_s=cfg.stale_after_s,
        links=[
            LinkOut(
                src_node_id=r.src_node_id,
                dst_node_id=r.dst_node_id,
                rtt_ms=round(r.rtt_ms, 3),
                bw_bytes_s=round(r.bw_bytes_s, 1),
                samples=r.samples,
                measured_at=r.measured_at,
                age_s=round(age_s(r.measured_at, now), 1),
                stale=is_stale(r.measured_at, now, cfg.stale_after_s),
                last_failure=None
                if (f := failures.get((r.src_node_id, r.dst_node_id))) is None
                or f.at < r.measured_at
                else FailureOut(error=f.error, at=f.at),
            )
            for r in links
        ],
        controller_links=[
            ControllerLinkOut(
                node_id=c.node_id,
                bw_bytes_s=round(c.bw_bytes_s, 1),
                samples=c.samples,
                last_at=c.last_at,
                age_s=round(age_s(c.last_at, now), 1),
            )
            for c in controller.values()
        ],
        failures=[
            PairFailureOut(src_node_id=s, dst_node_id=d, error=f.error, at=f.at)
            for (s, d), f in sorted(failures.items())
            if (s, d) not in measured
        ],
        prober=api.prober.status(),
    )
