"""Print progress bars for STATUS.md from the roadmap checkboxes (stdlib only).

Counts every task line ``- [x] **0.4.1** ...`` under each ``## V…`` heading of
``docs/DEVELOPMENT_ROADMAP.md``. Deferred versions (ADR-0012: V0.11, V0.12, V0.13,
V0.16) and individually deferred tasks (struck through ``~~**0.2.8**~~``) are left
out. ``[x]`` counts as done; ``[~]`` (in progress / waiting on a lab step) is shown
separately and never counted as done.

Usage: ``uv run python scripts/status_bars.py`` and paste the output into STATUS.md.
"""

from __future__ import annotations

import re
from pathlib import Path

ROADMAP = Path(__file__).resolve().parent.parent / "docs" / "DEVELOPMENT_ROADMAP.md"
DEFERRED_VERSIONS = {"V0.11", "V0.12", "V0.13", "V0.16"}
WIDTH = 20

_VERSION = re.compile(r"^## (V\d+\.\d+) — (.+)$")
_TASK = re.compile(r"^- \[([ x~])\] (~~)?\*\*\d")


def bar(done: int, partial: int, total: int) -> str:
    """``█`` done, ``▒`` in progress, ``░`` not started."""
    if total == 0:
        return "░" * WIDTH
    full = round(WIDTH * done / total)
    half = min(WIDTH - full, round(WIDTH * partial / total))
    return "█" * full + "▒" * half + "░" * (WIDTH - full - half)


def main() -> None:
    rows: list[tuple[str, str, int, int, int]] = []
    current: list[object] | None = None
    for line in ROADMAP.read_text(encoding="utf-8").splitlines():
        if m := _VERSION.match(line):
            version, title = m.group(1), m.group(2)
            current = (
                None if version in DEFERRED_VERSIONS else [version, title, 0, 0, 0]
            )
            if current is not None:
                rows.append(current)  # type: ignore[arg-type]
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None or not (t := _TASK.match(line)) or t.group(2):
            continue
        state = t.group(1)
        current[2] += state == "x"  # type: ignore[operator]
        current[3] += state == "~"  # type: ignore[operator]
        current[4] += 1  # type: ignore[operator]

    done = sum(r[2] for r in rows)
    partial = sum(r[3] for r in rows)
    total = sum(r[4] for r in rows)
    print("```")
    pct = 100 * done // total
    counts = f"{done}/{total} done, {partial} in progress"
    print(f"TOTAL   {bar(done, partial, total)} {pct:3d}%  {counts}")
    print()
    for version, title, d, p, n in rows:
        pct = 100 * d // n if n else 0
        print(f"{version:<6}  {bar(d, p, n)} {pct:3d}%  {d:>2}/{n:<2}  {title}")
    print("```")
    print(
        "█ done `[x]` · ▒ in progress or waiting on a 🧑 lab step `[~]` · ░ not started"
    )


if __name__ == "__main__":
    main()
