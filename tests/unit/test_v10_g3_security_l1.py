"""V1.0 G3 (D413) L1: the manifest loader stays safe, the Windows SID hash is
declared non-security, and ``scripts/bandit_gate.py`` gates bandit findings.

The gate is tested on synthetic bandit JSON reports (no bandit run here), plus
one run over the real triage file to prove it parses and every entry is
well-formed. The full ``bandit -r src -ll`` run is the CI lint step.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from meshforge.common import manifest, windows_service

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "scripts" / "bandit_gate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mf_bandit_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bg = _load()


# ------------------------------------------------------------------ manifest
def test_strict_loader_is_a_safe_loader_subclass() -> None:
    loader = manifest._StrictSafeLoader
    assert issubclass(loader, yaml.SafeLoader)
    assert not issubclass(loader, (yaml.FullLoader, yaml.UnsafeLoader))
    # the constructors are SafeConstructor's: no python/object tags
    assert "tag:yaml.org,2002:python/object" not in loader.yaml_constructors
    assert not any(
        str(tag).startswith("tag:yaml.org,2002:python/")
        for tag in loader.yaml_multi_constructors
    )


def test_strict_loader_refuses_python_object_tags() -> None:
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load(
            "!!python/object/apply:os.system ['true']",
            Loader=manifest._StrictSafeLoader,  # noqa: S506 - the point of the test
        )


def test_manifest_uses_only_the_strict_loader() -> None:
    """Every ``yaml.load`` call in the manifest module passes the strict loader."""
    tree = ast.parse(Path(manifest.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("load", "load_all", "unsafe_load", "full_load")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
    ]
    assert calls, "manifest no longer calls yaml.load - update this test"
    for call in calls:
        assert call.func.attr == "load"  # type: ignore[attr-defined]
        loaders = [k.value for k in call.keywords if k.arg == "Loader"]
        assert len(loaders) == 1
        assert isinstance(loaders[0], ast.Name)
        assert loaders[0].id == "_StrictSafeLoader"


# ------------------------------------------------------------ windows service
def test_service_sid_hash_is_marked_not_for_security() -> None:
    source = Path(windows_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    sha1_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sha1"
    ]
    assert sha1_calls
    for call in sha1_calls:
        flags = {k.arg: k.value for k in call.keywords}
        value = flags.get("usedforsecurity")
        assert isinstance(value, ast.Constant) and value.value is False


def test_service_sid_value_unchanged() -> None:
    """The documented derivation (checked against ``sc showsid`` in SP1)."""
    assert windows_service.service_sid("TrustedInstaller") == (
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )


# ------------------------------------------------------------------- the gate
def _report(repo: Path, *results: dict[str, object]) -> dict[str, object]:
    return {"errors": [], "results": list(results)}


def _result(
    file: str, line: int, test_id: str = "B608", severity: str = "MEDIUM"
) -> dict[str, object]:
    return {
        "filename": file,
        "line_number": line,
        "test_id": test_id,
        "issue_severity": severity,
        "issue_text": "x",
    }


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "x = 1\nq = f'SELECT {T}'\nh = sha1(b'')\n", encoding="utf-8"
    )
    return tmp_path


def _triage(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "triage.toml"
    path.write_text(body, encoding="utf-8")
    return path


ENTRY = (
    '[[finding]]\ntest_id = "B608"\nfile = "src/a.py"\n'
    "line = \"q = f'SELECT {T}'\"\n"
    'reason = "T is a module constant table name."\nref = "D1"\n'
)


def _run(
    tmp_path: Path,
    report: dict[str, object],
    triage: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    rp = tmp_path / "report.json"
    rp.write_text(json.dumps(report), encoding="utf-8")
    code = bg.main(
        ["--triage", str(triage), "--report", str(rp), "--repo", str(tmp_path)]
    )
    return code, capsys.readouterr().out


def test_gate_passes_when_every_finding_is_triaged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    code, out = _run(
        repo, _report(repo, _result("src/a.py", 2)), _triage(repo, ENTRY), capsys
    )
    assert code == 0, out
    assert "0 untriaged" in out


def test_gate_is_keyed_by_line_text_not_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "a.py").write_text(
        "\n\nx = 1\nq = f'SELECT {T}'\n", encoding="utf-8"
    )
    code, out = _run(
        repo,
        _report(repo, _result(str(repo / "src" / "a.py"), 4)),  # absolute path too
        _triage(repo, ENTRY),
        capsys,
    )
    assert code == 0, out


def test_gate_fails_on_an_untriaged_high_and_prints_a_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    report = _report(
        repo, _result("src/a.py", 2), _result("src/a.py", 3, "B324", "HIGH")
    )
    code, out = _run(repo, report, _triage(repo, ENTRY), capsys)
    assert code == 1
    assert "UNTRIAGED HIGH   B324 src/a.py:3" in out
    assert 'test_id = "B324"' in out and "line = \"h = sha1(b'')\"" in out


def test_gate_ignores_low_severity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    report = _report(
        repo, _result("src/a.py", 2), _result("src/a.py", 3, "B101", "LOW")
    )
    code, _ = _run(repo, report, _triage(repo, ENTRY), capsys)
    assert code == 0


def test_gate_fails_on_a_stale_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    code, out = _run(repo, _report(repo), _triage(repo, ENTRY), capsys)
    assert code == 1
    assert "STALE" in out


def test_gate_fails_on_a_changed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    (repo / "src" / "a.py").write_text(
        "x = 1\nq = f'SELECT {T} WHERE {user}'\n", encoding="utf-8"
    )
    code, out = _run(
        repo, _report(repo, _result("src/a.py", 2)), _triage(repo, ENTRY), capsys
    )
    assert code == 1 and "UNTRIAGED" in out


@pytest.mark.parametrize(
    "body",
    [
        ENTRY.replace('reason = "T is a module constant table name."', 'reason = "ok"'),
        ENTRY.replace('ref = "D1"\n', ""),
        ENTRY + ENTRY,
        "finding = 3\n",
    ],
    ids=["short-reason", "no-ref", "duplicate", "not-an-array"],
)
def test_gate_rejects_a_malformed_triage(
    tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    code, _ = _run(
        repo, _report(repo, _result("src/a.py", 2)), _triage(repo, body), capsys
    )
    assert code == 1


def test_gate_exit_2_when_bandit_output_is_unusable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    rp = tmp_path / "report.json"
    rp.write_text("not json", encoding="utf-8")
    code = bg.main(
        [
            "--triage",
            str(_triage(repo, ENTRY)),
            "--report",
            str(rp),
            "--repo",
            str(repo),
        ]
    )
    assert code == 2


def test_real_triage_file_is_well_formed() -> None:
    keys = bg.load_triage(bg.TRIAGE)
    assert keys
    for _test_id, file, _line in keys:
        assert (REPO / file).is_file(), file
