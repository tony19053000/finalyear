"""V1.0 G3 tester: attack ``scripts/bandit_gate.py`` (zero untriaged high/critical).

Runs the REAL bandit against throw-away source trees: a new medium/high finding
must fail the gate, a deleted/changed one must make its triage entry stale.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

GATE = Path(__file__).resolve().parents[2] / "scripts" / "bandit_gate.py"
_spec = importlib.util.spec_from_file_location("tester_bandit_gate", GATE)
assert _spec and _spec.loader
bg = importlib.util.module_from_spec(_spec)
sys.modules["tester_bandit_gate"] = bg
_spec.loader.exec_module(bg)

pytestmark.append(
    pytest.mark.skipif(shutil.which("bandit") is None and importlib.util.find_spec("bandit") is None,
                       reason="bandit not installed")
)

HIGH = "import hashlib\nh = hashlib.md5(b'x')\n"          # B324 HIGH
MEDIUM = "import yaml\nd = yaml.load('a: 1')\n"           # B506 MEDIUM
LOW = "import subprocess\nsubprocess.run(['ls'])\n"       # B404/B603 LOW only
TRIAGE_HDR = ""


def entry(test_id: str, file: str, line: str, reason: str = "constant input only here") -> str:
    return (f'[[finding]]\ntest_id = "{test_id}"\nfile = "{file}"\nline = {json.dumps(line)}\n'
            f'reason = "{reason}"\nref = "tester"\n')


def tree(tmp_path: Path, files: dict[str, str], triage: str = "") -> tuple[Path, Path]:
    for name, body in files.items():
        p = tmp_path / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    t = tmp_path / "triage.toml"
    t.write_text(triage, encoding="utf-8")
    return tmp_path, t


def run(repo: Path, triage: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = bg.main(["--repo", str(repo), "--triage", str(triage)])
    return code, capsys.readouterr().out


def test_real_bandit_untriaged_high_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": HIGH})
    code, out = run(repo, t, capsys)
    assert code == 1 and "UNTRIAGED" in out and "B324" in out


def test_real_bandit_untriaged_medium_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": MEDIUM})
    assert run(repo, t, capsys)[0] == 1


def test_real_bandit_clean_and_low_only_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": LOW, "b.py": "x = 1\n"})
    assert run(repo, t, capsys)[0] == 0


def test_triaged_then_line_edited_is_untriaged_and_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": HIGH}, entry("B324", "src/a.py", "h = hashlib.md5(b'x')"))
    assert run(repo, t, capsys)[0] == 0
    (repo / "src" / "a.py").write_text("import hashlib\nh = hashlib.md5(b'y')\n")
    code, out = run(repo, t, capsys)
    assert code == 1 and "UNTRIAGED" in out and "STALE" in out


def test_fixed_finding_leaves_a_stale_entry_that_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": "x = 1\n"}, entry("B324", "src/a.py", "h = hashlib.md5(b'x')"))
    code, out = run(repo, t, capsys)
    assert code == 1 and "STALE" in out


def test_same_text_in_another_file_is_not_covered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": HIGH, "b.py": HIGH}, entry("B324", "src/a.py", "h = hashlib.md5(b'x')"))
    code, out = run(repo, t, capsys)
    assert code == 1 and "UNTRIAGED" in out and "src/b.py" in out


def test_other_test_id_on_a_triaged_line_is_not_covered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": HIGH}, entry("B608", "src/a.py", "h = hashlib.md5(b'x')"))
    assert run(repo, t, capsys)[0] == 1


@pytest.mark.xfail(strict=True, reason="TG3-6: keys are (test_id,file,line text): a copy-pasted identical vulnerable line in the same file is masked by the one triage entry")
def test_duplicate_vulnerable_line_in_same_file_is_not_masked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "import hashlib\nh = hashlib.md5(b'x')\n\n\ndef f():\n    h = hashlib.md5(b'x')\n    return h\n"
    repo, t = tree(tmp_path, {"a.py": body}, entry("B324", "src/a.py", "h = hashlib.md5(b'x')"))
    code, out = run(repo, t, capsys)
    assert code == 1, out


def test_nosec_comment_does_not_silently_hide_in_shipped_src() -> None:
    """A `# nosec` would suppress bandit before the gate sees it."""
    src = Path(__file__).resolve().parents[2] / "src"
    offenders = [str(p) for p in src.rglob("*.py") if "# nosec" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []


@pytest.mark.parametrize(
    "bad",
    [
        entry("B324", "src/a.py", "x", reason="ok"),
        entry("B324", "src/a.py", "x", reason="   "),
        '[[finding]]\ntest_id = "B324"\nfile = "src/a.py"\nline = "x"\nreason = "a real sentence here"\n',
        entry("B324", "src/a.py", "x") + entry("B324", "src/a.py", "  x  "),
        "[[finding]\n",
    ],
)
def test_malformed_triage_never_passes(bad: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"a.py": "x = 1\n"}, bad)
    assert run(repo, t, capsys)[0] == 1


def test_missing_triage_file_is_not_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _ = tree(tmp_path, {"a.py": "x = 1\n"})
    assert bg.main(["--repo", str(repo), "--triage", str(tmp_path / "nope.toml")]) == 1


def test_bandit_parse_error_in_source_is_not_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file bandit cannot parse must not silently count as clean."""
    repo, t = tree(tmp_path, {"a.py": "def (:\n"})
    code, out = run(repo, t, capsys)
    assert code != 0, out


def test_high_inside_unicode_path_and_nested_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, t = tree(tmp_path, {"pkg/ünï/c.py": HIGH})
    code, out = run(repo, t, capsys)
    assert code == 1 and "UNTRIAGED" in out


def test_real_repo_gate_is_green_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    assert bg.main([]) == 0
    assert "0 untriaged, 0 stale" in capsys.readouterr().out
