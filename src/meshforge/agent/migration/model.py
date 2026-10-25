"""Data of an identity migration: where, what was decided, what was done."""

from __future__ import annotations

import os
import sys
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
from meshforge.agent.policy import POLICY_FILE_NAME
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
POSIX_USER_IDENTITY: Final = Path(".local/share/meshforge") / IDENTITY_DIR_NAME
POSIX_USER_CONFIG: Final = Path(".config/meshforge") / CONFIG_FILE_NAME
WINDOWS_USER_IDENTITY: Final = Path("AppData/Local/MeshForge") / IDENTITY_DIR_NAME
"""V0.17 G4: a developer's Windows agent (``%LOCALAPPDATA%\\MeshForge``), relative
to the profile folder (``C:\\Users\\<name>``)."""
WINDOWS_USER_CONFIG: Final = Path("AppData/Roaming/MeshForge") / CONFIG_FILE_NAME
_WINDOWS: Final = sys.platform == "win32"
_USER_IDENTITY: Final = WINDOWS_USER_IDENTITY if _WINDOWS else POSIX_USER_IDENTITY
_USER_CONFIG: Final = WINDOWS_USER_CONFIG if _WINDOWS else POSIX_USER_CONFIG
_HOME_PREFIXES: Final = ("/home/", "/root/", "~")


def windows_profiles_root(environ: dict[str, str] | None = None) -> Path:
    """``C:\\Users`` (the parent of ``%PUBLIC%``, else ``%SystemDrive%\\Users``)."""
    env = os.environ if environ is None else environ
    public = env.get("PUBLIC", "").strip()
    if public:
        return Path(public).parent
    return Path((env.get("SystemDrive") or "C:") + "\\") / "Users"


def rerun_hint() -> str:
    """How to run the migration again, for this OS's messages."""
    if _WINDOWS:
        return (
            "the MeshForge installer again (or `meshforge-agent migrate-identity` "
            "in an administrator prompt)"
        )
    return "`sudo meshforge-agent migrate-identity`"


def admin_prefix() -> str:
    """What goes before an admin command in this OS's messages (Windows: an
    elevated prompt instead, said separately)."""
    return "" if _WINDOWS else "sudo "


def start_hint() -> str:
    """How to start the service once a blocked migration is resolved."""
    if _WINDOWS:
        return "`sc.exe start MeshForgeAgent` (administrator prompt)"
    return "`sudo systemctl enable --now meshforge-agent`"


def example_identity_dir() -> str:
    if _WINDOWS:
        return r"C:\Users\<user>\AppData\Local\MeshForge\identity"
    return "/home/<user>/.local/share/meshforge/identity"


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
    user_identity: Path = _USER_IDENTITY
    """A user's identity folder relative to their home (Windows: profile)."""
    user_config: Path = _USER_CONFIG

    @property
    def target_policy(self) -> Path:
        """The service's live owner policy: ``<data_dir>/policy.toml``, beside
        the identity folder (``agent.policy.policy_path``)."""
        return self.target_identity.parent / POLICY_FILE_NAME


def system_paths() -> Paths:
    dirs = platform_dirs(mode=InstallMode.SYSTEM)
    return Paths(
        target_identity=Path(dirs.data_dir) / IDENTITY_DIR_NAME,
        target_config=Path(dirs.config_dir) / CONFIG_FILE_NAME,
        home_root=windows_profiles_root() if _WINDOWS else Path("/home"),
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
    uid: int | None = None
    """``copy``: the user the file must belong to (``safe_source``)."""


@dataclass(slots=True)
class PolicyPlan:
    """The owner's live policy (``policy.toml``, G1 follow-up a): it moves with
    the identity it belongs to; a refused one is only a note (the service then
    starts with the safe default - it accepts no jobs - and the owner sets it
    again in the console)."""

    source: Path | None = None
    action: str = "none"
    """``none`` (no source), ``kept`` (target exists), ``copy``, ``refused``."""
    problem: str | None = None
    uid: int | None = None


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
    source_uid: int | None = None
    """The uid ``source`` / ``leftover_source`` must belong to (``safe_source``)."""
    config: ConfigPlan = field(default_factory=ConfigPlan)
    policy: PolicyPlan = field(default_factory=PolicyPlan)
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
            "policy": {
                "source": str(p.policy.source) if p.policy.source else None,
                "target": str(p.paths.target_policy),
                "action": p.policy.action,
                "problem": p.policy.problem,
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
                of_user = f" of user {p.user}" if p.user else ""
                out.append(
                    f"{prefix}{verb} node {node}{of_user} from {p.source} "
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
        if p.policy.action == "copy":
            out.append(
                f"  owner policy: {'would copy' if self.dry_run else 'copied'} "
                f"{p.policy.source} -> {p.paths.target_policy}"
            )
        elif p.policy.action == "kept" and p.policy.source is not None:
            out.append(
                f"  owner policy: {p.paths.target_policy} already exists; "
                f"{p.policy.source} left as is"
            )
        elif p.policy.action == "refused":
            out.append(
                f"  owner policy: not migrated ({p.policy.problem}); the service "
                "accepts no jobs until the owner sets it again (MeshForge console "
                "-> Settings, or `meshforge-agent policy set`)"
            )
        return out


# --------------------------------------------------------------------- plan
