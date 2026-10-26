"""``meshforge-agent doctor`` - what is true about this laptop's agent, in one
report (V0.17 G5, D393/D403).

The owner (or the troubleshooting page) runs it when something looks wrong. It
reads; it never changes anything:

* the version that answers (and the bundle it runs from, if installed);
* whether this laptop has joined a fabric;
* the last upgrade: rolled back, interrupted, or in progress
  (:mod:`meshforge.common.upgrade_state`, written by the packages);
* the same Docker / GPU / disk / firewall checks the local console shows.

Exit 0 when nothing needs the owner; 1 when an item is ``fail`` (an upgrade
was rolled back or interrupted). ``warn`` items (no Docker, no GPU) are
reports, not failures (D388).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from meshforge import __version__
from meshforge.agent.enrollment import is_enrolled
from meshforge.agent.settings import AgentSettings
from meshforge.common.buildinfo import bundle_root, read_build_info
from meshforge.common.upgrade_state import UpgradeNotice, program_dir, upgrade_notice

__all__ = ["DoctorItem", "DoctorReport", "ItemStatus", "gather", "render"]


class ItemStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorItem:
    id: str
    status: ItemStatus
    text: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "status": self.status.value,
            "text": self.text,
            "fix": self.fix,
        }


@dataclass(slots=True)
class DoctorReport:
    items: list[DoctorItem] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(i.status is ItemStatus.FAIL for i in self.items)

    def as_dict(self) -> dict[str, object]:
        return {"ok": not self.failed, "items": [i.as_dict() for i in self.items]}


def upgrade_item(notice: UpgradeNotice | None) -> DoctorItem:
    if notice is None:
        return DoctorItem("upgrade", ItemStatus.OK, "no unfinished or failed upgrade")
    if notice.kind == "in_progress":
        return DoctorItem("upgrade", ItemStatus.WARN, notice.message)
    fix = (
        "fix the cause (the installer's output says why), then install the new "
        "version again"
        if notice.kind == "rolled_back"
        else "Linux: sudo dpkg --configure -a; Windows: run the installer again"
    )
    return DoctorItem("upgrade", ItemStatus.FAIL, notice.message, fix)


def gather(
    settings: AgentSettings,
    *,
    root: Path | None = None,
    with_checks: bool = True,
    require_root: bool = True,
) -> DoctorReport:
    """The report (module docstring). ``root``: the bundle this runs from
    (default: :func:`bundle_root`); ``None`` = a developer checkout."""
    report = DoctorReport()
    root = bundle_root() if root is None else root
    info = read_build_info(root) if root is not None else None
    if info is None:
        report.items.append(
            DoctorItem(
                "version",
                ItemStatus.OK,
                f"meshforge-agent {__version__} (developer checkout, not installed)",
            )
        )
    else:
        report.items.append(
            DoctorItem(
                "version",
                ItemStatus.OK,
                f"meshforge-agent {info.version} (git {info.short_sha or '?'}) from {root}",
            )
        )
    if is_enrolled(settings.identity_dir):
        report.items.append(
            DoctorItem(
                "joined", ItemStatus.OK, f"joined a fabric ({settings.identity_dir})"
            )
        )
    else:
        report.items.append(
            DoctorItem(
                "joined",
                ItemStatus.WARN,
                "this laptop has not joined a fabric yet",
                "open MeshForge (app menu / Start menu) and paste a join code",
            )
        )
    if root is not None:
        report.items.append(
            upgrade_item(upgrade_notice(program_dir(root), require_root=require_root))
        )
    if with_checks:
        from meshforge.agent.console.checks import run_checks

        for check in asyncio.run(run_checks(settings, peer_endpoint=None)):
            status = ItemStatus.OK if check.status.value == "ok" else ItemStatus.WARN
            report.items.append(DoctorItem(check.id, status, check.why, check.fix))
    return report


def render(report: DoctorReport) -> str:
    width = max((len(i.id) for i in report.items), default=4)
    lines = []
    for item in report.items:
        lines.append(f"{item.status.value.upper():4}  {item.id:<{width}}  {item.text}")
        if item.fix and item.status is not ItemStatus.OK:
            lines.append(f"{'':4}  {'':<{width}}  fix: {item.fix}")
    lines.append(
        "doctor: something needs attention (FAIL above)"
        if report.failed
        else "doctor: nothing needs attention"
    )
    return "\n".join(lines)
