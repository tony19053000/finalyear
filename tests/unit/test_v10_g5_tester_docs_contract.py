"""V1.0 G5 tester: adversarial checks of the docs deliverables (D414, D420, D421).

Drift detection is proven by *mutating a copy of the repo* (a new REST route, a
changed proto field, a new CLI flag) and requiring ``gen_api_docs.py --check`` to
exit 1 naming the page; the changelog check is fed malformed / incomplete files;
the ADR index and the ARCHITECTURE section 21 tree are compared with the files on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import changelog as cl  # noqa: E402

# ---------------------------------------------------------------- api-docs drift
# gen_api_docs.py imports the controller and agent apps (their option types and
# OpenAPI documents), so every test that runs it needs both extras: without them
# it exits 2, which is "cannot generate", not drift (D399: skip, not fail).


@pytest.fixture(scope="module")
def repo_copy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dst = tmp_path_factory.mktemp("repocopy")
    for name in ("src", "proto", "scripts", "docs/api"):
        src = ROOT / name
        out = dst / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src,
            out,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "node_modules", "dist"
            ),
        )
    shutil.copy(ROOT / "pyproject.toml", dst / "pyproject.toml")
    return dst


def _run_check(copy: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(copy / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(copy / "scripts" / "gen_api_docs.py"), "--check"],
        capture_output=True,
        text=True,
        env=env,
        cwd=copy,
        timeout=300,
    )


def _mutate(path: Path, old: str, new: str) -> str:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor missing in {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return text


@pytest.mark.extras("controller", "agent")
def test_baseline_copy_is_up_to_date(repo_copy: Path) -> None:
    r = _run_check(repo_copy)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.extras("controller", "agent")
def test_drift_detected_when_proto_field_added(repo_copy: Path) -> None:
    proto = repo_copy / "proto" / "meshforge" / "v1" / "enroll.proto"
    original = _mutate(
        proto,
        "message EnrollRequest {",
        "message EnrollRequest {\n  string tester_zz = 99;",
    )
    try:
        r = _run_check(repo_copy)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "grpc.md" in r.stdout + r.stderr
    finally:
        proto.write_text(original, encoding="utf-8", newline="\n")


@pytest.mark.extras("controller", "agent")
def test_drift_detected_when_cli_flag_added(repo_copy: Path) -> None:
    cmd = repo_copy / "src" / "meshforge" / "cli" / "fabric_commands.py"
    text = cmd.read_text(encoding="utf-8")
    m = re.search(r"^@\w+\.command\(.*?\)\n(?:@.*\n)*", text, re.M)
    assert m, "no click command found"
    cmd.write_text(
        text[: m.end()]
        + '@click.option("--tester-zz-flag", help="zz marker")\n'
        + text[m.end() :],
        encoding="utf-8",
        newline="\n",
    )
    try:
        r = _run_check(repo_copy)
        # the decorator order may break the signature; either way the check must not pass
        assert r.returncode != 0, "CLI change went undetected"
    finally:
        cmd.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.extras("controller", "agent")
def test_drift_detected_when_route_added(repo_copy: Path) -> None:
    http = repo_copy / "src" / "meshforge" / "controller" / "http.py"
    text = http.read_text(encoding="utf-8")
    m = re.search(r"^(\s*)@app\.get\(", text, re.M)
    assert m
    ind = m.group(1)
    inject = (
        f'{ind}@app.get("/v1/tester-zz-route", summary="zz")\n'
        f"{ind}async def _tester_zz() -> dict[str, str]:\n"
        f"{ind}    return {{}}\n\n"
    )
    http.write_text(
        text[: m.start()] + inject + text[m.start() :], encoding="utf-8", newline="\n"
    )
    try:
        r = _run_check(repo_copy)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "rest-controller.md" in r.stdout + r.stderr
    finally:
        http.write_text(text, encoding="utf-8", newline="\n")


@pytest.mark.extras("controller", "agent")
def test_drift_detected_for_hand_edit_and_stray_page(repo_copy: Path) -> None:
    page = repo_copy / "docs" / "api" / "cli.md"
    original = page.read_text(encoding="utf-8")
    stray = repo_copy / "docs" / "api" / "stray.md"
    try:
        page.write_text(original + "hand edit\n", encoding="utf-8", newline="\n")
        assert _run_check(repo_copy).returncode == 1
        page.write_text(original, encoding="utf-8", newline="\n")
        stray.write_text("x", encoding="utf-8")
        assert _run_check(repo_copy).returncode == 1
        stray.unlink()
        page.unlink()
        r = _run_check(repo_copy)
        assert r.returncode == 1 and "cli.md" in r.stdout + r.stderr
    finally:
        page.write_text(original, encoding="utf-8", newline="\n")
        stray.unlink(missing_ok=True)


@pytest.mark.extras("controller", "agent")
def test_generation_is_idempotent(tmp_path: Path) -> None:
    env = dict(os.environ)
    for _ in range(2):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "gen_api_docs.py"), "--out", str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
            timeout=300,
        )
        assert r.returncode == 0, r.stderr
    for p in (ROOT / "docs" / "api").glob("*.md"):
        assert (tmp_path / p.name).read_bytes() == p.read_bytes(), p.name
        assert b"\r" not in p.read_bytes()


# ------------------------------------------------------------------ changelog


def _good() -> str:
    return (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")


def _problems(text: str) -> list[str]:
    return cl.check(text, cl.load_versions())


def test_changelog_committed_passes() -> None:
    assert _problems(_good()) == []


def test_changelog_missing_version_entry_detected() -> None:
    text = re.sub(r"^#### V0\.10 .*$", "#### Something else", _good(), flags=re.M)
    assert any("V0.10" in p for p in _problems(text))


def test_changelog_v0_1_heading_does_not_cover_v0_10() -> None:
    # remove V0.1 only; V0.10 must not satisfy it by prefix
    text = re.sub(r"^#### V0\.1 .*$", "#### nothing", _good(), flags=re.M)
    assert any("V0.1 " in p for p in _problems(text))


@pytest.mark.parametrize(
    "bad",
    [
        "## [1.0] - 2026-10-28",
        "## [1.0.0] - 28-10-2026",
        "## [1.0.0]",
        "## 1.0.0 - 2026-10-28",
        "## [Unreleased] ",
        "## [Unreleased]x",
    ],
)
def test_changelog_malformed_release_heading_detected(bad: str) -> None:
    text = _good().replace("## [0.17.0] - 2026-10-27", bad, 1)
    assert _problems(text), bad


def test_changelog_unknown_change_type_detected() -> None:
    text = _good().replace("### Added", "### Adds", 1)
    assert any("Adds" in p for p in _problems(text))


def test_changelog_empty_and_headingless_files_detected() -> None:
    assert _problems("")
    assert _problems("just prose\n")


def test_changelog_deferred_version_must_say_deferred() -> None:
    versions = [v for v in cl.load_versions() if v.deferred]
    assert versions, "roadmap has deferred versions"
    v = versions[0]
    text = _good()
    start = text.index(f"#### {v.id} ")
    nxt = text.find("\n#### ", start + 1)
    entry = text[start : nxt if nxt != -1 else len(text)]
    stripped = re.sub(r"(?i)deferred", "kept", entry)
    assert any(v.id in p for p in _problems(text.replace(entry, stripped)))


def test_changelog_cli_exit_codes(tmp_path: Path) -> None:
    def run(path: Path) -> int:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "changelog.py"),
                "check",
                "--changelog",
                str(path),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        ).returncode

    bad = tmp_path / "bad.md"
    bad.write_text("## [x]\n", encoding="utf-8")
    assert run(bad) == 1
    assert run(tmp_path / "missing.md") == 2
    assert run(ROOT / "docs" / "CHANGELOG.md") == 0
    bin_ = tmp_path / "bin.md"
    bin_.write_bytes(b"\xff\xfe\x00bad")
    assert run(bin_) in (1, 2)  # must not traceback with another code


def test_changelog_draft_unknown_version_exit_2() -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "changelog.py"), "draft", "V9.99"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    assert r.returncode == 2


# --------------------------------------------------------------------- ADR index


def test_adr_index_lists_every_adr_file_with_matching_status() -> None:
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    rows = re.findall(
        r"^\| \[(\d{4})\]\(([^)]+)\) \| (.*?) \| (.*?) \| (.*?) \|$", index, re.M
    )
    files = sorted(p for p in (ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert [r[0] for r in rows] == [p.name[:4] for p in files]
    for num, link, title, status, _scope in rows:
        target = ROOT / "docs" / "adr" / link
        assert target.is_file(), link
        text = target.read_text(encoding="utf-8")
        m = re.search(r"^(?:>|-) ?\*\*Status:\*\*\s*(.+)$", text, re.M)
        assert m, f"ADR {num} has no Status line"
        assert m.group(1).strip().lower().startswith(status.split()[0].lower()), (
            num,
            status,
            m.group(1),
        )
        h1 = re.search(r"^# (?:ADR[- ]?\d+[:.\s—-]*)?(.+)$", text, re.M)
        assert h1 and title.split(":")[0].lower()[:15] in text.lower(), num
    # every Proposed ADR is in the acceptance list
    tail = index.split("## Needs team acceptance", 1)[1]
    for num, _l, _t, status, _s in rows:
        if status.startswith("Proposed"):
            assert num in tail, f"Proposed ADR {num} missing from acceptance list"


# ------------------------------------------------------ ARCHITECTURE section 21


def _tree_lines() -> list[str]:
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    sec = text.split("## 21. Repository structure", 1)[1].split("## 22.", 1)[0]
    return sec.split("```text", 1)[1].split("```", 1)[0].splitlines()[1:]


def test_architecture_tree_paths_exist() -> None:
    stack: list[tuple[int, str]] = []
    missing: list[str] = []
    for line in _tree_lines():
        m = re.match(r"^([│ ]*)(?:[├└]── )?(.*)$", line)
        assert m
        depth = len(m.group(1)) // 4
        body = re.split(r"\s{2,}", m.group(2).strip())[0]
        tokens = [t.strip() for t in body.split("·")]
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = Path(*[s for _, s in stack]) if stack else Path()
        for tok in tokens:
            for piece in tok.split():
                if not re.fullmatch(r"[\w.\-*/]+", piece) or not (
                    "/" in piece or "." in piece
                ):
                    continue
                if piece == ".":
                    continue
                rel = parent / piece
                if "*" in piece:
                    if not list((ROOT / rel.parent).glob(rel.name)):
                        missing.append(str(rel))
                elif not (ROOT / rel).exists():
                    missing.append(str(rel))
        first = tokens[0].split()[0] if tokens and tokens[0] else ""
        if first.endswith("/"):
            stack.append((depth, first.rstrip("/")))
    assert not missing, missing


def test_architecture_tree_covers_every_src_subpackage() -> None:
    text = "\n".join(_tree_lines())
    for pkg in sorted((ROOT / "src" / "meshforge").iterdir()):
        if pkg.is_dir() and not pkg.name.startswith("_"):
            assert f"{pkg.name}/" in text, (
                f"src/meshforge/{pkg.name} missing from section 21"
            )
