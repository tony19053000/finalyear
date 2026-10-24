"""Data of an identity migration: where, what was decided, what was done."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from meshforge.agent.identity import (
    CA_CERT_FILE_NAME,
    JSON_FILE_NAME,
    KEY_FILE_NAME,
    NODE_CERT_FILE_NAME,
    ROTATION_KEY_FILE_NAME,
    NodeIdentity,
)
from meshforge.agent.settings import IDENTITY_DIR_NAME
from meshforge.common.config import CONFIG_FILE_NAME, InstallMode, platform_dirs
from meshforge.common.exit_codes import ExitCode

SERVICE_USER: Final = "meshforge"
"""The system user the ``.deb`` creates and the unit runs as (D386)."""
USER_UNIT_NAME: Final = "meshforge-agent.service"
IDENTITY_FILES: Final = (
    KEY_FILE_NAME,
    JSON_FILE_NAME,
    NODE_CERT_FILE_NAME,
    CA_CERT_FILE_NAME,
    ROTATION_KEY_FILE_NAME,
)
"""What moves: the key, its metadata, the enrolment (cert + pinned CA) and a key
staged by ``rotate-key``. Not the lock (each directory has its own)."""
_USER_IDENTITY: Final = Path(".local/share/meshforge") / IDENTITY_DIR_NAME
_USER_CONFIG: Final = Path(".config/meshforge") / CONFIG_FILE_NAME
_HOME_PREFIXES: Final = ("/home/", "/root/", "~")


class MigrationOutcome(StrEnum):
    MIGRATED = "migrated"
    NOTHING_TO_MIGRATE = "nothing_to_migrate"
    TARGET_PRESENT = "target_present"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Paths:
    """Where things are; every field injectable so tests never touch ``/``."""

    target_identity: Path
    target_config: Path
    home_root: Path = Path("/home")


def system_paths() -> Paths:
    dirs = platform_dirs(mode=InstallMode.SYSTEM)
    return Paths(
        target_identity=Path(dirs.data_dir) / IDENTITY_DIR_NAME,
        target_config=Path(dirs.config_dir) / CONFIG_FILE_NAME,
    )


@dataclass(frozen=True, slots=True)
class Owner:
    """The service account files are handed to (``None`` fields: leave as is)."""

    uid: int | None
    gid: int | None


@dataclass(slots=True)
class ConfigPlan:
    source: Path | None = None
    action: str = "none"
    """``none`` (no source), ``kept`` (target exists), ``copy``, ``refused``."""
    problem: str | None = None


@dataclass(slots=True)
class MigrationPlan:
    outcome: MigrationOutcome
    paths: Paths
    source: Path | None = None
    identity: NodeIdentity | None = None
    target_node_id: str | None = None
    leftover_source: Path | None = None
    """``target_present``: a source of the same node an interrupted run left."""
    user: str | None = None
    config: ConfigPlan = field(default_factory=ConfigPlan)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MigrationReport:
    plan: MigrationPlan
    dry_run: bool
    actions: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> ExitCode:
        return (
            ExitCode.CONFIG_ERROR
            if self.plan.outcome is MigrationOutcome.BLOCKED
            else ExitCode.OK
        )

    def as_dict(self) -> dict[str, Any]:
        p = self.plan
        return {
            "outcome": p.outcome.value,
            "dry_run": self.dry_run,
            "source": str(p.source) if p.source else None,
            "target": str(p.paths.target_identity),
            "node_id": str(p.identity.node_id) if p.identity else p.target_node_id,
            "user": p.user,
            "config": {
                "source": str(p.config.source) if p.config.source else None,
                "target": str(p.paths.target_config),
                "action": p.config.action,
                "problem": p.config.problem,
            },
            "problems": list(p.problems),
            "notes": list(p.notes),
            "actions": list(self.actions),
        }

    def lines(self) -> list[str]:
        p = self.plan
        prefix = "(dry run) " if self.dry_run else ""
        out: list[str] = []
        match p.outcome:
            case MigrationOutcome.MIGRATED:
                verb = "would migrate" if self.dry_run else "migrated"
                node = p.identity.node_id if p.identity is not None else "?"
                out.append(
                    f"{prefix}{verb} node {node} from {p.source} "
                    f"to {p.paths.target_identity} (same node_id: the dashboard row "
                    "and its history stay)"
                )
            case MigrationOutcome.TARGET_PRESENT:
                out.append(
                    f"{prefix}the service already has an identity "
                    f"({p.target_node_id or 'unknown node_id'}) in "
                    f"{p.paths.target_identity}; nothing migrated"
                )
            case MigrationOutcome.NOTHING_TO_MIGRATE:
                out.append(
                    f"{prefix}no per-user identity found; a new one is created in "
                    f"{p.paths.target_identity} when this laptop joins a fabric"
                )
            case MigrationOutcome.BLOCKED:
                out.append(
                    "identity migration STOPPED - nothing was copied and no "
                    "existing identity was changed; the agent service will not "
                    "create a new identity until this is resolved:"
                )
        out.extend(f"  problem: {text}" for text in p.problems)
        out.extend(f"  note: {text}" for text in p.notes)
        out.extend(f"  {prefix}done: {text}" for text in self.actions)
        if p.config.action == "copy":
            out.append(
                f"  config: {'would copy' if self.dry_run else 'copied'} "
                f"{p.config.source} -> {p.paths.target_config}"
            )
        elif p.config.action == "kept" and p.config.source is not None:
            out.append(
                f"  config: {p.paths.target_config} already exists; "
                f"{p.config.source} left as is"
            )
        return out


# --------------------------------------------------------------------- plan
