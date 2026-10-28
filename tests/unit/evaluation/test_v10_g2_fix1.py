"""V1.0 G2 fix round 1: the lab profile hook validates the root-owned copy it
installs (no check-then-use race on the user's file), allows only the keys
the profiles generate, refuses comment continuations and option-like file
names; ``--retry-errors`` redoes ``submit_error`` runs without a duplicate
job; ``check_sample.py`` names the matplotlib version on a mismatch."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from mf_eval_kit import EVAL_DIR, load

load()

import check_sample  # noqa: E402
from evalkit.arms import PROFILES, profile_env, profile_env_file  # noqa: E402
from evalkit.raw import append_line, read_raw  # noqa: E402
from evalkit.runner import run_campaign  # noqa: E402
from evalkit.schedule import schedule  # noqa: E402
from test_v10_g2_runner import Clock, FakeFabric, _options, _spec  # noqa: E402

pytestmark = pytest.mark.unit

HOOK = EVAL_DIR.parents[1] / "lab" / "eval_profile.sh"
posix = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX shell hook"
)
GOOD = "MESHFORGE_CONTROLLER__PLANNER__ML='auto'\n"
EVIL = "LD_PRELOAD='/tmp/x.so'\n"


def _install_copy(tmp_path: Path) -> tuple[Path, Path]:
    """The hook with its drop-in directory moved under ``tmp_path`` (so the
    install path runs without root); ``(script, drop-in dir)``."""
    dropin = tmp_path / "etc" / "meshforge-controller.service.d"
    text = HOOK.read_text("utf-8")
    original = "DIR=/etc/systemd/system/${UNIT}.d\n"
    assert original in text
    script = tmp_path / "eval_profile.sh"
    script.write_text(text.replace(original, f"DIR='{dropin}'\n"), "utf-8")
    return script, dropin


def _fake_bin(tmp_path: Path) -> Path:
    """systemctl that logs its calls; chmod that, when ``SWAP_SRC`` is set,
    rewrites that file with an LD_PRELOAD line just before the hook makes its
    copy readable - an attacker swapping the source after the check."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls.log"\nexit 0\n', "utf-8"
    )
    real_chmod = shutil.which("chmod")
    assert real_chmod is not None
    chmod = bin_dir / "chmod"
    chmod.write_text(
        "#!/bin/sh\n"
        'if [ -n "${SWAP_SRC:-}" ]; then\n'
        f'  printf \'%s\' "{EVIL}" > "$SWAP_SRC"\n'
        "fi\n"
        f'exec {real_chmod} "$@"\n',
        "utf-8",
    )
    for path in (systemctl, chmod):
        path.chmod(0o755)
    return bin_dir


def _run(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, **(env or {})},
    )


# ------------------------------------------------------------- lab hook


def test_the_hook_allowlist_is_exactly_the_keys_the_profiles_generate() -> None:
    match = re.search(r"^KEYS='([A-Z_|]+)'$", HOOK.read_text("utf-8"), re.M)
    assert match is not None
    generated = {
        key.removeprefix("MESHFORGE_CONTROLLER__PLANNER__")
        for profile in PROFILES.values()
        for key in profile_env(profile)
    }
    assert set(match.group(1).split("|")) == generated


@posix
@pytest.mark.parametrize(
    "content",
    [
        "MESHFORGE_CONTROLLER__PLANNER__POLICY='random'\n",
        "MESHFORGE_CONTROLLER__PLANNER__MLX='auto'\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML_MODEL_DIR='/tmp/evil'\n",
        "MESHFORGE_CONTROLLER__PLANNER__CALIBRATIONS='off'\n",
        "# a comment \\\n" + GOOD,
        "#\\\n" + GOOD,
        GOOD + "# trailing continuation \\\n",
    ],
)
def test_the_hook_refuses_other_keys_and_comment_continuations(
    tmp_path: Path, content: str
) -> None:
    f = tmp_path / "p.env"
    f.write_text(content, "utf-8")
    assert _run(HOOK, "--check", str(f)).returncode == 2


@posix
def test_the_hook_reports_the_offending_line_number_not_its_content(
    tmp_path: Path,
) -> None:
    f = tmp_path / "p.env"
    f.write_text("# profile\n" + GOOD + EVIL, "utf-8")
    done = _run(HOOK, "--check", str(f))
    assert done.returncode == 2
    assert "line 3" in done.stderr
    assert "LD_PRELOAD" not in done.stderr


