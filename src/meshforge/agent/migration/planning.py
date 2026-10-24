"""Decide an identity migration without writing anything (``plan``)."""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from meshforge.agent.identity import (
    JSON_FILE_NAME,
    MIGRATION_PENDING_FILE_NAME,
    read_identity,
)
from meshforge.agent.migration.model import (
    _HOME_PREFIXES,
    _USER_CONFIG,
    _USER_IDENTITY,
    IDENTITY_FILES,
    ConfigPlan,
    MigrationOutcome,
    MigrationPlan,
    Paths,
)
from meshforge.common.errors import IdentityError, MeshForgeError


def _has_identity_files(directory: Path) -> bool:
    return any((directory / name).exists() for name in IDENTITY_FILES)


def _peek_node_id(directory: Path) -> str | None:
    """``node_id`` from ``node.json`` for a message, never trusted for more."""
    try:
        data = json.loads((directory / JSON_FILE_NAME).read_bytes())
    except (OSError, ValueError):
        return None
    value = data.get("node_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _home_of(user: str) -> Path | None:
    if sys.platform == "win32":  # pragma: no cover - the Windows path is G4
        return None
    import pwd

    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _user_of(path: Path) -> str | None:
    if sys.platform == "win32":  # pragma: no cover - the Windows path is G4
        return None
    import pwd

    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError):
        return None


def find_candidates(
    paths: Paths,
    *,
    explicit: Path | None,
    sudo_user: str | None,
    home_of: Callable[[str], Path | None] = _home_of,
) -> list[Path]:
    """Identity folders that hold identity files: ``explicit`` only when given;
    else ``$SUDO_USER``'s if it has one; else every
    ``<home_root>/*/.local/share/meshforge/identity``."""
    if explicit is not None:
        return [explicit]
    if sudo_user and sudo_user != "root":
        home = home_of(sudo_user)
        if home is not None and _has_identity_files(home / _USER_IDENTITY):
            return [home / _USER_IDENTITY]
    found = sorted(
        d
        for d in paths.home_root.glob(f"*/{_USER_IDENTITY.as_posix()}")
        if d.is_dir() and _has_identity_files(d)
    )
    return found


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def check_config(path: Path) -> str | None:
    """``None`` if ``path`` may become the service's config, else why not.

    The same settings model validates it (section keys are already strict);
    unknown *top-level* keys are refused here rather than silently dropped, and
    so are values naming a home directory (the service cannot read ``/home``:
    ``ProtectHome=yes``) and ``data_dir`` (the service's state lives in
    ``/var/lib/meshforge``)."""
    from meshforge.agent.settings import AgentSettings

    try:
        data = tomllib.loads(path.read_bytes().removeprefix(b"\xef\xbb\xbf").decode())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return f"cannot read {path} as TOML: {exc}"
    known = set(AgentSettings.model_fields)
    unknown = sorted(set(data) - known)
    if unknown:
        return (
            f"{path} has keys the agent does not know: {', '.join(unknown)} "
            "(a controller's or a typo); remove them, then re-run"
        )
    if "data_dir" in data:
        return (
            f"{path} sets data_dir; the service keeps its state in "
            "/var/lib/meshforge - remove the key, then re-run"
        )
    homes = sorted({s for s in _strings(data) if s.startswith(_HOME_PREFIXES)})
    if homes:
        return (
            f"{path} names paths in a home directory ({', '.join(homes)}), "
            "which the service cannot use; change or remove them, then re-run"
        )
    try:
        AgentSettings.load(path, agent={"discovery": {"enabled": True}})
    except MeshForgeError as exc:
        return f"{path} is not a valid agent configuration: {exc.message}"
    return None


