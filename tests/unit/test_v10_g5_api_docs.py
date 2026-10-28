"""V1.0 G5 (D414): the API reference in ``docs/api/`` is generated from the code
and cannot drift - ``scripts/gen_api_docs.py --check`` (CI job ``api-docs``).

* the renderers are pure and deterministic (OpenAPI, proto, CLI help);
* every operation of the real controller document, every RPC of every proto
  service and every ``compute`` sub-command is on its page;
* ``--check`` reports a changed page, a missing page and a stray page;
* the committed pages are up to date (Linux: the CI job's platform - some help
  text names platform-specific behaviour).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str) -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SAMPLE: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "9.9", "summary": "Sum | pipe"},
    "paths": {
        "/v1/things/{tid}": {
            "get": {
                "summary": "One thing",
                "tags": ["things"],
                "parameters": [
                    {
                        "name": "tid",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "maxLength": 9},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Thing"}
                            }
                        },
                    },
                    "404": {"description": "missing"},
                },
            },
            "head": {"summary": "Exists?", "responses": {"200": {"description": "y"}}},
        }
    },
    "components": {
        "schemas": {
            "Zeta": {"type": "string", "enum": ["a", "b"]},
            "Thing": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "tags": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Zeta"},
                    },
                },
            },
        }
    },
}


def test_openapi_render_is_complete_and_deterministic() -> None:
    lib = _load("apidoc_openapi")
    first = lib.render(SAMPLE, title="X", source="sample")
    assert first == lib.render(SAMPLE, title="X", source="sample")
    assert first.endswith("\n") and "\r" not in first
    assert "### `GET /v1/things/{tid}`" in first
    assert "### `HEAD /v1/things/{tid}`" in first
    assert "2 operations." in first
    # schemas sorted by name, each with its anchor; refs link to it
    assert first.index("### Thing") < first.index("### Zeta")
    assert '<a id="schema-thing"></a>' in first
    assert "[Thing](#schema-thing)" in first
    assert "| `note` | string (nullable) | no |" in first
    assert "array of [Zeta](#schema-zeta)" in first
    assert "maxLength 9" in first
    assert "Sum | pipe" in first  # prose is not a table cell: kept as is
    assert "enum: `a`, `b`" in first


def test_openapi_table_cells_escape_pipes_and_newlines() -> None:
    lib = _load("apidoc_openapi")
    doc = {
        "paths": {
            "/x": {
                "get": {
                    "summary": "a | b\nc",
                    "responses": {"200": {"description": "1|2"}},
                }
            }
        }
    }
    text = lib.render(doc, title="X", source="s")
    assert "| GET | `/x` | a \\| b c | - |" in text
    assert "| 200 | 1\\|2 | - |" in text


@pytest.mark.extras("controller")
def test_every_controller_operation_is_documented() -> None:
    gen = _load("gen_api_docs")
    from meshforge.controller.http import openapi_document

    page = gen.PAGES["rest-controller.md"]()
    doc = openapi_document()
    lib = _load("apidoc_openapi")
    ops = list(lib.operations(doc))
    assert len(ops) > 50
    for path, method, _ in ops:
        assert f"### `{method.upper()} {path}`" in page, (method, path)
    for name in doc["components"]["schemas"]:
        assert f"\n### {name}\n" in page, name


def test_every_proto_rpc_message_and_enum_is_documented() -> None:
    gen = _load("gen_api_docs")
    proto = _load("apidoc_proto")
    files = list(proto.descriptor_set(ROOT / "proto").file)
    page = gen.grpc_page()
    assert page == gen.grpc_page()
    own = [f for f in files if f.name.startswith("meshforge/")]
    assert {f.name for f in own} == set(gen.PROTO_STATUS), "a proto file has no status"
    for f in own:
        assert f"## `{f.name}`" in page
        for service in f.service:
            assert f"### service `{service.name}`" in page
            for method in service.method:
                assert f"| `{method.name}` |" in page, method.name
        for msg in f.message_type:
            assert f"#### message `{msg.name}`" in page, msg.name
        for enum in f.enum_type:
            assert f"#### enum `{enum.name}`" in page, enum.name
    # the GPU plane is marked deferred (ADR-0012), the session current
    rows = {
        line.split("`")[1]: line
        for line in page.splitlines()
        if line.startswith("| [`")
    }
    assert rows["meshforge/v1/gpu.proto"].endswith("| deferred |")
    assert rows["meshforge/v1/agent.proto"].endswith("| current |")
    gpu = page.split("## `meshforge/v1/gpu.proto`", 1)[1]
    assert gpu.lstrip().startswith("> **Status:** deferred.")
    agent = page.split("## `meshforge/v1/agent.proto`", 1)[1]
    assert agent.lstrip().startswith("> **Status:** current.")


def test_proto_comments_are_attached_to_their_declarations() -> None:
    proto = _load("apidoc_proto")
    files = {f.name: f for f in proto.descriptor_set(ROOT / "proto").file}
    page = "\n".join(proto.render_file(files["meshforge/v1/gpu.proto"]))
    # the leading comment of PutTensor, in its RPC row
    assert (
        "| `PutTensor` | `stream TensorChunk` | `PutTensorResponse` | Upload one tensor"
        in page
    )
    # the file's header comment (detached, before `syntax`)
    assert "GPU plane: GpuService" in page


@pytest.mark.extras("controller", "agent")
def test_cli_page_has_every_command_at_a_fixed_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = _load("gen_api_docs")
    monkeypatch.setenv("COLUMNS", "37")
    narrow = gen.cli_page()
    monkeypatch.setenv("COLUMNS", "250")
    assert gen.cli_page() == narrow, "help text depends on the terminal width"
    for command in (
        "compute run",
        "compute planner ml train",
        "compute fabric invite",
        "compute bench start",
        "meshforge-agent join",
        "meshforge-agent policy",
        "meshforge-controller db",
        "meshforge-controller fabric create",
    ):
        assert f"### `{command}`" in narrow, command
    assert "\x1b[" not in narrow and "╭" not in narrow  # no colour, no rich boxes


def test_cli_help_shows_path_defaults_the_same_on_every_os() -> None:
    """A ``Path`` default is printed with ``/`` even where ``str()`` gives ``\\``.

    CI fix round 4: ``meshforge-controller setup init-config --path`` defaults to
    ``Path("/etc/meshforge/controller.toml")``, which Click printed as
    ``\\etc\\meshforge\\controller.toml`` on Windows. A Windows path object
    stands in for the Windows run here; the default is restored afterwards.
    """
    import typer

    cli = _load("apidoc_cli")
    app = typer.Typer(add_completion=False)

    @app.command()
    def init(
        path: Path = typer.Option(Path("/etc/mf/c.toml"), help="Settings."),  # noqa: B008
    ) -> None:
        """Init."""

    root = typer.main.get_command(app)
    (param,) = (p for p in root.params if p.name == "path")
    windows_default = PureWindowsPath("/etc/mf/c.toml")
    param.default = windows_default
    assert "\\" in str(windows_default)
    ((_, text),) = list(cli._walk(root, ("init",), None))
    assert "[default: /etc/mf/c.toml]" in text, text
    assert "\\" not in text
    assert param.default is windows_default


def test_check_reports_changed_missing_and_stray_pages(tmp_path: Path) -> None:
    gen = _load("gen_api_docs")
    pages = {"a.md": "one\n", "b.md": "two\n"}
    gen.write(tmp_path, pages)
    assert gen.check(tmp_path, pages) == []
    (tmp_path / "a.md").write_text("edited by hand\n", encoding="utf-8")
    (tmp_path / "b.md").unlink()
    (tmp_path / "old.md").write_text("x\n", encoding="utf-8")
    assert gen.check(tmp_path, pages) == [
        "a.md: out of date",
        "b.md: missing",
        "old.md: not generated by this script (remove it)",
    ]


def test_check_sees_a_crlf_checkout_as_drift(tmp_path: Path) -> None:
    gen = _load("gen_api_docs")
    pages = {"a.md": "one\ntwo\n"}
    (tmp_path / "a.md").write_bytes(b"one\r\ntwo\r\n")
    assert gen.check(tmp_path, pages) == ["a.md: out of date"]


@pytest.mark.extras("controller", "agent")
@pytest.mark.skipif(sys.platform != "linux", reason="CI's api-docs job checks on Linux")
def test_committed_api_docs_are_up_to_date() -> None:
    gen = _load("gen_api_docs")
    problems = gen.check(gen.OUT_DIR, gen.render_all())
    assert problems == [], (
        f"{problems}: run `uv run --extra controller --extra agent python "
        "scripts/gen_api_docs.py` and commit docs/api/"
    )