@posix
def test_the_hook_refuses_option_like_names_and_extra_clear_args(
    tmp_path: Path,
) -> None:
    fake = _fake_bin(tmp_path)
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    for args in (
        ("--check", "-n"),
        ("--check", "--help"),
        ("calibrated", "-"),
        ("calibrated", "-rf"),
        ("--clear", "x"),
        ("--clear", "--clear"),
    ):
        done = _run(HOOK, *args, env=env)
        assert done.returncode == 2, (args, done.stderr)
    assert not (fake / "calls.log").exists()


@posix
def test_the_hook_installs_a_valid_profile_and_restarts(tmp_path: Path) -> None:
    script, dropin = _install_copy(tmp_path)
    fake = _fake_bin(tmp_path)
    src = tmp_path / "src dir" / "ml.env"
    src.parent.mkdir()
    src.write_text(profile_env_file(PROFILES["ml"]), "utf-8")
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    done = _run(script, "ml", str(src), env=env)
    assert done.returncode == 0, done.stderr
    installed = dropin / "eval-profile.env"
    assert installed.read_bytes() == src.read_bytes()
    assert installed.stat().st_mode & 0o777 == 0o644
    assert (dropin / "eval-profile.conf").read_text("utf-8") == (
        f"[Service]\nEnvironmentFile={installed}\n"
    )
    assert [p.name for p in dropin.iterdir() if p.name.startswith(".")] == []
    calls = (fake / "calls.log").read_text("utf-8").splitlines()
    assert calls[0] == "daemon-reload"


@posix
def test_swapping_the_source_after_the_check_does_not_change_what_is_installed(
    tmp_path: Path,
) -> None:
    script, dropin = _install_copy(tmp_path)
    fake = _fake_bin(tmp_path)
    src = tmp_path / "ml.env"
    src.write_text(GOOD, "utf-8")
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "SWAP_SRC": str(src)}
    done = _run(script, "ml", str(src), env=env)
    assert done.returncode == 0, done.stderr
    # the attacker did swap the source ...
    assert src.read_text("utf-8") == EVIL
    # ... but the validated copy is what was installed
    assert (dropin / "eval-profile.env").read_text("utf-8") == GOOD


@posix
def test_a_refused_copy_installs_nothing_and_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    """The source passes the early check, then turns evil before the copy
    (here: it is a symlink retargeted between the two) - the copy's check
    refuses it, nothing is installed and the temp file is removed."""
    script, dropin = _install_copy(tmp_path)
    fake = _fake_bin(tmp_path)
    # a fake `install` that retargets the source after the early check
    good = tmp_path / "good.env"
    good.write_text(GOOD, "utf-8")
    evil = tmp_path / "evil.env"
    evil.write_text(EVIL, "utf-8")
    link = tmp_path / "link.env"
    link.symlink_to(good)
    real_install = shutil.which("install")
    assert real_install is not None
    (fake / "install").write_text(
        f"#!/bin/sh\nln -sfn '{evil}' '{link}'\nexec {real_install} \"$@\"\n",
        "utf-8",
    )
    (fake / "install").chmod(0o755)
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    done = _run(script, "ml", str(link), env=env)
    assert done.returncode == 2, done.stderr
    assert "line 1" in done.stderr
    assert not (dropin / "eval-profile.env").exists()
    assert not (dropin / "eval-profile.conf").exists()
    assert list(dropin.iterdir()) == []
    assert not (fake / "calls.log").exists()


# ---------------------------------------------------------- --retry-errors