def plan(
    paths: Paths,
    *,
    explicit: Path | None = None,
    config_from: Path | None = None,
    sudo_user: str | None = None,
    home_of: Callable[[str], Path | None] = _home_of,
    user_of: Callable[[Path], str | None] = _user_of,
) -> MigrationPlan:
    """Decide what to do; reads only (module docstring)."""
    target = paths.target_identity
    candidates = find_candidates(
        paths, explicit=explicit, sudo_user=sudo_user, home_of=home_of
    )
    pending = (target / MIGRATION_PENDING_FILE_NAME).exists()
    if _has_identity_files(target) and not pending:
        result = MigrationPlan(MigrationOutcome.TARGET_PRESENT, paths)
        result.target_node_id = _peek_node_id(target)
        same = [
            c
            for c in candidates
            if result.target_node_id is not None
            and _peek_node_id(c) == result.target_node_id
        ]
        if same:
            result.leftover_source = same[0]
            result.notes.append(
                f"{same[0]} is the same node and was not renamed by an earlier "
                "run; renaming it now"
            )
        others = [c for c in candidates if c not in same]
        for other in others:
            result.notes.append(
                f"{other} holds another identity ({_peek_node_id(other) or 'unreadable'}"
                ") and was left as is; the service keeps the one it has"
            )
        result.config = _plan_config(paths, config_from, None, sudo_user, home_of)
        _note_refused_config(result)
        return result

    if explicit is not None and not explicit.is_dir():
        return _blocked(
            paths,
            [f"--from {explicit}: no such directory"],
            "give the identity folder (it holds node.key and node.json)",
        )
    if not candidates:
        result = MigrationPlan(MigrationOutcome.NOTHING_TO_MIGRATE, paths)
        result.config = _plan_config(paths, config_from, None, sudo_user, home_of)
        _note_refused_config(result)
        return result
    if len(candidates) > 1:
        listed = [
            f"{c} (node_id {_peek_node_id(c) or 'unreadable'})" for c in candidates
        ]
        return _blocked(
            paths,
            ["more than one per-user identity found:", *listed],
            "choose the one this laptop should keep: `sudo meshforge-agent "
            "migrate-identity --from <folder>` (or rename the others, e.g. to "
            "identity.unused, and re-run); then `sudo systemctl enable --now "
            "meshforge-agent`",
        )
    source = candidates[0]
    try:
        identity = read_identity(source)
    except IdentityError as exc:
        return _blocked(
            paths,
            [f"the identity in {source} cannot be used: {exc.problem} ({exc.path})"],
            f"repair it, or move {source} aside if this laptop should start as a "
            "NEW node, then re-run `sudo meshforge-agent migrate-identity`",
        )
    result = MigrationPlan(MigrationOutcome.MIGRATED, paths, source, identity)
    result.user = sudo_user if explicit is None and sudo_user else user_of(source)
    result.config = _plan_config(paths, config_from, source, sudo_user, home_of)
    if result.config.action == "refused":
        return _blocked_config(result)
    return result


def _note_refused_config(result: MigrationPlan) -> None:
    """No identity moves, so a bad old config only goes unmigrated (the service
    starts from defaults); it blocks only a real migration (:func:`plan`)."""
    if result.config.action == "refused":
        result.notes.append(
            f"config not migrated: {result.config.problem}; the service starts "
            "from defaults"
        )


def _blocked(paths: Paths, problems: Sequence[str], fix: str) -> MigrationPlan:
    result = MigrationPlan(MigrationOutcome.BLOCKED, paths)
    result.problems.extend(problems)
    result.notes.append(f"fix: {fix}")
    return result


def _blocked_config(result: MigrationPlan) -> MigrationPlan:
    blocked = MigrationPlan(MigrationOutcome.BLOCKED, result.paths, result.source)
    blocked.config = result.config
    blocked.problems.append(result.config.problem or "config refused")
    blocked.notes.append(
        "fix: correct the file named above (or move it aside to start from "
        "defaults) and re-run `sudo meshforge-agent migrate-identity`"
    )
    return blocked


def _plan_config(
    paths: Paths,
    config_from: Path | None,
    identity_source: Path | None,
    sudo_user: str | None,
    home_of: Callable[[str], Path | None],
) -> ConfigPlan:
    source = config_from
    if source is None and identity_source is not None:
        # <home>/.local/share/meshforge/identity -> <home>/.config/meshforge/config.toml
        parts = identity_source.parts
        if len(parts) > len(_USER_IDENTITY.parts) and identity_source.match(
            f"*/{_USER_IDENTITY.as_posix()}"
        ):
            home = Path(*parts[: -len(_USER_IDENTITY.parts)])
            source = home / _USER_CONFIG
    if source is None and sudo_user and sudo_user != "root":
        sudo_home = home_of(sudo_user)
        source = sudo_home / _USER_CONFIG if sudo_home is not None else None
    if source is None or not source.is_file():
        if config_from is not None:
            return ConfigPlan(
                config_from, "refused", f"--config-from {config_from}: no such file"
            )
        return ConfigPlan()
    if paths.target_config.exists():
        return ConfigPlan(source, "kept")
    problem = check_config(source)
    if problem is not None:
        return ConfigPlan(source, "refused", problem)
    return ConfigPlan(source, "copy")


# ------------------------------------------------------------------ execute
