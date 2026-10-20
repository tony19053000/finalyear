"""A split job's progress in the terminal (V0.10 G7, D270; roadmap 0.10.8).

``compute watch JOB`` and ``compute run -f`` for a ``split:`` job poll
``GET /v1/jobs/{id}/split`` (the numbers are the controller's rows, read on
every request - checklist 37) every ``[cli] progress_interval_s`` and show:

* one bar line: items and chunks done, failed tries, ETA, elapsed vs predicted;
* one line per lane (node): its state, chunks / items committed, its measured
  items/s and the chunks it holds now;
* the job's warnings (``nondeterministic_chunk``).

On a terminal the block is redrawn in place on **stderr** (stdout stays for
the command's result); in a pipe one bar line is printed when it changes.
(``compute watch --json`` does not loop: it prints the summary once, like
every ``--json`` read.) Every value from the network is flattened before it
is printed (no escape sequence reaches the terminal).

Pure rendering (:func:`progress_lines`) is separate from the loop
(:func:`watch_split`) so it is unit-tested without a controller.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from meshforge.cli import render

__all__ = [
    "TERMINAL_STATES",
    "bar",
    "format_duration",
    "progress_lines",
    "watch_split",
]

TERMINAL_STATES: Final = frozenset({"succeeded", "failed", "cancelled"})
BAR_WIDTH: Final = 24
MAX_LANE_LINES: Final = 16
MAX_CURRENT_SHOWN: Final = 4
MAX_TRANSIENT_FAILURES: Final = 5
"""Polls in a row that may fail (network) before the watch gives up."""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _num(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def format_duration(seconds: object) -> str:
    """``42s``, ``3m05s``, ``1h02m``; ``-`` when unknown."""
    value = _num(seconds)
    if value is None or value < 0:
        return "-"
    total = round(value)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{total % 3600 // 60:02d}m"


def bar(done: int, total: int, *, width: int = BAR_WIDTH) -> str:
    """``[#######-----------------]``; an empty total is an empty bar."""
    filled = 0 if total <= 0 else min(width, done * width // total)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _rate(value: object) -> str:
    rate = _num(value)
    return "-" if rate is None else f"{rate:.3g} items/s"


def progress_lines(summary: Mapping[str, Any]) -> list[str]:
    """The block for one summary (pure; values flattened)."""
    chunks = summary.get("chunks")
    counts = chunks if isinstance(chunks, Mapping) else {}
    items_total = _int(summary.get("items_total"))
    items_done = _int(summary.get("items_done"))
    state = render.flat(summary.get("job_state") or "?")
    percent = 0 if items_total <= 0 else items_done * 100 // items_total
    lanes = summary.get("lanes")
    lane_list = (
        [x for x in lanes if isinstance(x, Mapping)] if isinstance(lanes, list) else []
    )
    failed_tries = sum(_int(lane.get("failed_tries")) for lane in lane_list)
    head = (
        f"{bar(items_done, items_total)} {items_done}/{items_total} items "
        f"({percent}%)  {_int(counts.get('done'))}/{_int(counts.get('total'))} "
        f"chunks  {state}"
    )
    extras = []
    if _int(counts.get("leased")):
        extras.append(f"{_int(counts.get('leased'))} running")
    if _int(counts.get("failed")):
        extras.append(f"{_int(counts.get('failed'))} chunks failed")
    if failed_tries:
        extras.append(f"{failed_tries} failed tries")
    if _int(summary.get("speculative_live")):
        extras.append(f"{_int(summary.get('speculative_live'))} speculative")
    if state not in TERMINAL_STATES:
        extras.append(f"ETA {format_duration(summary.get('eta_s'))}")
    elapsed = format_duration(summary.get("elapsed_s"))
    predicted = summary.get("predicted_s")
    extras.append(
        f"elapsed {elapsed}"
        + (f" / predicted {format_duration(predicted)}" if predicted else "")
    )
    if state == "collecting":
        extras.append("combining")
    lines = [head + "  " + ", ".join(extras)]
    for lane in lane_list[:MAX_LANE_LINES]:
        current = lane.get("current")
        held = [
            f"{_int(c.get('chunk_index'))}" + ("*" if c.get("speculative") else "")
            for c in (current if isinstance(current, list) else [])
            if isinstance(c, Mapping)
        ]
        now = ", ".join(held[:MAX_CURRENT_SHOWN]) + (
            ", ..." if len(held) > MAX_CURRENT_SHOWN else ""
        )
        lines.append(
            f"  {render.clip(lane.get('node_id'))}  "
            f"{render.flat(lane.get('state') or '?'):<10} "
            f"{_int(lane.get('chunks_done')):>5} chunks "
            f"{_int(lane.get('items_done')):>7} items  "
            f"{_rate(lane.get('rate_items_s')):>16}" + (f"  now: {now}" if now else "")
        )
    hidden = _int(summary.get("lanes_total")) - min(len(lane_list), MAX_LANE_LINES)
    if hidden > 0:
        lines.append(f"  ... {hidden} more lane(s)")
    warnings = summary.get("warnings")
    lines.extend(
        f"  warning: {render.flat(item.get('code'))} x{_int(item.get('count'))}"
        for item in (warnings if isinstance(warnings, list) else [])
        if isinstance(item, Mapping)
    )
    return lines


def _comparable(summary: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes two polls different for the pipe / JSON output."""
    chunks = summary.get("chunks")
    return (
        summary.get("job_state"),
        summary.get("items_done"),
        tuple(sorted(chunks.items())) if isinstance(chunks, Mapping) else None,
        summary.get("lanes_total"),
        repr(summary.get("lanes")),
        repr(summary.get("warnings")),
    )


def watch_split(
    fetch: Callable[[], Mapping[str, Any]],
    *,
    interval_s: float,
    is_terminal: bool | None = None,
    sleep: Callable[[float], None] | None = None,
    transient: Callable[[Exception], bool] = lambda _: False,
) -> Mapping[str, Any]:
    """Poll ``fetch`` until the job ends; return the final summary. A poll
    failing with an error ``transient`` accepts (a network blip) is retried -
    at most :data:`MAX_TRANSIENT_FAILURES` in a row, then it propagates."""
    pause = sleep or time.sleep
    console = render.console(stderr=True)
    tty = console.is_terminal if is_terminal is None else is_terminal
    live = None
    if tty:
        from rich.live import Live
        from rich.text import Text

        live = Live(Text(""), console=console, auto_refresh=False, transient=False)
        live.start()
    last: tuple[Any, ...] | None = None
    failures = 0
    try:
        while True:
            try:
                summary = fetch()
                failures = 0
            except Exception as exc:
                failures += 1
                if not transient(exc) or failures >= MAX_TRANSIENT_FAILURES:
                    raise
                pause(interval_s)
                continue
            comparable = _comparable(summary)
            changed = comparable != last
            last = comparable
            done = str(summary.get("job_state")) in TERMINAL_STATES
            if live is not None:
                from rich.text import Text

                live.update(Text("\n".join(progress_lines(summary))), refresh=True)
            elif changed:
                render.line(progress_lines(summary)[0], err=True)
            if done:
                return summary
            pause(interval_s)
    finally:
        if live is not None:
            live.stop()
