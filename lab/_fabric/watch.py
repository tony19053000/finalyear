"""``fabric_check watch``: measure how fast the controller notices a node going
away and coming back (V0.3 success criterion 2; with an agent restart instead of
a cable pull it is the criterion-4 check).

One *repetition* walks the ``--expect`` states in order. For each state the
operator is prompted, presses Enter at the moment they act (pull the cable, plug
it back in), and the loop keeps polling ``GET /v1/nodes/{id}`` every
``poll_s`` until the state is reached. Everything is timed on the monotonic clock.

After the last step of a repetition the result also records:

* ``end_reason`` of the session that was live before the step - whether the OS
  reset the TCP connection (``stream_lost``) or the lease had to run out
  (``lease_expired``) is itself evidence about a real cable pull;
* ``restarted`` - did the agent **process** restart? From the per-process resource
  ``sequence`` (see :func:`timeline.restarted`), never from ``session_epoch``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from _fabric.api import ApiError, ControllerUnreachable, FabricApi
from _fabric.clock import Clock, Operator, iso
from _fabric.timeline import (
    BOUNDS_S,
    NodeView,
    Timeline,
    condition,
    latency,
    restarted,
    within_bound,
)

NOT_DETECTED: Final = "not_detected"
MARK_LATE: Final = "mark_after_change"
NOT_READY: Final = "node_not_online"
NO_MARK: Final = "no_operator_mark"
NODE_ID_CHANGED: Final = "node_id_changed"
NOT_FOUND: Final = "node_not_found"

_ACTIONS: Final = {
    "offline": "PULL the network cable of {host} (Wi-Fi must already be off on it) - "
    "press Enter at the same moment",
    "suspect": "PULL the network cable of {host} - press Enter at the same moment",
    "online": "PLUG the network cable of {host} back in - press Enter at the same moment",
}


class WatchAbort(Exception):  # noqa: N818 (a typed stop, carries failure_type)
    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass(frozen=True, slots=True)
class WatchConfig:
    node_id: str
    expect: tuple[str, ...]
    reps: int = 5
    poll_s: float = 0.1
    detect_timeout_s: float = 60.0
    ready_timeout_s: float = 180.0
    mark_timeout_s: float = 1800.0
    sample_wait_s: float = 15.0
    action_text: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.expect:
            raise ValueError("--expect needs at least one state")
        for state in self.expect:
            condition(state)  # raises for an unknown state
        if len(set(self.expect)) != len(self.expect):
            raise ValueError("--expect must not repeat a state within one repetition")
        if self.reps < 1:
            raise ValueError("--reps must be at least 1")


@dataclass
class WatchResult:
    node_id: str
    hostname: str = ""
    samples: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    aborted: bool = False


class _Poller:
    """Polls one node at a fixed cadence and feeds the timeline."""

    def __init__(self, api: FabricApi, clock: Clock, cfg: WatchConfig) -> None:
        self.api, self.clock, self.cfg = api, clock, cfg
        self.timeline = Timeline()
        self._next = clock.monotonic()

    def poll(self) -> NodeView | None:
        try:
            doc = self.api.node(self.cfg.node_id)
            view: NodeView | None = NodeView.from_json(doc)
        except ControllerUnreachable:
            view = None
        except ApiError as exc:
            if exc.failure_type == "not_found":
                raise WatchAbort(
                    NOT_FOUND, f"node {self.cfg.node_id} is not known"
                ) from None
            raise
        t = self.clock.monotonic()
        self.timeline.observe(t, iso(self.clock.wall()), view)
        return view

    def tick(self) -> None:
        """Sleep until the next poll slot (skipping slots a slow request overran)."""
        self._next += self.cfg.poll_s
        now = self.clock.monotonic()
        if self._next < now:
            self._next = now
        self.clock.sleep(self._next - now)


def run_watch(
    api: FabricApi,
    clock: Clock,
    operator: Operator,
    cfg: WatchConfig,
    result: WatchResult | None = None,
) -> WatchResult:
    """Run ``cfg.reps`` repetitions, filling ``result`` as it goes (pass your own
    to keep the finished repetitions when the operator presses Ctrl-C)."""
    poller = _Poller(api, clock, cfg)
    result = result if result is not None else WatchResult(node_id=cfg.node_id)
    for rep in range(cfg.reps):
        try:
            sample, error = _one_rep(poller, operator, cfg, rep, result)
        except WatchAbort as exc:
            result.errors.append(_error(rep, exc.failure_type, str(exc), fatal=True))
            result.aborted = True
            operator.say(f"ABORTED: {exc}")
            break
        if sample is not None:
            result.samples.append(sample)
        if error is not None:
            result.errors.append(error)
    return result


def _error(
    rep: int, failure_type: str, message: str, *, fatal: bool = False
) -> dict[str, Any]:
    return {
        "rep": rep,
        "phase": "measure",
        "failure_type": failure_type,
        "message": message,
        "fatal": fatal,
    }


def _wait_ready(poller: _Poller, operator: Operator, cfg: WatchConfig) -> NodeView:
    online = condition("online")
    deadline = poller.clock.monotonic() + cfg.ready_timeout_s
    said = False
    while True:
        view = poller.poll()
        if view is not None and online(view):
            return view
        if not said:
            operator.say(f"waiting for {cfg.node_id} to be online and connected ...")
            said = True
        if poller.clock.monotonic() >= deadline:
            raise WatchAbort(
                NOT_READY,
                f"{cfg.node_id} was not online+connected within {cfg.ready_timeout_s:.0f} s "
                "- restore it (cable in, agent running) and run again",
            )
        poller.tick()


def _max_sequence(api: FabricApi, node_id: str) -> int | None:
    try:
        samples = api.live_samples(node_id)
    except (ControllerUnreachable, ApiError):
        return None
    values = [s["sequence"] for s in samples if isinstance(s.get("sequence"), int)]
    return max(values) if values else None


def _new_sequences(poller: _Poller, epoch: int | None, cfg: WatchConfig) -> list[int]:
    """Sequences of live samples that arrived on session ``epoch`` (waits a bit)."""
    deadline = poller.clock.monotonic() + cfg.sample_wait_s
    while True:
        try:
            samples = poller.api.live_samples(cfg.node_id)
        except (ControllerUnreachable, ApiError):
            samples = []
        found = [
            s["sequence"]
            for s in samples
            if s.get("session_epoch") == epoch and isinstance(s.get("sequence"), int)
        ]
        if found or poller.clock.monotonic() >= deadline:
            return found
        poller.tick()


def _end_reason(api: FabricApi, node_id: str, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    try:
        sessions = api.sessions(node_id, limit=50)
    except (ControllerUnreachable, ApiError):
        return None
    for s in sessions:
        if s.get("session_id") == session_id:
            reason = s.get("end_reason")
            return reason if isinstance(reason, str) else None
    return None


def _same_host_elsewhere(api: FabricApi, view: NodeView) -> str | None:
    """A *different* connected node with this node's hostname (identity changed?)."""
    try:
        nodes = api.nodes()
    except (ControllerUnreachable, ApiError):
        return None
    for node in nodes:
        if (
            node.get("hostname") == view.hostname
            and node.get("node_id") != view.node_id
            and node.get("connected") is True
        ):
            return str(node.get("node_id"))
    return None


