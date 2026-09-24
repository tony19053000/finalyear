"""``fabric_check restart-check``: kill + restart the controller and time how long
until every node that was connected is connected again (V0.3 success criterion 3,
<= 15 s), and whether any node came back under a different ``node_id`` (4).

Per repetition, the tool itself sees the three moments (no Enter needed):

``t_down``  first poll the controller did not answer;
``t_up``    first ``/healthz`` answer after that;
``t_all``   first ``/v1/nodes`` answer where every node connected before is
            ``connected`` again.

``reconnect_all_s = t_all - t_up`` is the criterion-3 number: from the moment the
new controller process serves, to the whole fabric being back. It deliberately
excludes how long the human took to restart the process (``outage_s``). Note the
agents back off with full jitter capped at 5 s (ADR-0002 Updates §3, D27), so a
long outage lets the backoff reach its cap - restart the controller promptly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from _fabric.api import ApiError, ControllerUnreachable, FabricApi
from _fabric.clock import Clock, Operator, iso
from _fabric.timeline import restarted

RECONNECT_BOUND_S: Final = 15.0
NO_NODES: Final = "no_connected_nodes"
NOT_DOWN: Final = "controller_not_stopped"
NOT_UP: Final = "controller_not_restarted"
NOT_ALL_BACK: Final = "not_all_reconnected"
NODE_ID_CHANGED: Final = "node_id_changed"


class RestartAbort(Exception):  # noqa: N818 (a typed stop, carries failure_type)
    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass(frozen=True, slots=True)
class RestartConfig:
    reps: int = 5
    poll_s: float = 0.1
    down_timeout_s: float = 600.0
    up_timeout_s: float = 600.0
    reconnect_timeout_s: float = 120.0
    sample_wait_s: float = 15.0
    settle_s: float = 5.0
    """Pause between repetitions so every agent's session is fully up again."""

    def __post_init__(self) -> None:
        if self.reps < 1:
            raise ValueError("--reps must be at least 1")


@dataclass
class RestartResult:
    samples: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    nodes: dict[str, str] = field(default_factory=dict)
    """node_id -> hostname of every node that was connected at some repetition."""
    aborted: bool = False


def _error(rep: int, failure_type: str, message: str, *, fatal: bool) -> dict[str, Any]:
    return {
        "rep": rep,
        "phase": "measure",
        "failure_type": failure_type,
        "message": message,
        "fatal": fatal,
    }


def _max_sequence(api: FabricApi, node_id: str, epoch: int | None = None) -> list[int]:
    try:
        samples = api.live_samples(node_id)
    except (ControllerUnreachable, ApiError):
        return []
    return [
        s["sequence"]
        for s in samples
        if isinstance(s.get("sequence"), int)
        and (epoch is None or s.get("session_epoch") == epoch)
    ]


def _snapshot(api: FabricApi) -> dict[str, dict[str, Any]]:
    """The connected nodes right now, with epoch and highest sample sequence."""
    out: dict[str, dict[str, Any]] = {}
    for node in api.nodes():
        if node.get("connected") is not True:
            continue
        node_id = str(node["node_id"])
        detail = api.node(node_id)
        seqs = _max_sequence(api, node_id)
        out[node_id] = {
            "hostname": str(node.get("hostname", "")),
            "epoch_before": detail.get("session_epoch"),
            "sequence_before": max(seqs) if seqs else None,
        }
    return out


def run_restart_check(
    api: FabricApi,
    clock: Clock,
    operator: Operator,
    cfg: RestartConfig,
    result: RestartResult | None = None,
) -> RestartResult:
    """Run ``cfg.reps`` repetitions, filling ``result`` as it goes (Ctrl-C safe)."""
    result = result if result is not None else RestartResult()
    for rep in range(cfg.reps):
        if rep:
            clock.sleep(cfg.settle_s)
        try:
            sample = _one_rep(api, clock, operator, cfg, rep, result)
        except RestartAbort as exc:
            result.errors.append(_error(rep, exc.failure_type, str(exc), fatal=True))
            result.aborted = True
            operator.say(f"ABORTED: {exc}")
            break
        result.samples.append(sample)
        missing = [n for n, d in sample["nodes"].items() if d["reconnect_s"] is None]
        changed = [n for n, d in sample["nodes"].items() if d["node_id_kept"] is False]
        if missing:
            result.errors.append(
                _error(
                    rep,
                    NOT_ALL_BACK,
                    f"not reconnected within {cfg.reconnect_timeout_s:.0f} s: "
                    + ", ".join(
                        f"{sample['nodes'][n]['hostname']} ({n})" for n in missing
                    ),
                    fatal=False,
                )
            )
        if changed:
            result.errors.append(
                _error(
                    rep,
                    NODE_ID_CHANGED,
                    "came back under a new node_id: "
                    + ", ".join(
                        f"{sample['nodes'][n]['hostname']} {n} -> "
                        f"{sample['nodes'][n]['new_node_id']}"
                        for n in changed
                    ),
                    fatal=False,
                )
            )
    return result


