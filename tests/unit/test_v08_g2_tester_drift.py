"""V0.8 G2 tester r1: the drift gate must actually catch a changed backend
contract (D171, D184 item 3) and a console gate must not be bypassable."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit
pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_openapi.py"
SPEC = ROOT / "frontend" / "openapi.json"
SCHEMA = ROOT / "frontend" / "src" / "api" / "schema.d.ts"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi_g2_tester", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi_g2_tester"] = module
    spec.loader.exec_module(module)
    return module


def _patched(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> None:
    import meshforge.controller.http as http

    real = http.openapi_document

    def doc() -> dict[str, Any]:
        d = copy.deepcopy(real())
        mutate(d)
        return d

    monkeypatch.setattr(http, "openapi_document", doc)


def test_check_passes_on_the_untouched_contract() -> None:
    assert _load().main(["--check", str(SPEC)]) == 0


def test_a_new_backend_route_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched(
        monkeypatch,
        lambda d: d["paths"].update(
            {"/v1/added-by-tester": {"get": {"responses": {"200": {"description": "x"}}}}}
        ),
    )
    assert _load().main(["--check", str(SPEC)]) == 1


def test_a_removed_route_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched(monkeypatch, lambda d: d["paths"].pop("/v1/nodes"))
    assert _load().main(["--check", str(SPEC)]) == 1


def test_a_changed_response_field_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate(d: dict[str, Any]) -> None:
        schemas = d["components"]["schemas"]
        schemas["SessionOut"]["properties"]["session_id"]["type"] = "integer"

    _patched(monkeypatch, mutate)
    assert _load().main(["--check", str(SPEC)]) == 1


def test_a_changed_description_only_also_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-for-byte comparison: even a doc-string edit demands a regenerate
    (strict, by design)."""
    _patched(monkeypatch, lambda d: d["info"].update({"title": d["info"]["title"] + "!"}))
    assert _load().main(["--check", str(SPEC)]) == 1


def test_a_missing_committed_spec_fails_the_gate(tmp_path: Path) -> None:
    assert _load().main(["--check", str(tmp_path / "nope.json")]) == 1


def test_a_hand_edited_openapi_json_fails_the_gate(tmp_path: Path) -> None:
    edited = tmp_path / "openapi.json"
    edited.write_text(SPEC.read_text(encoding="utf-8").replace('"/v1/nodes"', '"/v1/nodez"', 1))
    assert _load().main(["--check", str(edited)]) == 1


def test_a_hand_edited_openapi_json_breaks_the_schema_digest(tmp_path: Path) -> None:
    """Editing openapi.json without regenerating schema.d.ts: the recorded
    digest no longer matches (what the unit drift test relies on)."""
    first = SCHEMA.read_text(encoding="utf-8").splitlines()[0].split(": ", 1)[1].strip()
    edited = SPEC.read_bytes().replace(b'"/v1/nodes"', b'"/v1/nodez"', 1)
    assert hashlib.sha256(edited).hexdigest() != first


def test_export_is_deterministic_across_runs() -> None:
    script = _load()
    assert script.render() == script.render()
    assert script.render() == SPEC.read_text(encoding="utf-8")


# --- the e2e console gate: only the one named line is excused -----------------


def _msg(kind: str, text: str, url: str) -> Any:
    return SimpleNamespace(type=kind, text=text, location={"url": url})


@pytest.fixture
def watch() -> Any:
    sys.path.insert(0, str(ROOT / "tests" / "_support"))
    from mf_dashboard import PageWatch

    page = SimpleNamespace(evaluate=lambda _js: [])
    w = PageWatch(page)  # type: ignore[arg-type]
    w.allow("status of 401", "/v1/session")
    return w


BOOT = "Failed to load resource: the server responded with a status of 401 ()"


def test_gate_excuses_exactly_the_session_probe_401(watch: Any) -> None:
    watch._on_console(_msg("error", BOOT, "https://h:1/v1/session"))
    assert watch.console_errors == []


@pytest.mark.parametrize(
    ("text", "url"),
    [
        (BOOT, "https://h:1/v1/nodes"),  # another endpoint's 401
        (BOOT, "https://h:1/v1/session/other"),  # sub-path
        (BOOT, "https://h:1/v1/session?x=/v1/session/x"),  # suffix only by prefix
        ("Failed to load resource: the server responded with a status of 500 ()", "https://h:1/v1/session"),
        ("Uncaught TypeError: x is not a function", "https://h:1/assets/index.js"),
        ("Error: status of 401", "https://h:1/assets/index.js"),  # text matches, URL not
    ],
)
def test_gate_rejects_everything_else(watch: Any, text: str, url: str) -> None:
    watch._on_console(_msg("error", text, url))
    assert len(watch.console_errors) == 1, (text, url)


def test_gate_still_fails_on_page_errors_and_csp_even_with_the_allowance() -> None:
    page = SimpleNamespace(evaluate=lambda _js: [{"directive": "style-src"}])
    from mf_dashboard import PageWatch

    w = PageWatch(page)  # type: ignore[arg-type]
    w.allow("status of 401", "/v1/session")
    with pytest.raises(AssertionError, match="CSP"):
        w.assert_clean()
    w2 = PageWatch(SimpleNamespace(evaluate=lambda _js: []))  # type: ignore[arg-type]
    w2.page_errors.append("boom")
    with pytest.raises(AssertionError, match="uncaught"):
        w2.assert_clean()


def test_gate_counts_assert_type_and_ignores_warnings(watch: Any) -> None:
    watch._on_console(_msg("assert", "x", "https://h/a.js"))
    watch._on_console(_msg("warning", "x", "https://h/a.js"))
    assert len(watch.console_errors) == 1