def _one_rep(
    poller: _Poller, operator: Operator, cfg: WatchConfig, rep: int, result: WatchResult
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    clock = poller.clock
    before = _wait_ready(poller, operator, cfg)
    result.hostname = before.hostname
    seq_before = _max_sequence(poller.api, cfg.node_id)
    rep_start = clock.monotonic()
    sample: dict[str, Any] = {
        "rep": rep,
        "epoch_before": before.session_epoch,
        "session_before": before.last_session_id,
        "sequence_before": seq_before,
        "marks": {},
    }
    operator.say(
        f"--- repetition {rep + 1}/{cfg.reps} on {before.hostname} ({cfg.node_id})"
    )
    after = rep_start  # an edge counts only after the previous step's edge
    for state in cfg.expect:
        text = cfg.action_text.get(state) or _ACTIONS[state]
        operator.prompt(text.format(host=before.hostname))
        edge, failure = _measure_step(poller, operator, cfg, state, after, sample)
        if edge is not None:
            after = edge
        if failure is not None:
            view = poller.timeline.last.view if poller.timeline.last else None
            other = _same_host_elsewhere(poller.api, view) if view is not None else None
            if other is not None:
                failure = _error(
                    rep,
                    NODE_ID_CHANGED,
                    f"{before.hostname} came back as a different node {other} "
                    f"(was {cfg.node_id}): the identity was not kept",
                )
            _finish(poller, cfg, sample, rep_start)
            operator.say(f"FAILED: {failure['message']}")
            if cfg.expect[-1] == "online" and state != "online":
                operator.say(
                    "restore the node (plug the cable back in) before the next repetition"
                )
            return sample, failure
    _finish(poller, cfg, sample, rep_start)
    return sample, None


def _measure_step(
    poller: _Poller,
    operator: Operator,
    cfg: WatchConfig,
    state: str,
    after: float,
    sample: dict[str, Any],
) -> tuple[float | None, dict[str, Any] | None]:
    """Wait for the operator's mark and for ``state``; return (edge, failure).

    The edge is searched from ``after`` (the previous step's edge), not from the
    prompt, so a change that happened before Enter was pressed is caught as
    :data:`MARK_LATE` rather than waited for until the timeout.
    """
    clock = poller.clock
    step_start = clock.monotonic()
    cond = condition(state)
    mark: float | None = None
    rep = int(sample["rep"])
    while True:
        poller.poll()
        if mark is None:
            mark = operator.poll_mark()
            if mark is not None:
                sample["marks"][state] = iso(clock.wall())
        edge = poller.timeline.first_edge(cond, after=after)
        now = clock.monotonic()
        if mark is None:
            if now - step_start >= cfg.mark_timeout_s:
                raise WatchAbort(NO_MARK, "nobody pressed Enter; stopping")
        elif edge is not None:
            seconds = latency(mark, edge)
            if seconds < 0:
                return edge, _error(
                    rep,
                    MARK_LATE,
                    f"{state} was reached {-seconds:.2f} s BEFORE Enter was pressed - "
                    "press Enter at the moment you act; this repetition does not count",
                )
            sample[f"{state}_s"] = seconds
            sample[f"{state}_within_bound"] = within_bound(state, seconds)
            bound = BOUNDS_S.get(state)
            limit = (
                f" (bound {bound:.0f} s: {'within' if seconds <= bound else 'EXCEEDED'})"
                if bound
                else ""
            )
            operator.say(f"{state} detected {seconds:.2f} s after Enter{limit}")
            return edge, None
        elif now - mark >= cfg.detect_timeout_s:
            sample[f"{state}_within_bound"] = False if state in BOUNDS_S else None
            return None, _error(
                rep,
                NOT_DETECTED,
                f"{state} not seen within {cfg.detect_timeout_s:.0f} s of Enter",
            )
        poller.tick()


def _finish(
    poller: _Poller, cfg: WatchConfig, sample: dict[str, Any], rep_start: float
) -> None:
    last = poller.timeline.last
    after = last.view if last is not None else None
    sample["epoch_after"] = after.session_epoch if after else None
    sample["session_after"] = after.last_session_id if after else None
    sample["end_reason"] = _end_reason(
        poller.api, cfg.node_id, sample["session_before"]
    )
    if (
        after is not None
        and condition("online")(after)
        and after.last_session_id != sample["session_before"]
    ):
        news = _new_sequences(poller, after.session_epoch, cfg)
        sample["first_new_sequence"] = min(news) if news else None
        sample["restarted"] = restarted(sample["sequence_before"], news)
    else:
        sample["first_new_sequence"] = None
        sample["restarted"] = None
    sample["changes"] = [
        c.to_json(rep_start)
        for c in poller.timeline.changes_between(rep_start, poller.clock.monotonic())
    ]
