"""Decide an identity migration without writing anything (``plan``)."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from meshforge.agent.identity import (
    JSON_FILE_NAME,
    KEY_FILE_NAME,
    MIGRATION_PENDING_FILE_NAME,
    NODE_CERT_FILE_NAME,
    NodeIdentity,
    read_identity,
)
from meshforge.agent.migration.model import (
    _HOME_PREFIXES,
    IDENTITY_FILES,
    ConfigPlan,
    MigrationOutcome,
    MigrationPlan,
    Paths,
    PolicyPlan,
    example_identity_dir,
    rerun_hint,
    start_hint,
)
from meshforge.agent.migration.safe_source import (
    ReadFile,
    UnsafeSourceError,
    expected_uid,
    opened_dir,
    read_file,
    read_files,
)
from meshforge.agent.policy import POLICY_FILE_NAME
from meshforge.common.config import InstallMode, platform_dirs
from meshforge.common.errors import IdentityError, MeshForgeError

_WINDOWS_HOME: Final = re.compile(
    r"^[A-Za-z]:[\\/]+(users|documents and settings)[\\/]", re.I
)
"""V0.17 G4: a Windows profile path (the virtual service account cannot read it)."""


def _has_identity_files(directory: Path) -> bool:
    # lexists: a symlinked file still makes a candidate - one that is then
    # refused with the reason, rather than silently skipped.
    return any(os.path.lexists(directory / name) for name in IDENTITY_FILES)


def _peek_node_id(directory: Path, home_root: Path) -> str | None:
    """``node_id`` from ``node.json`` for a message, never trusted for more
    (read like every source: ``safe_source``)."""
    try:
        with opened_dir(directory, expected_uid(directory, home_root)) as held:
            found = read_file(held, JSON_FILE_NAME)
        data = json.loads(found.data) if found is not None else None
    except (UnsafeSourceError, OSError, ValueError):
        return None
    value = data.get("node_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _home_of(user: str) -> Path | None:
    if sys.platform == "win32":  # pragma: no cover - Windows (V0.17 G4)
        # The installer runs elevated as the user who started it (the common
        # case): that user's profile is %USERPROFILE%. Any other name: unknown
        # here, the profiles are then searched one by one.
        if user and user.lower() == os.environ.get("USERNAME", "").lower():
            profile = os.environ.get("USERPROFILE", "").strip()
            return Path(profile) if profile else None
        return None
    import pwd

    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return None


def _user_of(path: Path) -> str | None:
    if sys.platform == "win32":  # pragma: no cover - Windows: profile_name()
        return None
    import pwd

    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (KeyError, OSError):
        return None


def identity_from(files: Mapping[str, ReadFile], source: Path) -> NodeIdentity:
    """Validate identity files already read from ``source`` (through checked fds)
    with the agent's own loader, on a private copy; errors name ``source``."""
    with tempfile.TemporaryDirectory(prefix="meshforge-migrate-") as tmp:
        scratch = Path(tmp)
        scratch.chmod(0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for name, found in files.items():
            fd = os.open(scratch / name, flags, 0o600)
            try:
                os.write(fd, found.data)
                if hasattr(os, "fchmod"):  # POSIX: the loader checks the modes
                    os.fchmod(fd, found.mode)
            finally:
                os.close(fd)
        try:
            identity = read_identity(scratch)
        except IdentityError as exc:
            where = source / exc.path.name if exc.path.parent == scratch else source
            raise IdentityError(
                exc.problem.replace(tmp, str(source)),
                where,
                exc.remedy.replace(tmp, str(source)),
            ) from None
        _check_enrolment(identity, source, tmp)
    return dataclasses.replace(identity, path=source)


def _check_enrolment(identity: NodeIdentity, source: Path, scratch: str) -> None:
    """An enrolled identity moves only with a usable enrolment: ``node.crt``
    parses, was issued by ``ca.crt`` (present, a CA), holds this key and names
    this node - the agent's own check (``enrollment.load``). Never enrolled (no
    ``node.crt``) is fine: the service joins later."""
    from meshforge.agent.enrollment import JoinError, load

    try:
        load(identity)
    except JoinError as exc:
        problem = exc.message.replace(scratch, str(source)).split("; re-join:")[0]
        # "the enrolment in <dir> is unusable (<reason>: <detail>)" -> the part
        # in brackets (the directory is named by the caller already)
        if " is unusable (" in problem:
            problem = problem.split(" is unusable (", 1)[1].removesuffix(")")
        raise IdentityError(
            f"its enrolment is unusable ({problem})",
            source / NODE_CERT_FILE_NAME,
            "repair it (copy back the node.crt and ca.crt from a backup), or move "
            f"{source} aside if this laptop should start as a NEW node",
        ) from None


def read_source_identity(source: Path, uid: int) -> NodeIdentity:
    with opened_dir(source, uid) as held:
        files = read_files(held, IDENTITY_FILES)
    return identity_from(files, source)


def find_candidates(
    paths: Paths,
    *,
    explicit: Path | None,
    sudo_user: str | None,
    home_of: Callable[[str], Path | None] = _home_of,
) -> list[Path]:
    """Identity folders that hold identity files: ``explicit`` only when given;
    else ``$SUDO_USER``'s (Windows: the installing user's) if it has one; else
    every ``<home_root>/*/<paths.user_identity>`` (Linux
    ``.local/share/meshforge/identity``, Windows
    ``AppData/Local/MeshForge/identity`` under ``C:\\Users``)."""
    if explicit is not None:
        return [explicit]
    rel = paths.user_identity
    if sudo_user and sudo_user != "root":
        home = home_of(sudo_user)
        if home is not None and _has_identity_files(home / rel):
            return [home / rel]
    found = sorted(
        d
        for d in paths.home_root.glob(f"*/{rel.as_posix()}")
        if d.is_dir() and _has_identity_files(d)
    )
    return found


def profile_name(path: Path, home_root: Path) -> str | None:
    """The home (profile) folder name ``path`` is in, below ``home_root``."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(home_root))
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _is_under(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(folder))
    except ValueError:
        return False
    return True


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def check_config_bytes(raw: bytes, path: Path) -> str | None:
    """``None`` if ``raw`` (the content of ``path``, already read through
    :mod:`~meshforge.agent.migration.safe_source`) may become the service's
    config, else why not.

    The same settings model validates it (section keys are already strict);
    unknown *top-level* keys are refused here rather than silently dropped, and
    so are values naming a home directory (the service cannot read ``/home``:
    ``ProtectHome=yes``) and ``data_dir`` (the service's state lives in
    ``/var/lib/meshforge``). There is deliberately no path-based variant: root
    never reads a user's file except through ``safe_source``."""
    from meshforge.agent.settings import AgentSettings

    try:
        data = tomllib.loads(raw.removeprefix(b"\xef\xbb\xbf").decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
            f"{platform_dirs(mode=InstallMode.SYSTEM).data_dir} - remove the key, "
            "then re-run"
        )
    homes = sorted(
        {
            s
            for s in _strings(data)
            if s.startswith(_HOME_PREFIXES) or _WINDOWS_HOME.match(s)
        }
    )
    if homes:
        return (
            f"{path} names paths in a home directory ({', '.join(homes)}), "
            "which the service cannot use; change or remove them, then re-run"
        )
    with tempfile.TemporaryDirectory(prefix="meshforge-migrate-") as tmp:
        scratch = Path(tmp) / path.name
        scratch.write_bytes(raw)
        try:
            AgentSettings.load(scratch, agent={"discovery": {"enabled": True}})
        except MeshForgeError as exc:
            message = exc.message.replace(str(scratch), str(path))
            return f"{path} is not a valid agent configuration: {message}"
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
        result.target_node_id = _peek_node_id(target, paths.home_root)
        same = [
            c
            for c in candidates
            if result.target_node_id is not None
            and _peek_node_id(c, paths.home_root) == result.target_node_id
        ]
        if same:
            result.leftover_source = same[0]
            result.source_uid = expected_uid(same[0], paths.home_root)
            result.notes.append(
                f"{same[0]} is the same node and was not renamed by an earlier "
                "run; renaming it now"
            )
        others = [c for c in candidates if c not in same]
        for other in others:
            refusal = _explicit_problem(other)
            if refusal is not None:
                result.notes.append(f"{other}: {refusal}; nothing to migrate")
                continue
            other_id = _peek_node_id(other, paths.home_root)
            held = (
                f"another identity ({other_id})"
                if other_id
                else "an identity that cannot be read safely (or is damaged)"
            )
            result.notes.append(
                f"{other} holds {held} and was left as is; the service keeps the "
                "one it has"
            )
        result.config = _plan_config(paths, config_from, None, sudo_user, home_of)
        _note_refused_config(result)
        return result

    if explicit is not None:
        refusal = _explicit_problem(explicit)
        if refusal is not None:
            return _blocked(
                paths,
                [f"--from {explicit}: {refusal}"],
                "give the identity folder (it holds node.key and node.json), e.g. "
                f"{example_identity_dir()}",
            )
    if not candidates:
        result = MigrationPlan(MigrationOutcome.NOTHING_TO_MIGRATE, paths)
        result.config = _plan_config(paths, config_from, None, sudo_user, home_of)
        _note_refused_config(result)
        return result
    if len(candidates) > 1:
        listed = [
            f"{c} (node_id {_peek_node_id(c, paths.home_root) or 'unreadable'})"
            for c in candidates
        ]
        return _blocked(
            paths,
            ["more than one per-user identity found:", *listed],
            "choose the one this laptop should keep: `"
            + ("" if sys.platform == "win32" else "sudo ")
            + "meshforge-agent migrate-identity --from <folder>` (or rename the "
            f"others, e.g. to identity.unused, and re-run {rerun_hint()}); then "
            f"{start_hint()}",
        )
    source = candidates[0]
    try:
        uid = expected_uid(source, paths.home_root)
        identity = read_source_identity(source, uid)
    except UnsafeSourceError as exc:
        return _blocked(
            paths,
            [f"the identity in {source} is not safe to migrate: {exc}"],
            f"make {source} a plain folder (no symbolic links) of the user it "
            "belongs to, with plain files of that user and no group/other write "
            "access - or move it aside if this laptop should start as a NEW node - "
            f"then re-run {rerun_hint()}",
        )
    except IdentityError as exc:
        return _blocked(
            paths,
            [f"the identity in {source} cannot be used: {exc.problem} ({exc.path})"],
            f"repair it, or move {source} aside if this laptop should start as a "
            f"NEW node, then re-run {rerun_hint()}",
        )
    result = MigrationPlan(MigrationOutcome.MIGRATED, paths, source, identity)
    result.source_uid = uid
    sudo_home = home_of(sudo_user) if sudo_user else None
    if explicit is None and sudo_user and sudo_home is not None:
        # Only when the identity really is in that user's home (another user's
        # found by the search is *that* user's).
        result.user = (
            sudo_user
            if _is_under(source, sudo_home)
            else (user_of(source) or profile_name(source, paths.home_root))
        )
    elif explicit is None and sudo_user:
        result.user = sudo_user
    else:
        result.user = user_of(source) or profile_name(source, paths.home_root)
    result.config = _plan_config(paths, config_from, source, sudo_user, home_of)
    if result.config.action == "refused":
        return _blocked_config(result)
    result.policy = plan_policy(paths, source, uid)
    return result


def _explicit_problem(explicit: Path) -> str | None:
    if not os.path.lexists(explicit):
        return "no such directory"
    if not explicit.is_dir():
        return "not a directory"
    if not any(
        os.path.lexists(explicit / name) for name in (KEY_FILE_NAME, JSON_FILE_NAME)
    ):
        return "not a MeshForge identity directory (it holds no node.key or node.json)"
    return None


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
        f"defaults) and re-run {rerun_hint()}"
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
    user_identity, user_config = paths.user_identity, paths.user_config
    if source is None and identity_source is not None:
        # <home>/.local/share/meshforge/identity -> <home>/.config/meshforge/config.toml
        # (Windows: <profile>/AppData/Local/... -> <profile>/AppData/Roaming/...)
        parts = identity_source.parts
        if len(parts) > len(user_identity.parts) and identity_source.match(
            f"*/{user_identity.as_posix()}"
        ):
            home = Path(*parts[: -len(user_identity.parts)])
            source = home / user_config
    if source is None and sudo_user and sudo_user != "root":
        sudo_home = home_of(sudo_user)
        source = sudo_home / user_config if sudo_home is not None else None
    if source is None or not os.path.lexists(source):
        if config_from is not None:
            return ConfigPlan(
                config_from, "refused", f"--config-from {config_from}: no such file"
            )
        return ConfigPlan()
    if paths.target_config.exists():
        return ConfigPlan(source, "kept")
    try:
        uid = expected_uid(source, paths.home_root)
        raw = read_source_config(source, uid).data
    except UnsafeSourceError as exc:
        return ConfigPlan(source, "refused", str(exc))
    problem = check_config_bytes(raw, source)
    if problem is not None:
        return ConfigPlan(source, "refused", problem)
    return ConfigPlan(source, "copy", uid=uid)


def policy_problem(raw: bytes, path: Path) -> str | None:
    """``None`` if ``raw`` (a user's ``policy.toml``) is a valid live owner
    policy: the agent's own parser and validator (``agent.policy``), on the
    default base. The service re-validates it against its real configuration
    and fails closed if that combination is not valid."""
    from meshforge.agent.policy import effective_settings, parse_overlay
    from meshforge.agent.settings import PolicySettings

    try:
        effective_settings(PolicySettings(), parse_overlay(raw, path))
    except MeshForgeError as exc:
        return exc.message
    return None


def plan_policy(paths: Paths, identity_source: Path, uid: int) -> PolicyPlan:
    """The owner policy beside ``identity_source`` (``<user data dir>/policy.toml``),
    read through ``safe_source`` and validated; only ever a note when refused."""
    source = identity_source.parent / POLICY_FILE_NAME
    if not os.path.lexists(source):
        return PolicyPlan()
    if os.path.lexists(paths.target_policy):
        return PolicyPlan(source, "kept")
    try:
        found = read_source_config(source, uid)
    except UnsafeSourceError as exc:
        return PolicyPlan(source, "refused", str(exc))
    problem = policy_problem(found.data, source)
    if problem is not None:
        return PolicyPlan(source, "refused", problem)
    return PolicyPlan(source, "copy", uid=uid)


def read_source_config(source: Path, uid: int) -> ReadFile:
    """``source`` read through its checked parent folder (``safe_source``)."""
    with opened_dir(source.parent, uid) as held:
        found = read_file(held, source.name)
    if found is None:
        raise UnsafeSourceError(source, f"{source} does not exist")
    return found


# ------------------------------------------------------------------ execute
