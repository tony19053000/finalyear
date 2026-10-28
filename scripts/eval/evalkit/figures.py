"""The report's figures (matplotlib, one fixed style, SVG, deterministic).

Determinism: the Agg backend, ``rcdefaults()`` and then one fixed style dict
(:data:`STYLE`), text kept as text in the SVG (``svg.fonttype = none`` - no
font-file glyph paths), a fixed ``svg.hashsalt`` for element ids and no
``Date`` metadata. With the locked matplotlib version the same analysis gives
the same SVG bytes (the CI reproducibility job checks it).

Every function takes the analysis document and writes one file; nothing is
read from anywhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

__all__ = ["STYLE", "write_figures"]

STYLE: Final[Mapping[str, Any]] = {
    "svg.fonttype": "none",
    "svg.hashsalt": "meshforge-v1-eval",
    "font.family": "DejaVu Sans",
    "font.size": 9.0,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 100.0,
    "savefig.dpi": 100.0,
    "path.simplify": False,
}
_COLOR: Final = "#1f4e79"
_GREY: Final = "#888888"


def _plt() -> Any:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    matplotlib.rcdefaults()
    params: Any = matplotlib.rcParams
    params.update(STYLE)
    return plt


def _save(plt: Any, fig: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", metadata={"Date": None})
    plt.close(fig)


def _speedups(
    plt: Any, analysis: Mapping[str, Any], reference: str, path: Path
) -> bool:
    rows = [
        c
        for c in analysis.get("comparisons", ())
        if c["reference"] == reference
        and c["scope"] == "all"
        and c["kind"] == "baseline"
        and c["speedup_geomean"] is not None
    ]
    if not rows:
        return False
    fig, ax = plt.subplots(figsize=(6.0, 0.45 * len(rows) + 1.2))
    labels = [c["arm"] for c in rows]
    centres = [c["speedup_geomean"] for c in rows]
    lows = [c["speedup_geomean"] - c["speedup_ci"][0] for c in rows]
    highs = [c["speedup_ci"][1] - c["speedup_geomean"] for c in rows]
    ys = list(range(len(rows)))
    ax.errorbar(centres, ys, xerr=[lows, highs], fmt="o", color=_COLOR, capsize=3)
    ax.axvline(1.0, color=_GREY, linestyle="--", linewidth=1)
    ax.set_yticks(ys, labels)
    ax.invert_yaxis()
    ax.set_xlabel(f"speedup vs {reference} (geometric mean, 95 % paired bootstrap CI)")
    ax.set_title(f"Completion time relative to {reference} (> 1 = faster)")
    fig.tight_layout()
    _save(plt, fig, path)
    return True


def _regret(plt: Any, analysis: Mapping[str, Any], path: Path) -> bool:
    per_arm = analysis.get("per_arm", {})
    arms = [a for a in analysis.get("arms", ()) if per_arm[a]["regret_ci"] is not None]
    if not arms:
        return False
    fig, ax = plt.subplots(figsize=(6.0, 0.45 * len(arms) + 1.2))
    means = [per_arm[a]["mean_regret_pct"] for a in arms]
    lows = [m - per_arm[a]["regret_ci"][0] for a, m in zip(arms, means, strict=True)]
    highs = [per_arm[a]["regret_ci"][1] - m for a, m in zip(arms, means, strict=True)]
    ys = list(range(len(arms)))
    ax.errorbar(means, ys, xerr=[lows, highs], fmt="s", color=_COLOR, capsize=3)
    ax.set_yticks(ys, arms)
    ax.invert_yaxis()
    ax.set_xlabel("mean regret vs the best arm per group (%, 95 % bootstrap CI)")
    ax.set_title("Regret against the per-group oracle (0 = always the best)")
    fig.tight_layout()
    _save(plt, fig, path)
    return True


def _makespans(plt: Any, analysis: Mapping[str, Any], path: Path) -> bool:
    groups: Sequence[Mapping[str, Any]] = analysis.get("groups", ())
    arms: Sequence[str] = analysis.get("arms", ())
    if not groups or not arms:
        return False
    width = 0.8 / len(arms)
    fig, ax = plt.subplots(figsize=(max(6.0, 1.1 * len(groups) + 2.0), 3.6))
    for index, arm in enumerate(arms):
        xs, ys = [], []
        for position, group in enumerate(groups):
            summary = group["arms"][arm]["makespan_ms"]
            if group["arms"][arm]["state"] == "measured" and summary is not None:
                xs.append(position + (index - (len(arms) - 1) / 2) * width)
                ys.append(summary["median"] / 1000.0)
        ax.bar(xs, ys, width=width, label=arm)
    ax.set_xticks(
        range(len(groups)), [g["key"] for g in groups], rotation=30, ha="right"
    )
    ax.set_ylabel("median makespan (s)")
    ax.set_title("Median completion time per group and arm")
    ax.legend(ncol=min(len(arms), 5), fontsize=8)
    fig.tight_layout()
    _save(plt, fig, path)
    return True


def _overhead(plt: Any, analysis: Mapping[str, Any], path: Path) -> bool:
    per_arm = analysis.get("per_arm", {})
    bars: list[tuple[str, float]] = []
    for arm in analysis.get("arms", ()):
        for mode, summary in per_arm[arm]["overhead_ms_by_mode"].items():
            if summary is not None:
                bars.append((f"{arm} {mode}", summary["median"] / 1000.0))
    if not bars:
        return False
    fig, ax = plt.subplots(figsize=(6.0, 0.4 * len(bars) + 1.2))
    ys = list(range(len(bars)))
    ax.barh(ys, [b[1] for b in bars], color=_COLOR)
    ax.set_yticks(ys, [b[0] for b in bars])
    ax.invert_yaxis()
    ax.set_xlabel("median overhead = end-to-end - run phase (s)")
    ax.set_title("What the fabric adds around the program (RQ4)")
    fig.tight_layout()
    _save(plt, fig, path)
    return True


def write_figures(analysis: Mapping[str, Any], folder: Path) -> list[str]:
    """Write every figure the data supports; their file names, sorted."""
    plt = _plt()
    made: list[str] = []
    jobs: tuple[tuple[str, Callable[[Path], bool]], ...] = (
        ("speedup_vs_B0.svg", lambda p: _speedups(plt, analysis, "B0", p)),
        ("speedup_vs_B5.svg", lambda p: _speedups(plt, analysis, "B5", p)),
        ("regret_by_arm.svg", lambda p: _regret(plt, analysis, p)),
        ("makespan_by_group.svg", lambda p: _makespans(plt, analysis, p)),
        ("overhead_by_mode.svg", lambda p: _overhead(plt, analysis, p)),
    )
    for name, make in jobs:
        if make(folder / name):
            made.append(name)
    return sorted(made)
