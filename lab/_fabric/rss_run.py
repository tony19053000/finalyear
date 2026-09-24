"""The sampling loop behind ``agent_rss.py run`` (injected clock and process source)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from _fabric.clock import Clock, Operator, iso
from _fabric.rss import MIB, ProcessSource, ProcInfo, RssError, pick_agent


@dataclass(frozen=True, slots=True)
class RssConfig:
    hours: float = 24.0
    interval_s: float = 60.0
    warmup_min: float = 10.0
    pid: int | None = None

    def __post_init__(self) -> None:
        if self.hours <= 0 or self.interval_s <= 0 or self.warmup_min < 0:
            raise ValueError(
                "--hours and --interval must be positive, --warmup-min >= 0"
            )

    @property
    def target_s(self) -> float:
        return self.hours * 3600.0


def new_state(cfg: RssConfig, now: datetime, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "started_at": iso(now),
        "day": now.date().isoformat(),
        "hours": cfg.hours,
        "interval_s": cfg.interval_s,
        "warmup_min": cfg.warmup_min,
        "elapsed_s": 0.0,
        "saved_at": iso(now),
        "done": False,
        "runs": [],
        "gaps": [],
        "events": [],
        "samples": [],
    }


class RssSampler:
    """Samples every ``interval_s``; saves after every sample (cheap: once a minute)."""

    def __init__(
        self,
        source: ProcessSource,
        clock: Clock,
        operator: Operator,
        cfg: RssConfig,
        state: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        find_pid: Callable[[int], ProcInfo] | None = None,
    ) -> None:
        self.source, self.clock, self.operator, self.cfg = source, clock, operator, cfg
        self.state = state
        self._save_fn = save
        self._find_pid = find_pid
        self._current: ProcInfo | None = None

    def save(self) -> None:
        self.state["saved_at"] = iso(self.clock.wall())
        self._save_fn(self.state)

    def _event(self, kind: str, proc: ProcInfo | None, message: str) -> None:
        self.state["events"].append(
            {
                "at": iso(self.clock.wall()),
                "elapsed_s": self.state["elapsed_s"],
                "kind": kind,
                "pid": proc.pid if proc else None,
            }
        )
        self.operator.say(f"{iso(self.clock.wall())} {message}")

    def _locate(self) -> ProcInfo | None:
        try:
            if self.cfg.pid is not None and self._find_pid is not None:
                return self._find_pid(self.cfg.pid)  # --pid: never guess another one
            return pick_agent(self.source.processes())
        except RssError as exc:
            if exc.failure_type == "agent_ambiguous":
                raise
            return None

    def sample(self) -> None:
        proc = self._current
        rss = self.source.rss(proc.ident) if proc is not None else None
        if rss is None:
            found = self._locate()
            if found is None:
                last_kind = (
                    self.state["events"][-1]["kind"] if self.state["events"] else None
                )
                if last_kind != "agent_not_found":
                    self._event("agent_not_found", proc, "meshforge-agent not running")
                self._current = None
                return
            if self._current is None or found.ident != self._current.ident:
                last = self.state["samples"][-1] if self.state["samples"] else None
                is_new = (
                    last is not None
                    and (last["pid"], last["create_time"]) != found.ident
                )
                kind = "agent_restarted" if is_new else "agent_found"
                self._event(kind, found, f"{kind.replace('_', ' ')}: pid {found.pid}")
            self._current = found
            rss = self.source.rss(found.ident)
            if rss is None:
                return
        assert self._current is not None  # noqa: S101 (set on every path above)
        self.state["samples"].append(
            {
                "at": iso(self.clock.wall()),
                "elapsed_s": self.state["elapsed_s"],
                "pid": self._current.pid,
                "create_time": self._current.create_time,
                "rss_bytes": rss,
            }
        )

    def run(self) -> dict[str, Any]:
        now = iso(self.clock.wall())
        if self.state["runs"]:
            self.state["gaps"].append({"from": self.state["saved_at"], "to": now})
            last = self.state["samples"][-1] if self.state["samples"] else None
            if last is not None:  # resume on the same process if it is still there
                self._current = ProcInfo(
                    last["pid"], 0, "", (), float(last["create_time"])
                )
        self.state["runs"].append(
            {"started_at": now, "resumed": bool(self.state["runs"])}
        )
        t_prev = self.clock.monotonic()
        next_at = t_prev
        while True:
            self.sample()
            if self.state["samples"]:
                mib = self.state["samples"][-1]["rss_bytes"] / MIB
                self.operator.say(f"{iso(self.clock.wall())} rss {mib:.1f} MiB")
            self.save()
            if self.state["elapsed_s"] >= self.cfg.target_s:
                break
            next_at += self.cfg.interval_s
            now_t = self.clock.monotonic()
            self.clock.sleep(max(next_at - now_t, 0.0))
            t = self.clock.monotonic()
            self.state["elapsed_s"] = round(self.state["elapsed_s"] + (t - t_prev), 3)
            t_prev = t
        self.state["done"] = True
        self.save()
        return self.state
