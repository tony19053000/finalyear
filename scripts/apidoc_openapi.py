"""OpenAPI document -> Markdown reference (V1.0 G5, D414; used by gen_api_docs.py).

Pure function of the document: no clock, no paths, sorted where the document
has no order of its own (operations keep the document's path order, which
FastAPI derives from the route table; schemas are sorted by name). The same
document always renders the same bytes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

METHODS: Final = ("get", "head", "post", "put", "patch", "delete", "options")
REF_PREFIX: Final = "#/components/schemas/"


def _cell(text: object) -> str:
    """One Markdown table cell: one line, pipes escaped."""
    flat = " ".join(str(text).split())
    return flat.replace("|", "\\|") if flat else "-"


def anchor(name: str) -> str:
    """The GitHub-style heading anchor of a schema heading ``name``."""
    return "schema-" + "".join(c if c.isalnum() else "-" for c in name.lower())


def type_of(schema: Mapping[str, Any]) -> str:
    """A short, readable type for a JSON schema fragment."""
    if "$ref" in schema:
        name = str(schema["$ref"]).removeprefix(REF_PREFIX)
        return f"[{name}](#{anchor(name)})"
    for key in ("anyOf", "oneOf"):
        if key in schema:
            parts = [type_of(s) for s in schema[key]]
            nullable = "null" in parts
            rest = [p for p in parts if p != "null"]
            text = " or ".join(rest) if rest else "null"
            return f"{text} (nullable)" if nullable else text
    if "allOf" in schema:
        return " and ".join(type_of(s) for s in schema["allOf"])
    if "enum" in schema:
        return "enum: " + ", ".join(f"`{v}`" for v in schema["enum"])
    if "const" in schema:
        return f"`{schema['const']}`"
    kind = schema.get("type")
    if isinstance(kind, list):
        return " or ".join(str(k) for k in kind)
    if kind == "array":
        items = schema.get("items")
        return f"array of {type_of(items)}" if isinstance(items, Mapping) else "array"
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, Mapping) and extra:
            return f"object of {type_of(extra)}"
        return "object"
    if kind is None:
        return "any"
    fmt = schema.get("format")
    return f"{kind} ({fmt})" if fmt else str(kind)


def _constraints(schema: Mapping[str, Any]) -> str:
    keys = (
        *("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"),
        *("minLength", "maxLength", "minItems", "maxItems", "pattern"),
    )
    found = [f"{k} {schema[k]}" for k in keys if k in schema]
    if "default" in schema:
        found.append(f"default `{schema['default']}`")
    return ", ".join(found)


def _schema_ref(content: Mapping[str, Any] | None) -> str:
    if not content:
        return "-"
    rows = []
    for media in sorted(content):
        schema = content[media].get("schema")
        rows.append(f"`{media}` {type_of(schema) if schema else ''}".strip())
    return "; ".join(rows)


def _operation(path: str, method: str, op: Mapping[str, Any]) -> list[str]:
    lines = [f"### `{method.upper()} {path}`", ""]
    if op.get("summary"):
        lines += [str(op["summary"]).strip(), ""]
    if op.get("description"):
        lines += [str(op["description"]).strip(), ""]
    params = op.get("parameters") or []
    if params:
        lines += ["| Parameter | In | Type | Required | Description |"]
        lines += ["|---|---|---|---|---|"]
        for p in params:
            schema = p.get("schema") or {}
            extra = _constraints(schema)
            desc = p.get("description") or schema.get("description") or ""
            if extra:
                desc = f"{desc} ({extra})" if desc else extra
            lines.append(
                f"| `{p['name']}` | {p['in']} | {_cell(type_of(schema))} | "
                f"{'yes' if p.get('required') else 'no'} | {_cell(desc)} |"
            )
        lines.append("")
    body = op.get("requestBody")
    if body:
        req = "required" if body.get("required") else "optional"
        lines += [f"**Request body** ({req}): {_schema_ref(body.get('content'))}", ""]
    responses = op.get("responses") or {}
    if responses:
        lines += ["| Status | Meaning | Body |", "|---|---|---|"]
        for code in sorted(responses):
            r = responses[code]
            lines.append(
                f"| {code} | {_cell(r.get('description', ''))} | "
                f"{_cell(_schema_ref(r.get('content')))} |"
            )
        lines.append("")
    return lines


def _schema(name: str, schema: Mapping[str, Any]) -> list[str]:
    lines = [f'<a id="{anchor(name)}"></a>', "", f"### {name}", ""]
    if schema.get("description"):
        lines += [str(schema["description"]).strip(), ""]
    props = schema.get("properties")
    if isinstance(props, Mapping) and props:
        required = set(schema.get("required") or ())
        lines += ["| Field | Type | Required | Description |", "|---|---|---|---|"]
        for field in sorted(props):
            sub = props[field]
            desc = sub.get("description", "")
            extra = _constraints(sub)
            if extra:
                desc = f"{desc} ({extra})" if desc else extra
            lines.append(
                f"| `{field}` | {_cell(type_of(sub))} | "
                f"{'yes' if field in required else 'no'} | {_cell(desc)} |"
            )
        lines.append("")
    else:
        lines += [f"Type: {type_of(schema)}", ""]
    return lines


def operations(document: Mapping[str, Any]) -> Iterable[tuple[str, str, Any]]:
    """Every ``(path, method, operation)`` in document order."""
    for path, item in (document.get("paths") or {}).items():
        for method in METHODS:
            if method in item:
                yield path, method, item[method]


def render(document: Mapping[str, Any], *, title: str, source: str) -> str:
    """The Markdown reference of one OpenAPI ``document``."""
    info = document.get("info") or {}
    ops = list(operations(document))
    lines = [
        f"# {title}",
        "",
        f"<!-- Generated by scripts/gen_api_docs.py from {source}. DO NOT EDIT. -->",
        "",
        f"API: **{info.get('title', '')}**, version `{info.get('version', '')}`, "
        f"OpenAPI {document.get('openapi', '')}.",
        "",
    ]
    if info.get("summary"):
        lines += [str(info["summary"]).strip(), ""]
    if info.get("description"):
        lines += [str(info["description"]).strip(), ""]
    lines += [f"{len(ops)} operations.", "", "## Endpoints", ""]
    lines += ["| Method | Path | Summary | Tags |", "|---|---|---|---|"]
    for path, method, op in ops:
        tags = ", ".join(op.get("tags") or ())
        lines.append(
            f"| {method.upper()} | `{path}` | {_cell(op.get('summary', ''))} | "
            f"{_cell(tags)} |"
        )
    lines += ["", "## Operations", ""]
    for path, method, op in ops:
        lines += _operation(path, method, op)
    schemas = (document.get("components") or {}).get("schemas") or {}
    lines += ["## Schemas", ""]
    for name in sorted(schemas):
        lines += _schema(name, schemas[name])
    return "\n".join(lines).rstrip("\n") + "\n"