def _wait(
    clock: Clock,
    cfg: RestartConfig,
    timeout_s: float,
    probe: Any,
    failure: tuple[str, str],
) -> float:
    """Poll ``probe()`` every ``poll_s`` until true; return the monotonic time."""
    start = clock.monotonic()
    while True:
        if probe():
            return clock.monotonic()
        if clock.monotonic() - start >= timeout_s:
            raise RestartAbort(*failure)
        clock.sleep(cfg.poll_s)


def _answers(api: FabricApi) -> bool:
    try:
        api.healthz()
    except (ControllerUnreachable, ApiError):
        return False
    return True


def _one_rep(
    api: FabricApi,
    clock: Clock,
    operator: Operator,
    cfg: RestartConfig,
    rep: int,
    result: RestartResult,
) -> dict[str, Any]:
    before = _snapshot(api)
    if not before:
        raise RestartAbort(NO_NODES, "no node is connected - start the agents first")
    result.nodes.update({n: d["hostname"] for n, d in before.items()})
    operator.say(
        f"--- repetition {rep + 1}/{cfg.reps}: {len(before)} node(s) connected: "
        + ", ".join(sorted(d["hostname"] for d in before.values()))
    )
    operator.prompt(
        "KILL the controller now and START it again straight away "
        "(this tool sees both on its own; no Enter needed)"
    )
    t_down = _wait(
        clock,
        cfg,
        cfg.down_timeout_s,
        lambda: not _answers(api),
        (NOT_DOWN, f"the controller kept answering for {cfg.down_timeout_s:.0f} s"),
    )
    down_wall = iso(clock.wall())
    operator.say("controller is down - waiting for it to come back ...")
    t_up = _wait(
        clock,
        cfg,
        cfg.up_timeout_s,
        lambda: _answers(api),
        (NOT_UP, f"the controller did not come back within {cfg.up_timeout_s:.0f} s"),
    )
    up_wall = iso(clock.wall())
    operator.say(
        f"controller is back after {t_up - t_down:.2f} s - waiting for the nodes ..."
    )
    first_seen: dict[str, float] = {}
    connected_now: dict[str, dict[str, Any]] = {}
    while True:
        try:
            nodes = api.nodes()
        except (ControllerUnreachable, ApiError):
            nodes = []
        now = clock.monotonic()
        connected_now = {
            str(n["node_id"]): n for n in nodes if n.get("connected") is True
        }
        for node_id in before:
            if node_id in connected_now and node_id not in first_seen:
                first_seen[node_id] = now
        if len(first_seen) == len(before) or now - t_up >= cfg.reconnect_timeout_s:
            break
        clock.sleep(cfg.poll_s)
    t_all = max(first_seen.values()) if len(first_seen) == len(before) else None
    per_node: dict[str, dict[str, Any]] = {}
    for node_id, info in before.items():
        entry = dict(info)
        seen = first_seen.get(node_id)
        entry["reconnect_s"] = round(seen - t_up, 3) if seen is not None else None
        new_id = next(
            (
                other
                for other, doc in connected_now.items()
                if other != node_id
                and other not in before
                and doc.get("hostname") == info["hostname"]
            ),
            None,
        )
        entry["new_node_id"] = new_id
        entry["node_id_kept"] = (
            False
            if new_id is not None and seen is None
            else (True if seen is not None else None)
        )
        epoch_after: int | None = None
        if seen is not None:
            try:
                epoch_after = api.node(node_id).get("session_epoch")
            except (ControllerUnreachable, ApiError):
                epoch_after = None
        entry["epoch_after"] = epoch_after
        news = (
            _wait_sequences(api, clock, cfg, node_id, epoch_after)
            if seen is not None
            else []
        )
        entry["first_new_sequence"] = min(news) if news else None
        entry["restarted"] = restarted(info["sequence_before"], news)
        per_node[node_id] = entry
    sample: dict[str, Any] = {
        "rep": rep,
        "down_at": down_wall,
        "up_at": up_wall,
        "outage_s": round(t_up - t_down, 3),
        "reconnect_all_s": round(t_all - t_up, 3) if t_all is not None else None,
        "reconnect_all_within_bound": (t_all - t_up) <= RECONNECT_BOUND_S
        if t_all is not None
        else False,
        "nodes": per_node,
        "unexpected_new_nodes": sorted(n for n in connected_now if n not in before),
    }
    if t_all is not None:
        verdict = "within" if sample["reconnect_all_within_bound"] else "EXCEEDED"
        operator.say(
            f"all {len(before)} node(s) connected {sample['reconnect_all_s']:.2f} s after the "
            f"controller came back (bound {RECONNECT_BOUND_S:.0f} s: {verdict})"
        )
    return sample


def _wait_sequences(
    api: FabricApi, clock: Clock, cfg: RestartConfig, node_id: str, epoch: int | None
) -> list[int]:
    deadline = clock.monotonic() + cfg.sample_wait_s
    while True:
        found = _max_sequence(api, node_id, epoch) if epoch is not None else []
        if found or clock.monotonic() >= deadline:
            return found
        clock.sleep(cfg.poll_s)
