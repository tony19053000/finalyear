"""``fabric_check soak``: watch the whole fabric for hours and record every state
change and every session end (V0.3 success criterion 5, part 1: zero false
OFFLINE events in 24 h on Ethernet).

* ``GET /v1/nodes`` every ``poll_s`` (default 1 s): every change of ``state`` or
  ``connected`` is a *transition*, stamped with wall time (for humans and for
  matching the controller's ``ended_at``) and with the soak's elapsed seconds.
* ``GET /v1/sessions`` every ``sessions_every_s`` (default 30 s): every session
  that ends during the soak is recorded with its ``end_reason``. Sessions live in
  PostgreSQL, so an end that happened while this tool was not running is still
  found on the next poll - that is what makes a resumed soak complete.
* The controller not answering is an *outage* (from, to, duration).

**Nothing is lost on a crash.** The checkpoint is rewritten atomically the moment
anything is recorded, and at least every ``checkpoint_every_s`` (default 5 min)
otherwise; re-running the same command resumes from it. Only elapsed time is
kept on the monotonic clock *within* one run; the time the tool was not running
is recorded as a *gap* (wall clock, last save -> resume) and does not count
towards ``--hours``, so the report states how many hours were really observed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from _fabric.api import ApiError, ControllerUnreachable, FabricApi, JsonObject
from _fabric.clock import Clock, Operator, iso

KIND: Final = "fabric-soak"
ATTRIBUTION_BEFORE_S: Final = 60.0
ATTRIBUTION_AFTER_S: Final = 15.0
"""An OFFLINE transition is attributed to the node's session that ended within
this window around it (wall clock, both from the controller laptop / controller)."""


@dataclass(frozen=True, slots=True)
class SoakConfig:
    hours: float
    link: str
    poll_s: float = 1.0
    sessions_every_s: float = 30.0
    checkpoint_every_s: float = 300.0

    def __post_init__(self) -> None:
        if self.hours <= 0:
            raise ValueError("--hours must be positive")
        if (
            self.poll_s <= 0
            or self.sessions_every_s <= 0
            or self.checkpoint_every_s <= 0
        ):
            raise ValueError("intervals must be positive")

    @property
    def target_s(self) -> float:
        return self.hours * 3600.0


def new_state(cfg: SoakConfig, now: datetime) -> dict[str, Any]:
    return {
        "started_at": iso(now),
        "day": now.date().isoformat(),
        "hours": cfg.hours,
        "link": cfg.link,
        "elapsed_s": 0.0,
        "saved_at": iso(now),
        "done": False,
        "runs": [],
        "gaps": [],
        "outages": [],
        "nodes": {},
        "transitions": [],
        "known_sessions": {},
        "session_ends": [],
        "polls": 0,
        "failed_polls": 0,
    }


class Soak:
    """One run of the soak loop over a (new or resumed) state dict.

    ``state`` is mutated in place and saved through ``save`` - the caller keeps
    the reference, so an interrupt can still save and report it.
    """

    def __init__(
        self,
        api: FabricApi,
        clock: Clock,
        operator: Operator,
        cfg: SoakConfig,
        state: dict[str, Any],
        save: Any,
    ) -> None:
        self.api, self.clock, self.operator, self.cfg = api, clock, operator, cfg
        self.state = state
        self._save_fn = save
        self._dirty = False

    # --------------------------------------------------------------- saving
    def save(self) -> None:
        self.state["saved_at"] = iso(self.clock.wall())
        self._save_fn(self.state)
        self._dirty = False

    def _event(self) -> None:
        self._dirty = True

    # ----------------------------------------------------------------- start
    def start(self) -> None:
        """First contact. A brand-new soak must reach the controller (raises
        :class:`ControllerUnreachable` otherwise); a resumed one records the gap."""
        now = iso(self.clock.wall())
        resumed = bool(self.state["runs"])
        if resumed:
            self.state["gaps"].append(
                {
                    "from": self.state["saved_at"],
                    "to": now,
                    "reason": "tool_not_running",
                }
            )
            nodes = self._nodes_or_none()
            if nodes is not None:
                self._diff_nodes(nodes, now, during_gap=True)
        else:
            nodes = self.api.nodes()
            for node in nodes:
                self.state["nodes"][str(node["node_id"])] = _node_entry(node)
            for session in self.api.sessions():
                self.state["known_sessions"][str(session["session_id"])] = (
                    session.get("ended_at") is not None
                )
        self.state["runs"].append({"started_at": now, "resumed": resumed})
        self._poll_sessions(now)
        self.save()

    def _nodes_or_none(self) -> list[JsonObject] | None:
        try:
            return self.api.nodes()
        except (ControllerUnreachable, ApiError):
            return None

    # ------------------------------------------------------------------ loop
    def run(self) -> dict[str, Any]:
        self.start()
        cfg = self.cfg
        t_prev = self.clock.monotonic()
        next_poll = t_prev
        next_sessions = t_prev + cfg.sessions_every_s
        next_save = t_prev + cfg.checkpoint_every_s
        while self.state["elapsed_s"] < cfg.target_s:
            next_poll += cfg.poll_s
            now_t = self.clock.monotonic()
            if next_poll < now_t:
                next_poll = now_t
            self.clock.sleep(next_poll - now_t)
            t = self.clock.monotonic()
            self.state["elapsed_s"] = round(self.state["elapsed_s"] + (t - t_prev), 3)
            t_prev = t
            wall = iso(self.clock.wall())
            reachable = self._poll_nodes(wall)
            if reachable and t >= next_sessions:
                self._poll_sessions(wall)
                next_sessions = t + cfg.sessions_every_s
            if self._dirty or t >= next_save:
                self.save()
                next_save = t + cfg.checkpoint_every_s
        self.state["done"] = True
        self._close_outage(iso(self.clock.wall()), final=True)
        self.save()
        return self.state

    def _poll_nodes(self, wall: str) -> bool:
        self.state["polls"] += 1
        nodes = self._nodes_or_none()
        if nodes is None:
            self.state["failed_polls"] += 1
            outages = self.state["outages"]
            if not outages or outages[-1]["to"] is not None:
                outages.append(
                    {
                        "from": wall,
                        "to": None,
                        "duration_s": None,
                        "opened_at_elapsed_s": self.state["elapsed_s"],
                    }
                )
                self.operator.say(f"{wall} controller not answering")
                self._event()
            return False
        self._close_outage(wall)
        self._diff_nodes(nodes, wall)
        return True

    def _close_outage(self, wall: str, *, final: bool = False) -> None:
        outages = self.state["outages"]
        if outages and outages[-1]["to"] is None:
            last = outages[-1]
            last["to"] = wall
            opened = float(last.get("opened_at_elapsed_s", self.state["elapsed_s"]))
            last["duration_s"] = round(self.state["elapsed_s"] - opened, 3)
            if final:
                last["still_down_at_end"] = True
            self.operator.say(f"{wall} controller answering again")
            self._event()

    def _diff_nodes(
        self, nodes: list[JsonObject], wall: str, *, during_gap: bool = False
    ) -> None:
        known: dict[str, dict[str, Any]] = self.state["nodes"]
        for node in nodes:
            node_id = str(node["node_id"])
            entry = _node_entry(node)
            old = known.get(node_id)
            if old is None:
                self._transition(
                    wall,
                    node_id,
                    entry["hostname"],
                    "appeared",
                    None,
                    entry["state"],
                    during_gap,
                )
            else:
                for name in ("state", "connected"):
                    if old[name] != entry[name]:
                        self._transition(
                            wall,
                            node_id,
                            entry["hostname"],
                            name,
                            old[name],
                            entry[name],
                            during_gap,
                        )
            known[node_id] = entry

    def _transition(
        self,
        wall: str,
        node_id: str,
        hostname: str,
        name: str,
        old: object,
        new: object,
        during_gap: bool,
    ) -> None:
        """Record one change. ``during_gap``: it happened while this tool was not
        running, so ``at`` is when it was *noticed* (on resume), not when it happened."""
        self.state["transitions"].append(
            {
                "at": wall,
                "elapsed_s": self.state["elapsed_s"],
                "node_id": node_id,
                "hostname": hostname,
                "field": name,
                "old": old,
                "new": new,
                "during_gap": during_gap,
            }
        )
        self.operator.say(f"{wall} {hostname} ({node_id}) {name}: {old} -> {new}")
        self._event()

    def _poll_sessions(self, wall: str) -> None:
        try:
            sessions = self.api.sessions()
        except (ControllerUnreachable, ApiError):
            return
        known: dict[str, bool] = self.state["known_sessions"]
        hostnames = {n: e["hostname"] for n, e in self.state["nodes"].items()}
        for session in sessions:
            sid = str(session["session_id"])
            ended = session.get("ended_at") is not None
            if known.get(sid) is True:
                continue  # already ended (before the soak, or already recorded)
            known[sid] = ended
            if ended:
                node_id = str(session.get("node_id"))
                self.state["session_ends"].append(
                    {
                        "session_id": sid,
                        "node_id": node_id,
                        "hostname": hostnames.get(node_id, ""),
                        "end_reason": session.get("end_reason"),
                        "started_at": session.get("started_at"),
                        "ended_at": session.get("ended_at"),
                        "seen_at": wall,
                    }
                )
                self.operator.say(
                    f"{wall} session of {hostnames.get(node_id, node_id)} ended: "
                    f"{session.get('end_reason')}"
                )
                self._event()


def _node_entry(node: JsonObject) -> dict[str, Any]:
    return {
        "hostname": str(node.get("hostname", "")),
        "state": node.get("state"),
        "connected": node.get("connected") is True,
    }


# ----------------------------------------------------------------- summary
def _parse(ts: object) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def attribute(transition: dict[str, Any], ends: list[dict[str, Any]]) -> str:
    """The ``end_reason`` of the node's session that ended around ``transition``."""
    at = _parse(transition.get("at"))
    if at is None:
        return "unknown"
    best: tuple[float, str] | None = None
    for end in ends:
        if end.get("node_id") != transition.get("node_id"):
            continue
        ended = _parse(end.get("ended_at"))
        if ended is None:
            continue
        delta = (ended - at).total_seconds()
        if -ATTRIBUTION_BEFORE_S <= delta <= ATTRIBUTION_AFTER_S and (
            best is None or abs(delta) < best[0]
        ):
            best = (abs(delta), str(end.get("end_reason")))
    return best[1] if best else "unknown"


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    ends: list[dict[str, Any]] = state["session_ends"]
    offline = [
        t
        for t in state["transitions"]
        if t["field"] in ("state", "appeared") and t["new"] == "offline"
    ]
    suspect = [
        t
        for t in state["transitions"]
        if t["field"] == "state" and t["new"] == "suspect"
    ]
    offline_reasons = Counter(attribute(t, ends) for t in offline)
    return {
        "target_h": state["hours"],
        "observed_h": round(state["elapsed_s"] / 3600.0, 3),
        "done": state["done"],
        "offline_events": len(offline),
        "offline_by_reason": dict(sorted(offline_reasons.items())),
        "offline_by_node": dict(
            sorted(Counter(t["hostname"] for t in offline).items())
        ),
        "suspect_events": len(suspect),
        "session_ends_by_reason": dict(
            sorted(Counter(str(e.get("end_reason")) for e in ends).items())
        ),
        "outages": len(state["outages"]),
        "outage_s": round(sum(o.get("duration_s") or 0.0 for o in state["outages"]), 3),
        "gaps": len(state["gaps"]),
        "runs": len(state["runs"]),
        "polls": state["polls"],
        "failed_polls": state["failed_polls"],
    }
