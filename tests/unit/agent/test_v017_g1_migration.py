"""V0.17 G1 (D387, Q13): per-user identity + config -> the machine-wide service.

The matrix of the brief: none / one valid / two / partial / corrupt / fingerprint
mismatch / target already present / source unreadable / crash mid-copy (re-run
converges), config with unknown keys refused - and for every refusal: the source
is byte-for-byte untouched and no identity can be minted in the target.
Everything runs in ``tmp_path`` (``Paths`` is injectable); no root needed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.agent.identity import (
    CA_CERT_FILE_NAME,
    JSON_FILE_NAME,
    KEY_FILE_NAME,
    MIGRATION_PENDING_FILE_NAME,
    NODE_CERT_FILE_NAME,
    NodeIdentity,
    load_or_create,
    read_identity,
)
from meshforge.agent.main import app
from meshforge.agent.migration import (
    MigrationOutcome,
    Owner,
    Paths,
    execute,
    migrate,
    plan,
    running_user_agents,
    stop_user_unit,
)
from meshforge.common.errors import IdentityError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux package (G4: Windows)"),
]

_NO_OWNER = Owner(None, None)
_TODAY = "20261024"


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        target_identity=tmp_path / "var/lib/meshforge/identity",
        target_config=tmp_path / "etc/meshforge/config.toml",
        home_root=tmp_path / "home",
    )


def _user_identity(
    tmp_path: Path, user: str, *, enrolled: bool = False
) -> NodeIdentity:
    directory = tmp_path / "home" / user / ".local/share/meshforge/identity"
    identity = load_or_create(directory)
    if enrolled:  # a real enrolment: the migration validates it (spec §5a item 6)
        from mf_fabric import enroll_identity

        enroll_identity(identity)
    return identity


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, int]]:
    out: dict[str, tuple[bytes | None, int]] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        data = path.read_bytes() if path.is_file() else None
        out[path.relative_to(root).as_posix()] = (data, st.st_mode)
    return out


def _run(chosen_paths: Paths, **kwargs: object) -> MigrationOutcome:
    chosen = plan(chosen_paths, **kwargs)  # type: ignore[arg-type]
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    return chosen.outcome


def _assert_cannot_mint(paths: Paths, *, half_copy: bool = False) -> None:
    with pytest.raises(IdentityError, match="migration"):
        load_or_create(paths.target_identity)
    if not half_copy:
        assert not (paths.target_identity / KEY_FILE_NAME).exists()


# ------------------------------------------------------------------ matrix


def test_none_found_lets_the_service_mint(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _run(paths) is MigrationOutcome.NOTHING_TO_MIGRATE
    assert not paths.target_identity.exists()  # nothing written
    assert load_or_create(paths.target_identity).session_epoch == 0


def test_one_valid_identity_moves_with_its_node_id(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    original = _user_identity(tmp_path, "asha", enrolled=True)
    source = original.path
    certs = {
        n: (source / n).read_bytes() for n in (NODE_CERT_FILE_NAME, CA_CERT_FILE_NAME)
    }
    assert _run(paths) is MigrationOutcome.MIGRATED

    moved = load_or_create(paths.target_identity)
    assert (moved.node_id, moved.fingerprint) == (
        original.node_id,
        original.fingerprint,
    )
    for name, data in certs.items():
        assert (paths.target_identity / name).read_bytes() == data
    assert not (paths.target_identity / MIGRATION_PENDING_FILE_NAME).exists()
    assert oct(paths.target_identity.stat().st_mode & 0o777) == "0o700"
    for name in (KEY_FILE_NAME, JSON_FILE_NAME, NODE_CERT_FILE_NAME):
        assert (paths.target_identity / name).stat().st_mode & 0o777 == 0o600
    assert not source.exists()
    renamed = source.with_name(f"identity.migrated-{_TODAY}")
    assert read_identity(renamed).node_id == original.node_id  # old copy kept


def test_sudo_user_is_preferred_over_scanning(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    asha = _user_identity(tmp_path, "asha")
    _user_identity(tmp_path, "ravi")
    homes = {"asha": tmp_path / "home/asha"}
    chosen = plan(paths, sudo_user="asha", home_of=homes.get)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.identity is not None
    assert chosen.identity.node_id == asha.node_id


def test_two_identities_block_and_touch_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    ravi = _user_identity(tmp_path, "ravi")
    before = _snapshot(tmp_path / "home")
    chosen = plan(paths)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert int(report.exit_code) == 4
    text = "\n".join(report.lines())
    assert "asha" in text and "ravi" in text and "--from" in text
    assert _snapshot(tmp_path / "home") == before
    _assert_cannot_mint(paths)

    # The admin chooses: the block clears and the chosen node moves.
    assert _run(paths, explicit=ravi.path) is MigrationOutcome.MIGRATED
    assert load_or_create(paths.target_identity).node_id == ravi.node_id


def test_a_resolved_block_without_candidates_clears_the_marker(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    a = _user_identity(tmp_path, "asha")
    b = _user_identity(tmp_path, "ravi")
    assert _run(paths) is MigrationOutcome.BLOCKED
    shutil.move(a.path, a.path.with_name("identity.unused"))
    shutil.move(b.path, b.path.with_name("identity.unused"))
    assert _run(paths) is MigrationOutcome.NOTHING_TO_MIGRATE
    load_or_create(paths.target_identity)  # minting allowed again


def _make_partial(directory: Path) -> None:
    (directory / JSON_FILE_NAME).unlink()


def _make_corrupt(directory: Path) -> None:
    (directory / JSON_FILE_NAME).write_bytes(b"{not json")


def _make_mismatch(directory: Path) -> None:
    other = load_or_create(directory.parent / "other")
    (directory / JSON_FILE_NAME).write_bytes((other.path / JSON_FILE_NAME).read_bytes())
    shutil.rmtree(other.path)


@pytest.mark.parametrize(
    ("damage", "words"),
    [
        (_make_partial, "partial identity"),
        (_make_corrupt, "not JSON"),
        (_make_mismatch, "does not match"),
    ],
)
def test_a_damaged_candidate_blocks_and_is_untouched(
    tmp_path: Path, damage: object, words: str
) -> None:
    paths = _paths(tmp_path)
    identity = _user_identity(tmp_path, "asha")
    damage(identity.path)  # type: ignore[operator]
    before = _snapshot(tmp_path / "home")
    chosen = plan(paths)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert words in "\n".join(chosen.problems)
    assert str(identity.path) in "\n".join(chosen.problems)
    assert _snapshot(tmp_path / "home") == before
    _assert_cannot_mint(paths)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything"
)
def test_an_unreadable_source_blocks(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _user_identity(tmp_path, "asha")
    key = identity.path / KEY_FILE_NAME
    key.chmod(0o000)
    try:
        before = _snapshot_names(tmp_path / "home")
        chosen = plan(paths)
        execute(chosen, owner=_NO_OWNER, today=_TODAY)
        assert chosen.outcome is MigrationOutcome.BLOCKED
        assert "could not read" in "\n".join(chosen.problems)
        assert _snapshot_names(tmp_path / "home") == before
        _assert_cannot_mint(paths)
    finally:
        key.chmod(0o600)


def _snapshot_names(root: Path) -> list[tuple[str, int]]:
    return [
        (p.relative_to(root).as_posix(), p.lstat().st_mode)
        for p in sorted(root.rglob("*"))
    ]


def test_target_already_present_changes_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    service = load_or_create(paths.target_identity)
    _user_identity(tmp_path, "asha")
    before = (_snapshot(tmp_path / "home"), _snapshot(paths.target_identity))
    chosen = plan(paths)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.TARGET_PRESENT
    assert int(report.exit_code) == 0
    assert str(service.node_id) in "\n".join(report.lines())
    assert "left as is" in "\n".join(chosen.notes)
    assert (_snapshot(tmp_path / "home"), _snapshot(paths.target_identity)) == before


class _CrashError(Exception):
    pass


@pytest.mark.parametrize(
    "crash_after", [KEY_FILE_NAME, JSON_FILE_NAME, CA_CERT_FILE_NAME]
)
def test_a_crash_mid_copy_converges_on_rerun(tmp_path: Path, crash_after: str) -> None:
    paths = _paths(tmp_path)
    original = _user_identity(tmp_path, "asha", enrolled=True)

    def crash(name: str) -> None:
        if name == crash_after:
            raise _CrashError

    with pytest.raises(_CrashError):
        execute(plan(paths), owner=_NO_OWNER, today=_TODAY, after_copy=crash)
    assert (paths.target_identity / MIGRATION_PENDING_FILE_NAME).exists()
    assert original.path.exists()  # the source is renamed only after the commit
    _assert_cannot_mint(paths, half_copy=True)

    assert _run(paths) is MigrationOutcome.MIGRATED
    moved = load_or_create(paths.target_identity)
    assert (moved.node_id, moved.fingerprint) == (
        original.node_id,
        original.fingerprint,
    )


def test_a_crash_between_commit_and_rename_converges(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    original = _user_identity(tmp_path, "asha")
    assert _run(paths) is MigrationOutcome.MIGRATED
    renamed = original.path.with_name(f"identity.migrated-{_TODAY}")
    renamed.rename(original.path)  # as if the rename never happened
    chosen = plan(paths)
    assert chosen.outcome is MigrationOutcome.TARGET_PRESENT
    assert chosen.leftover_source == original.path
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert not original.path.exists()
    assert original.path.with_name(f"identity.migrated-{_TODAY}").is_dir()


@pytest.mark.parametrize("users", [["asha"], ["asha", "ravi"]])
def test_dry_run_writes_nothing(tmp_path: Path, users: list[str]) -> None:
    paths = _paths(tmp_path)
    for user in users:
        _user_identity(tmp_path, user)
    config = tmp_path / "home/asha/.config/meshforge/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[log]\nlevel = "DEBUG"\n', encoding="utf-8")
    before = _snapshot(tmp_path)
    report = migrate(
        explicit=None, config_from=None, dry_run=True, paths=paths, service_user=None
    )
    assert report.dry_run
    assert _snapshot(tmp_path) == before
    expected = (
        MigrationOutcome.MIGRATED if len(users) == 1 else MigrationOutcome.BLOCKED
    )
    assert report.plan.outcome is expected


def test_no_key_material_in_the_report(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    chosen = plan(paths)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    text = json.dumps(report.as_dict()) + "\n".join(report.lines())
    assert "PRIVATE KEY" not in text
    key = (paths.target_identity / KEY_FILE_NAME).read_text()
    body = "".join(key.splitlines()[1:-1])
    assert body[:40] not in text


# ------------------------------------------------------------------- config


def _config(tmp_path: Path, text: str, user: str = "asha") -> Path:
    path = tmp_path / "home" / user / ".config/meshforge/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_valid_config_moves_with_the_identity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    text = '# mine\n[log]\nlevel = "DEBUG"\n[agent]\ncontroller_address = "10.0.0.2:47100"\n'
    source = _config(tmp_path, text)
    assert _run(paths) is MigrationOutcome.MIGRATED
    assert paths.target_config.read_text(encoding="utf-8") == text  # comments kept
    assert paths.target_config.stat().st_mode & 0o777 == 0o640
    assert not source.exists()
    assert source.with_name(f"config.toml.migrated-{_TODAY}").is_file()


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ('[controller]\ngrpc_bind = "0.0.0.0:47100"\n', "does not know: controller"),
        ("typo_key = 1\n", "does not know: typo_key"),
        ("[agent]\nno_such_option = 1\n", "not a valid agent configuration"),
        ('data_dir = "/srv/x"\n', "sets data_dir"),
        ('[agent.envs]\ndir = "/home/asha/envs"\n', "home directory"),
        ("[log\n", "cannot read"),
    ],
)
def test_a_bad_config_blocks_the_migration_untouched(
    tmp_path: Path, text: str, words: str
) -> None:
    """Unknown keys are refused, not dropped; nothing moves (identity included)."""
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    _config(tmp_path, text)
    before = _snapshot(tmp_path / "home")
    chosen = plan(paths)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert words in "\n".join(chosen.problems)
    assert _snapshot(tmp_path / "home") == before
    assert not paths.target_config.exists()
    _assert_cannot_mint(paths)


def test_an_existing_service_config_is_kept(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    source = _config(tmp_path, "typo_key = 1\n")  # never even validated
    paths.target_config.parent.mkdir(parents=True)
    paths.target_config.write_text("# admin's\n", encoding="utf-8")
    assert _run(paths) is MigrationOutcome.MIGRATED
    assert paths.target_config.read_text(encoding="utf-8") == "# admin's\n"
    assert source.is_file()


def test_a_bad_config_without_an_identity_only_goes_unmigrated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    homes = {"asha": tmp_path / "home/asha"}
    _config(tmp_path, "typo_key = 1\n")
    chosen = plan(paths, sudo_user="asha", home_of=homes.get)
    assert chosen.outcome is MigrationOutcome.NOTHING_TO_MIGRATE
    assert "config not migrated" in "\n".join(chosen.notes)


# --------------------------------------------------------------- user units


def test_the_old_user_unit_is_stopped_and_its_file_kept(tmp_path: Path) -> None:
    home = tmp_path / "home/asha"
    unit = home / ".config/systemd/user/meshforge-agent.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    seen: list[list[str]] = []

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = stop_user_unit("asha", home, runner)
    assert result is not None and result[0] is True
    assert seen[0][1:] == [
        "--user",
        "-M",
        "asha@",
        "disable",
        "--now",
        "meshforge-agent.service",
    ]
    assert unit.is_file()
    assert "left in place" in result[1]


def test_a_failed_unit_stop_says_what_to_run(tmp_path: Path) -> None:
    home = tmp_path / "home/asha"
    wants = home / ".config/systemd/user/default.target.wants"
    wants.mkdir(parents=True)
    (wants / "meshforge-agent.service").symlink_to("/nonexistent")

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")

    result = stop_user_unit("asha", home, runner)
    assert result is not None and result[0] is False
    assert "systemctl --user disable --now meshforge-agent.service" in result[1]


def test_no_user_unit_no_action(tmp_path: Path) -> None:
    assert stop_user_unit("asha", tmp_path / "home/asha") is None


def test_running_hand_started_agents_are_found(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    uid = os.getuid()
    for pid, argv in {
        "101": b"/usr/bin/python3\0/home/a/.venv/bin/meshforge-agent\0run\0",
        "102": b"/usr/bin/python3\0/home/a/.venv/bin/meshforge-agent\0identity\0show\0",
        "103": b"bash\0",
        "self": b"x\0",
    }.items():
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(argv)
    assert running_user_agents(uid, proc) == [101]
    assert running_user_agents(None, proc) == []


# --------------------------------------------------------------------- CLI


def test_cli_dry_run_and_blocked_exit_codes(tmp_path: Path) -> None:
    _user_identity(tmp_path, "asha")
    runner = CliRunner()
    base = ["migrate-identity", "--root-dir", str(tmp_path), "--service-user", ""]
    dry = runner.invoke(app, [*base, "--dry-run", "--json"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["outcome"] == "migrated"
    assert not (tmp_path / "var").exists()

    _user_identity(tmp_path, "ravi")
    blocked = runner.invoke(app, base)
    assert blocked.exit_code == 4
    assert "STOPPED" in blocked.stdout and "--from" in blocked.stdout

    chosen = tmp_path / "home/asha/.local/share/meshforge/identity"
    done = runner.invoke(app, [*base, "--from", str(chosen)])
    assert done.exit_code == 0, done.output
    assert "migrated node" in done.stdout


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="the check is for non-root"
)
def test_cli_refuses_a_real_run_without_root(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["migrate-identity", "--root-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "sudo" in result.stderr
    assert not (tmp_path / "var").exists()


# ------------------------------------------------------------ identity hooks


def test_a_pending_marker_blocks_even_a_complete_identity(tmp_path: Path) -> None:
    """Copied but not yet verified (the marker is the commit point): refuse."""
    identity = load_or_create(tmp_path / "id")
    (identity.path / MIGRATION_PENDING_FILE_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(IdentityError, match="migrate-identity"):
        load_or_create(identity.path)


def test_read_identity_is_read_only(tmp_path: Path) -> None:
    identity = load_or_create(tmp_path / "id")
    (identity.path / "identity.lock").unlink()
    before = _snapshot(identity.path)
    assert read_identity(identity.path).node_id == identity.node_id
    assert _snapshot(identity.path) == before  # no lock file created
    with pytest.raises(IdentityError, match="no identity"):
        read_identity(tmp_path / "empty")


# ------------------------------------------------- enrolment + --from (fix 1)


def _garbage_cert(directory: Path) -> None:
    (directory / NODE_CERT_FILE_NAME).write_bytes(b"junk\n")


def _missing_ca(directory: Path) -> None:
    (directory / CA_CERT_FILE_NAME).unlink()


def _cert_of_another_node(directory: Path) -> None:
    from mf_fabric import enroll_identity

    other = load_or_create(directory.parent / "other")
    enroll_identity(other)
    shutil.copy(other.path / NODE_CERT_FILE_NAME, directory / NODE_CERT_FILE_NAME)
    shutil.rmtree(other.path)


@pytest.mark.parametrize("damage", [_garbage_cert, _missing_ca, _cert_of_another_node])
def test_an_unusable_enrolment_blocks_and_is_untouched(
    tmp_path: Path, damage: object
) -> None:
    """Spec §5a item 6: a corrupt candidate (here its enrolment) is refused."""
    paths = _paths(tmp_path)
    identity = _user_identity(tmp_path, "asha", enrolled=True)
    damage(identity.path)  # type: ignore[operator]
    before = _snapshot(tmp_path / "home")
    chosen = plan(paths)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    text = "\n".join(chosen.problems)
    assert "enrolment is unusable" in text and str(identity.path) in text
    assert "/tmp/meshforge-migrate-" not in text + "\n".join(chosen.notes)
    assert _snapshot(tmp_path / "home") == before
    _assert_cannot_mint(paths)


def test_a_never_enrolled_identity_still_moves(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    original = _user_identity(tmp_path, "asha")
    assert not (original.path / NODE_CERT_FILE_NAME).exists()
    assert _run(paths) is MigrationOutcome.MIGRATED


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda d: d / "nope", "no such directory"),
        (lambda d: d / "file", "not a directory"),
        (lambda d: d / "empty", "not a MeshForge identity directory"),
    ],
)
def test_a_bad_from_is_named_precisely(
    tmp_path: Path, make: object, words: str
) -> None:
    paths = _paths(tmp_path)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    chosen = plan(paths, explicit=make(tmp_path))  # type: ignore[operator]
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert words in "\n".join(chosen.problems)


def test_a_non_identity_from_next_to_a_service_identity_says_so(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    load_or_create(paths.target_identity)
    (tmp_path / "empty").mkdir()
    chosen = plan(paths, explicit=tmp_path / "empty")
    assert chosen.outcome is MigrationOutcome.TARGET_PRESENT
    notes = "\n".join(chosen.notes)
    assert "not a MeshForge identity directory" in notes and "unreadable" not in notes


@pytest.mark.parametrize("flag", ["--from", "--config-from"])
def test_cli_refuses_an_empty_path(tmp_path: Path, flag: str) -> None:
    base = ["migrate-identity", "--root-dir", str(tmp_path), "--service-user", ""]
    result = CliRunner().invoke(app, [*base, "--dry-run", flag, ""])
    assert result.exit_code == 2, result.output
    assert "must not be empty" in result.output
