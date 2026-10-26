"""V1.0 G2 independent acceptance (tester): try to break the evaluation harness.

Derived from the G2 success criteria (brief section 5) and D415-D419, not from
the implementation: resume after a crash at any byte, corrupt raw folders,
byte-identical analysis across environments, the lab profile hook's input
validation and sequencing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mf_eval_kit import EVAL_DIR, SAMPLE_DIR, load, write_raw

load()

from evalkit.raw import (  # noqa: E402
    RawError,
    append_line,
    cut_partial_tail,
    read_raw,
)
from evalkit.runner import run_campaign  # noqa: E402
from evalkit.schedule import schedule  # noqa: E402
from test_v10_g2_runner import Clock, FakeFabric, _options, _spec  # noqa: E402

pytestmark = pytest.mark.unit

HOOK = EVAL_DIR.parents[1] / "lab" / "eval_profile.sh"


# --------------------------------------------------------------- raw folder


def _folder(tmp_path: Path) -> Path:
    return write_raw(
        tmp_path / "raw",
        name="adv",
        arms=("B0", "B1", "B5"),
        families={"cpu.primes": ("s",)},
        repetitions=3,
        makespan=lambda f, s, a, b: 1000.0 + 10 * b + {"B0": 0, "B1": 5, "B5": 9}[a],
        smoke=True,
    )


def test_cut_partial_tail_edge_cases(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    assert cut_partial_tail(p) == 0  # missing
    p.write_bytes(b"")
    assert cut_partial_tail(p) == 0  # empty
    p.write_bytes(b'{"a":1}\n')
    assert cut_partial_tail(p) == 0  # complete
    assert p.read_bytes() == b'{"a":1}\n'
    p.write_bytes(b'{"a":1}\n{"a":')
    assert cut_partial_tail(p) == 5
    assert p.read_bytes() == b'{"a":1}\n'
    assert cut_partial_tail(p) == 0  # idempotent
    # no newline anywhere: everything is the unfinished line
    p.write_bytes(b'{"a":')
    assert cut_partial_tail(p) == 5
    assert p.read_bytes() == b""
    # cut bytes accumulate across crashes
    p.write_bytes(b'{"b":')
    cut_partial_tail(p)
    assert p.with_name("x.jsonl.cut").read_bytes() == b'{"a":\n{"a":\n{"b":\n'
    # NUL / non-UTF8 garbage in the torn tail must not break the cut
    p.write_bytes(b'{"a":1}\n\x00\x00\xff\xfe')
    assert cut_partial_tail(p) == 4
    assert p.read_bytes() == b'{"a":1}\n'


def test_a_complete_json_object_without_newline_is_still_an_unfinished_line(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    runs = folder / "runs.jsonl"
    full = read_raw(folder)
    text = runs.read_text("utf-8")
    runs.write_text(text.rstrip("\n"), "utf-8")  # last record lost its newline
    data = read_raw(folder)
    assert len(data.runs) == len(full.runs) - 1
    assert data.partial_lines == 1


@pytest.mark.parametrize(
    "tail",
    ['{"run_id": "x"', "   ", "\t", "}{", "null", "[1,2", "\x00" * 10],
)
def test_torn_tails_are_ignored_not_fatal(tmp_path: Path, tail: str) -> None:
    folder = _folder(tmp_path)
    before = len(read_raw(folder).runs)
    with (folder / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(tail)
    data = read_raw(folder)
    assert len(data.runs) == before


@pytest.mark.parametrize("garbage", ["not json", "[1, 2]", '"str"', "123", "{", "null"])
def test_mid_file_corruption_is_refused_loudly(tmp_path: Path, garbage: str) -> None:
    folder = _folder(tmp_path)
    runs = folder / "runs.jsonl"
    lines = runs.read_text("utf-8").splitlines(keepends=True)
    lines.insert(2, garbage + "\n")
    runs.write_text("".join(lines), "utf-8")
    with pytest.raises(RawError):
        read_raw(folder)


@pytest.mark.parametrize(
    "content",
    [b"", b"{", b"\x00\x01\xff", b"[]", b'"x"', b'{"format": "other/9"}', b"null"],
)
def test_corrupt_campaign_file_is_an_error_not_a_traceback(
    tmp_path: Path, content: bytes
) -> None:
    folder = _folder(tmp_path)
    (folder / "campaign.json").write_bytes(content)
    done = subprocess.run(
        [
            sys.executable,
            str(EVAL_DIR / "analyze.py"),
            "--raw",
            str(folder),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr


def test_invalid_utf8_in_runs_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    with (folder / "runs.jsonl").open("ab") as fh:
        fh.write(b"\xff\xfe garbage\n")
    done = subprocess.run(
        [
            sys.executable,
            str(EVAL_DIR / "analyze.py"),
            "--raw",
            str(folder),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "Traceback" not in done.stderr


def test_a_missing_raw_folder_exits_2(tmp_path: Path) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(EVAL_DIR / "analyze.py"),
            "--raw",
            str(tmp_path / "nope"),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 2
    assert "Traceback" not in done.stderr


# ------------------------------------------------------------------- resume


def _lines_of(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines(keepends=True)


@pytest.mark.parametrize("stop_after", [0, 1, 5, 11])
@pytest.mark.parametrize("tear", [0, 1, 17, "all-but-newline"])
def test_resume_after_a_crash_at_any_point_gives_each_run_exactly_once(
    tmp_path: Path, stop_after: int, tear: int | str
) -> None:
    """Kill after N runs and tear the last line at several offsets: the
    resumed campaign has every planned run once, one job per run."""
    spec = _spec()
    planned = schedule(spec)
    fabric = FakeFabric()
    clock = Clock()
    run_campaign(fabric, spec, _options(tmp_path, clock, max_runs=stop_after))
    runs_file = tmp_path / "raw" / "runs.jsonl"
    if stop_after and tear != 0:
        lines = _lines_of(runs_file)
        last = lines[-1]
        keep = len(last) - 1 if tear == "all-but-newline" else min(int(tear), 30)
        runs_file.write_bytes(b"".join(lines[:-1]) + last[:keep])
    sessions = tmp_path / "raw" / "sessions.jsonl"
    # also tear the sessions file's last line on odd cases
    if stop_after % 2 == 1:
        sessions.write_bytes(sessions.read_bytes().rstrip(b"\n")[:-3])
    submitted_before = len(fabric.submits)
    summary = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert summary["remaining"] == 0
    raw = read_raw(tmp_path / "raw")
    assert [r["run_id"] for r in raw.runs] == [r.run_id for r in planned]
    assert raw.duplicates == ()
    assert raw.partial_lines == 0
    # one job per planned run in total, whatever was redone
    assert len(fabric.jobs) == len(planned)
    assert len(fabric.submits) <= submitted_before + len(planned)
    # a third start is a no-op (nothing to do, nothing new submitted)
    n_jobs = len(fabric.jobs)
    again = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert again["executed"] == 0
    assert len(fabric.jobs) == n_jobs
    assert len(read_raw(tmp_path / "raw").runs) == len(planned)


def test_resume_when_runs_file_was_lost_does_not_duplicate_jobs(
    tmp_path: Path,
) -> None:
    spec = _spec()
    fabric = FakeFabric()
    clock = Clock()
    run_campaign(fabric, spec, _options(tmp_path, clock))
    n_jobs = len(fabric.jobs)
    (tmp_path / "raw" / "runs.jsonl").unlink()
    summary = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert summary["executed"] == len(schedule(spec))
    assert len(fabric.jobs) == n_jobs  # idempotency keys found them again


def test_resume_refuses_a_hand_edited_seed_or_arms(tmp_path: Path) -> None:
    fabric = FakeFabric()
    clock = Clock()
    run_campaign(fabric, _spec(), _options(tmp_path, clock, max_runs=2))
    for over in ({"seed": 6}, {"arms": ["B0", "B1"]}, {"repetitions": 3}):
        with pytest.raises(RawError):
            run_campaign(fabric, _spec(**over), _options(tmp_path, clock))
    # nothing was appended by the refused starts
    assert len(read_raw(tmp_path / "raw").runs) == 2


def test_resume_with_mid_file_corruption_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    fabric = FakeFabric()
    clock = Clock()
    run_campaign(fabric, _spec(), _options(tmp_path, clock, max_runs=4))
    runs = tmp_path / "raw" / "runs.jsonl"
    lines = _lines_of(runs)
    lines[1] = b"garbage\n"
    runs.write_bytes(b"".join(lines))
    before = runs.read_bytes()
    n_jobs = len(fabric.jobs)
    with pytest.raises(RawError):
        run_campaign(fabric, _spec(), _options(tmp_path, clock))
    assert runs.read_bytes() == before
    assert len(fabric.jobs) == n_jobs


def test_duplicate_run_lines_keep_the_first_and_do_not_change_the_analysis(
    tmp_path: Path,
) -> None:
    from evalkit.analysis import analyze

    folder = _folder(tmp_path)
    clean = analyze([read_raw(folder)])
    runs = folder / "runs.jsonl"
    first = _lines_of(runs)[0]
    # a later conflicting duplicate (different makespan) must lose
    other = json.loads(first)
    other["jobs"][0]["end_to_end_ms"] = 999999
    append_line(runs, other)
    append_line(runs, json.loads(first))
    data = read_raw(folder)
    assert set(data.duplicates) == {json.loads(first)["run_id"]}
    dirty = analyze([data])
    # the results are untouched; the duplicate is reported, not silent
    assert {k: v for k, v in dirty.items() if k != "inputs"} == {
        k: v for k, v in clean.items() if k != "inputs"
    }
    assert set(dirty["inputs"][0]["duplicate_runs"]) == {json.loads(first)["run_id"]}


# ------------------------------------------------------------- determinism


def _run_analyze(raw: Path, out: Path, env_over: dict[str, str], cwd: Path) -> None:
    env = {**os.environ, **env_over}
    done = subprocess.run(
        [
            sys.executable,
            str(EVAL_DIR / "analyze.py"),
            "--raw",
            str(raw),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
        cwd=cwd,
    )
    assert done.returncode == 0, done.stderr


def _tree(path: Path) -> dict[str, bytes]:
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def test_analysis_is_byte_identical_across_environments(tmp_path: Path) -> None:
    """Hash seed, time zone, locale, cwd, and the raw folder's location and
    name must not leak into any output byte (D419)."""
    folder = _folder(tmp_path)
    moved = tmp_path / "some other dir" / "raw-renamed"
    shutil.copytree(folder, moved)
    # litter that is not data
    (moved / "runs.jsonl.cut").write_text("junk\n", "utf-8")
    (moved / "notes.txt").write_text("hello\n", "utf-8")
    cwd2 = tmp_path / "cwd2"
    cwd2.mkdir()
    variants = [
        (folder, {"PYTHONHASHSEED": "0", "TZ": "UTC", "LC_ALL": "C"}, tmp_path),
        (
            moved,
            {"PYTHONHASHSEED": "12345", "TZ": "Asia/Kolkata", "LC_ALL": "C"},
            cwd2,
        ),
        (
            folder,
            {
                "PYTHONHASHSEED": "random",
                "TZ": "America/Los_Angeles",
                "LC_ALL": "C.UTF-8",
            },
            cwd2,
        ),
    ]
    trees = []
    for i, (raw, env, cwd) in enumerate(variants):
        out = tmp_path / f"out{i}"
        _run_analyze(raw, out, env, cwd)
        trees.append(_tree(out))
    assert trees[0] == trees[1] == trees[2]
    # no absolute path of this machine inside any output
    blob = b"".join(trees[0].values())
    assert str(tmp_path).encode() not in blob
    assert b"raw-renamed" not in blob
    assert b"SHA256SUMS" in b"".join(k.encode() for k in trees[0])


def test_sha256sums_matches_the_files_it_lists(tmp_path: Path) -> None:
    import hashlib

    folder = _folder(tmp_path)
    out = tmp_path / "out"
    _run_analyze(folder, out, {}, tmp_path)
    sums = (out / "SHA256SUMS").read_text("utf-8").splitlines()
    assert sums
    for line in sums:
        digest, name = line.split("  ", 1)
        if name.startswith("input:"):
            continue  # the raw inputs' own hashes (provenance)
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    # and it lists every analysis file except itself
    listed = {
        line.split("  ", 1)[1]
        for line in sums
        if not line.split("  ", 1)[1].startswith("input:")
    }
    on_disk = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS"
    }
    assert listed == on_disk


def test_a_different_seed_changes_the_output_and_the_same_seed_does_not(
    tmp_path: Path,
) -> None:
    from analyze import run_analysis  # type: ignore[import-not-found]
    from evalkit.analysis import AnalysisSettings

    folder = _folder(tmp_path)
    a = run_analysis([folder], tmp_path / "a", settings=AnalysisSettings(seed=1))
    b = run_analysis([folder], tmp_path / "b", settings=AnalysisSettings(seed=1))
    assert a == b
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")


def test_the_committed_sample_is_not_modified_by_checking_it() -> None:
    import check_sample  # type: ignore[import-not-found]

    before = _tree(SAMPLE_DIR)
    assert check_sample.main(["--no-figures"]) == 0
    assert _tree(SAMPLE_DIR) == before


def test_check_sample_detects_a_tampered_expected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import check_sample  # type: ignore[import-not-found]

    expected = tmp_path / "expected.sha256"
    lines = check_sample.EXPECTED.read_text("utf-8").splitlines()
    digest, name = lines[0].split("  ", 1)
    flipped = ("0" if digest[0] != "0" else "1") + digest[1:]
    expected.write_text("\n".join([f"{flipped}  {name}", *lines[1:]]) + "\n", "utf-8")
    monkeypatch.setattr(check_sample, "EXPECTED", expected)
    assert check_sample.main(["--no-figures"]) == 1
    # a missing/extra entry is a failure too, never a silent pass
    expected.write_text("\n".join(lines[1:]) + "\n", "utf-8")
    assert check_sample.main(["--no-figures"]) != 0


# ---------------------------------------------------------- evidence rules


@pytest.mark.parametrize(
    "build",
    [
        {"git_sha": "a" * 40, "package_version": "1.0.0", "source": "checkout"},
        {"git_sha": "a" * 40, "package_version": "1.0.0"},
        {"git_sha": "a" * 40, "package_version": "1.0.0", "source": None},
        {"git_sha": "a" * 40, "package_version": "1.0.0", "source": "BUNDLE "},
    ],
)
def test_anything_but_a_bundle_build_is_never_evidence(
    tmp_path: Path,
    build: dict,  # type: ignore[type-arg]
) -> None:
    from evalkit.analysis import analyze

    folder = write_raw(
        tmp_path / "raw",
        name="ev",
        arms=("B0", "B1"),
        families={"cpu.primes": ("s",)},
        repetitions=10,
        makespan=lambda f, s, a, b: 1000.0 + b,
        smoke=False,
        builds=(build,),
    )
    assert analyze([read_raw(folder)])["evidence"] is False


def test_ten_reps_bundle_complete_is_evidence_and_nine_is_not(
    tmp_path: Path,
) -> None:
    from evalkit.analysis import analyze

    def make(reps: int, name: str) -> dict:  # type: ignore[type-arg]
        folder = write_raw(
            tmp_path / name,
            name=name,
            arms=("B0", "B1"),
            families={"cpu.primes": ("s",)},
            repetitions=reps,
            makespan=lambda f, s, a, b: 1000.0 + b,
            smoke=False,
        )
        return analyze([read_raw(folder)])

    assert make(10, "ten")["evidence"] is True
    assert make(9, "nine")["evidence"] is False


def test_a_missing_run_makes_the_campaign_not_evidence(tmp_path: Path) -> None:
    from evalkit.analysis import analyze

    folder = write_raw(
        tmp_path / "raw",
        name="inc",
        arms=("B0", "B1"),
        families={"cpu.primes": ("s",)},
        repetitions=10,
        makespan=lambda f, s, a, b: 1000.0 + b,
        smoke=False,
    )
    runs = folder / "runs.jsonl"
    lines = _lines_of(runs)
    runs.write_bytes(b"".join(lines[:-1]))
    assert analyze([read_raw(folder)])["evidence"] is False


# --------------------------------------------------------------- lab hook


pytestmark_hook = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX shell hook"
)


def _hook(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["sh", str(HOOK), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=env,
    )


GOOD = "MESHFORGE_CONTROLLER__PLANNER__CALIBRATION='true'\n"


@pytestmark_hook
@pytest.mark.parametrize(
    "content",
    [
        "LD_PRELOAD='/tmp/x.so'\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML='x'; touch PWNED\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML=$(touch PWNED)\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML=`touch PWNED`\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML='a'\nPATH=/evil\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML=true\n",  # unquoted
        'MESHFORGE_CONTROLLER__PLANNER__ML="true"\n',  # double-quoted
        "MESHFORGE_CONTROLLER__PLANNER__ML='a'b\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML='a\n",  # unterminated
        "MESHFORGE_CONTROLLER__PLANNER__ML='a''\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML='it'\\''s'\n",
        "MESHFORGE_CONTROLLER__PLANNER__ML='true'\r\n",  # CRLF
        " MESHFORGE_CONTROLLER__PLANNER__ML='true'\n",  # leading space
        "MESHFORGE_CONTROLLER__PLANNER__ML='true' \n",  # trailing space
        "meshforge_controller__planner__ml='true'\n",  # case
        "MESHFORGE_CONTROLLER__SECURITY__X='1'\n",  # other section
        "MESHFORGE_CONTROLLER__DATABASE_URL='postgres://evil'\n",
        "MESHFORGE_AGENT__X='1'\n",
        "export MESHFORGE_CONTROLLER__PLANNER__ML='true'\n",
        "MESHFORGE_CONTROLLER__PLANNER__='true'\n",  # empty key
        "MESHFORGE_CONTROLLER__PLANNER__ml='true'\n",  # lower-case key
        "MESHFORGE_CONTROLLER__PLANNER__A-B='true'\n",
        "GOOD='1'\n" + GOOD,
        GOOD + "\\\n",
        GOOD + "[Service]\n",
    ],
)
def test_hook_refuses_everything_but_planner_assignments(
    tmp_path: Path, content: str
) -> None:
    f = tmp_path / "p.env"
    f.write_bytes(content.encode())
    fake_bin = _fake_systemctl(tmp_path)
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    for args in (("--check", str(f)), ("calibrated", str(f))):
        done = _hook(*args, env_extra=env)
        assert done.returncode == 2, (args, done.stdout, done.stderr)
    assert not (tmp_path / "PWNED").exists()
    assert not Path("PWNED").exists()
    assert not (fake_bin / "calls.log").exists(), "systemctl called for a bad file"


@pytestmark_hook
@pytest.mark.parametrize(
    "content",
    [
        GOOD,
        "# comment\n" + GOOD + "\n# another\n",
        'MESHFORGE_CONTROLLER__PLANNER__DISABLED_MODES=\'["mode1","mode3"]\'\n',
        "MESHFORGE_CONTROLLER__PLANNER__ML='$(touch PWNED)'\n",  # literal in systemd
        "MESHFORGE_CONTROLLER__PLANNER__ML=''\n",
        GOOD.rstrip("\n"),  # no trailing newline
    ],
)
def test_hook_accepts_valid_profile_files_without_executing_them(
    tmp_path: Path, content: str
) -> None:
    f = tmp_path / "dir with spaces" / "p.env"
    f.parent.mkdir()
    f.write_text(content, "utf-8")
    done = _hook("--check", str(f))
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "PWNED").exists()
    assert not Path("PWNED").exists()


@pytestmark_hook
@pytest.mark.parametrize(
    "name",
    [
        "",
        "Calibrated",
        "a b",
        "a-b",
        "../etc",
        "a/b",
        "a\nb",
        "a;b",
        "$(id)",
        "`id`",
        "ml.no",
        "é",
    ],
)
def test_hook_refuses_bad_profile_names_before_touching_anything(
    tmp_path: Path, name: str
) -> None:
    f = tmp_path / "p.env"
    f.write_text(GOOD, "utf-8")
    fake_bin = _fake_systemctl(tmp_path)
    done = _hook(
        name,
        str(f),
        env_extra={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert done.returncode != 0, (done.stdout, done.stderr)
    assert not (fake_bin / "calls.log").exists()


@pytestmark_hook
def test_hook_wrong_argument_counts_and_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "p.env"
    f.write_text(GOOD, "utf-8")
    assert _hook().returncode == 2
    assert _hook("calibrated").returncode == 2
    assert _hook("calibrated", str(f), "extra").returncode == 2
    assert _hook("--check").returncode == 2
    assert _hook("--check", str(tmp_path / "missing")).returncode == 2
    assert _hook("--check", str(tmp_path)).returncode == 2  # a directory


def _fake_systemctl(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "systemctl"
    script.write_text(
        '#!/bin/sh\necho "$@" >> "$(dirname "$0")/calls.log"\n'
        'if [ -n "${FAKE_FAIL:-}" ] && [ "$1" = "$FAKE_FAIL" ]; then exit 1; fi\n'
        "exit 0\n",
        "utf-8",
    )
    script.chmod(0o755)
    return bin_dir


@pytestmark_hook
def test_hook_clear_restarts_and_reports_service_state(tmp_path: Path) -> None:
    if Path("/etc/systemd/system/meshforge-controller.service.d").exists():
        pytest.skip("a real controller drop-in exists on this machine")
    fake_bin = _fake_systemctl(tmp_path)
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    done = _hook("--clear", env_extra=env)
    assert done.returncode == 0, done.stderr
    calls = (fake_bin / "calls.log").read_text("utf-8").splitlines()
    assert calls == [
        "daemon-reload",
        "restart meshforge-controller.service",
        "is-active --quiet meshforge-controller.service",
    ]
    # the service not coming back active is a failure the runner will see
    (fake_bin / "calls.log").unlink()
    done = _hook("--clear", env_extra={**env, "FAKE_FAIL": "is-active"})
    assert done.returncode != 0
    done = _hook("--clear", env_extra={**env, "FAKE_FAIL": "restart"})
    assert done.returncode != 0