class FlakyFabric(FakeFabric):
    """Submission of the second copy of every B1 run fails while ``down``
    (the first copy is accepted: a retry must find it, not duplicate it)."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def submit(
        self,
        folder: Path,
        manifest: Mapping[str, Any],
        placement: Mapping[str, Any],
        *,
        submitter_node_id: str,
        idempotency_key: str,
    ) -> str:
        name = str(manifest["name"])
        if self.down and "-B1-" in name and "-c1" in name:
            raise RuntimeError("controller said 503")
        return super().submit(
            folder,
            manifest,
            placement,
            submitter_node_id=submitter_node_id,
            idempotency_key=idempotency_key,
        )


def test_retry_errors_redoes_submit_errors_without_duplicate_jobs(
    tmp_path: Path,
) -> None:
    spec = _spec(jobs_per_run=2)
    fabric = FlakyFabric()
    clock = Clock()
    first = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert first["statuses"]["submit_error"] == 4
    accepted = len(fabric.submits)
    # resuming without the flag changes nothing
    plain = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert plain["executed"] == 0 and plain["remaining"] == 0
    fabric.down = False
    second = run_campaign(fabric, spec, _options(tmp_path, clock, retry_errors=True))
    assert second == {
        "executed": 4,
        "statuses": {"ok": 4},
        "remaining": 0,
        "planned": len(schedule(spec)),
    }
    # only the 4 failed second copies were new jobs; the first copies were found
    assert len(fabric.submits) == accepted + 4
    raw = read_raw(tmp_path / "raw")
    assert len(raw.runs) == len(schedule(spec))
    assert {r["status"] for r in raw.runs} == {"ok"}
    assert raw.duplicates == ()
    assert len(raw.retried) == 4
    assert all(len(r["jobs"]) == 2 for r in raw.runs)
    assert raw.sessions[-1]["retry_errors"] == 4
    assert raw.sessions[0]["retry_errors"] is None
    # nothing left to retry
    third = run_campaign(fabric, spec, _options(tmp_path, clock, retry_errors=True))
    assert third["executed"] == 0


def test_a_retry_that_fails_again_stays_retryable(tmp_path: Path) -> None:
    spec = _spec()
    fabric = FakeFabric(fail_submit={"-B1-"})
    clock = Clock()
    run_campaign(fabric, spec, _options(tmp_path, clock))
    again = run_campaign(fabric, spec, _options(tmp_path, clock, retry_errors=True))
    assert again["statuses"] == {"submit_error": 4}
    raw = read_raw(tmp_path / "raw")
    assert sum(r["status"] == "submit_error" for r in raw.runs) == 4
    fabric.fail_submit = set()
    last = run_campaign(fabric, spec, _options(tmp_path, clock, retry_errors=True))
    assert last["statuses"] == {"ok": 4}
    assert {r["status"] for r in read_raw(tmp_path / "raw").runs} == {"ok"}


def test_only_a_retried_line_replaces_and_only_a_submit_error(tmp_path: Path) -> None:
    spec = _spec()
    run_campaign(FakeFabric(), spec, _options(tmp_path, Clock()))
    runs_file = tmp_path / "raw" / "runs.jsonl"
    first = read_raw(tmp_path / "raw").runs[0]
    # a "retried" line for a run that was ok is a duplicate, never a replacement
    append_line(runs_file, {**first, "status": "failed", "retried": True})
    raw = read_raw(tmp_path / "raw")
    assert raw.runs[0]["status"] == "ok"
    assert raw.duplicates == (first["run_id"],)
    assert raw.retried == ()


def test_run_campaign_cli_has_retry_errors() -> None:
    import run_campaign as cli

    args = cli._parser().parse_args(
        ["--spec", "x.yaml", "--raw", "r", "--retry-errors"]
    )
    assert args.retry_errors is True
    assert cli._parser().parse_args(["--spec", "x", "--raw", "r"]).retry_errors is False


# ------------------------------------------------------------ check_sample


def test_a_figure_mismatch_names_the_matplotlib_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = tmp_path / "expected.sha256"
    expected.write_text(f"{'0' * 64}  figures/a.svg\n", "utf-8")
    monkeypatch.setattr(check_sample, "EXPECTED", expected)
    monkeypatch.setattr(
        check_sample, "compute_hashes", lambda out, *, figures: {"figures/a.svg": "1"}
    )
    monkeypatch.setattr(check_sample, "_matplotlib_version", lambda: "9.9.9")
    assert check_sample.main([]) == 1
    err = capsys.readouterr().err
    assert "matplotlib 9.9.9" in err
    # the analysis-only check does not mention it
    monkeypatch.setattr(
        check_sample, "compute_hashes", lambda out, *, figures: {"analysis/a": "1"}
    )
    expected.write_text(f"{'0' * 64}  analysis/a\n", "utf-8")
    assert check_sample.main(["--no-figures"]) == 1
    assert "matplotlib" not in capsys.readouterr().err
