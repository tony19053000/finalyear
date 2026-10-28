"""``proto/`` -> Markdown gRPC reference (V1.0 G5, D414; used by gen_api_docs.py).

The doc generator is protoc itself (``grpc_tools``, pinned by ``uv.lock``, the
same compiler ``scripts/gen_protos.py`` uses): it writes a
``FileDescriptorSet`` with source info, and this module renders services,
RPCs, messages, enums and their comments from it. No second toolchain
(``protoc-gen-doc`` is a Go binary) and no hand-written parser - what is
documented is exactly what protoc compiled.

Deterministic: files sorted by name, declarations in source order, comments
normalised to LF, nothing machine-specific in the output.
"""

from __future__ import annotations

import importlib.resources
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from google.protobuf import descriptor_pb2

# FileDescriptorProto / DescriptorProto field numbers used in source-info paths
_MESSAGE: Final = 4
_ENUM: Final = 5
_SERVICE: Final = 6
_SYNTAX: Final = 12
_FIELD: Final = 2
_NESTED: Final = 3
_NESTED_ENUM: Final = 4
_METHOD: Final = 2
_VALUE: Final = 2

_TYPES: Final = {
    v: k.removeprefix("TYPE_").lower()
    for k, v in descriptor_pb2.FieldDescriptorProto.Type.items()
}

Comments = Mapping[tuple[int, ...], str]


class ProtoDocError(RuntimeError):
    """protoc could not compile the protos."""


def descriptor_set(proto_root: Path) -> descriptor_pb2.FileDescriptorSet:
    """Compile every ``*.proto`` under ``proto_root`` into one descriptor set."""
    protos = sorted(
        p.relative_to(proto_root).as_posix() for p in proto_root.rglob("*.proto")
    )
    if not protos:
        raise ProtoDocError(f"no .proto files under {proto_root}")
    wkt = importlib.resources.files("grpc_tools") / "_proto"
    with tempfile.TemporaryDirectory(prefix="mf-apidoc-") as tmp:
        out = Path(tmp) / "set.pb"
        done = subprocess.run(
            [sys.executable, "-m", "grpc_tools.protoc", "-I.", f"-I{wkt}",
             "--include_source_info", f"--descriptor_set_out={out}", *protos],
            cwd=proto_root, capture_output=True, text=True, encoding="utf-8",
            check=False,
        )  # fmt: skip
        if done.returncode != 0:
            raise ProtoDocError(f"protoc failed ({done.returncode}): {done.stderr}")
        result = descriptor_pb2.FileDescriptorSet()
        result.ParseFromString(out.read_bytes())
    return result


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    # protoc keeps the one space after "//"; drop it, keep relative indentation
    lines = [line[1:] if line.startswith(" ") else line for line in lines]
    return "\n".join(lines).strip("\n")


def comments(file: descriptor_pb2.FileDescriptorProto) -> dict[tuple[int, ...], str]:
    """Source path -> its leading (else trailing) comment, cleaned."""
    found: dict[tuple[int, ...], str] = {}
    for loc in file.source_code_info.location:
        path = tuple(loc.path)
        text = loc.leading_comments or loc.trailing_comments
        if path == (_SYNTAX,):
            text = "\n\n".join(loc.leading_detached_comments) or text
        if text:
            found[path] = _clean(text)
    return found


def _cell(text: str) -> str:
    flat = " ".join(text.split())
    return flat.replace("|", "\\|") if flat else "-"


def _type(field: descriptor_pb2.FieldDescriptorProto) -> str:
    if field.type_name:
        return field.type_name.lstrip(".").removeprefix("meshforge.v1.")
    return _TYPES.get(field.type, str(field.type))


def _label(field: descriptor_pb2.FieldDescriptorProto) -> str:
    if field.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED:
        return "repeated"
    if field.proto3_optional:
        return "optional"
    return ""


def _block(text: str | None) -> list[str]:
    return [text, ""] if text else []


def _enum(
    enum: descriptor_pb2.EnumDescriptorProto,
    path: tuple[int, ...],
    notes: Comments,
    prefix: str,
) -> list[str]:
    lines = [f"#### enum `{prefix}{enum.name}`", "", *_block(notes.get(path))]
    lines += ["| Value | Number | Description |", "|---|---|---|"]
    for i, value in enumerate(enum.value):
        note = notes.get((*path, _VALUE, i), "")
        lines.append(f"| `{value.name}` | {value.number} | {_cell(note)} |")
    return [*lines, ""]


def _message(
    msg: descriptor_pb2.DescriptorProto,
    path: tuple[int, ...],
    notes: Comments,
    prefix: str = "",
) -> list[str]:
    if msg.options.map_entry:
        return []
    name = f"{prefix}{msg.name}"
    lines = [f"#### message `{name}`", "", *_block(notes.get(path))]
    if msg.field:
        lines += [
            "| Field | Number | Type | Label | Description |",
            "|---|---|---|---|---|",
        ]
        for i, field in enumerate(msg.field):
            note = notes.get((*path, _FIELD, i), "")
            oneof = ""
            if field.HasField("oneof_index") and not field.proto3_optional:
                oneof = f" (oneof `{msg.oneof_decl[field.oneof_index].name}`)"
            lines.append(
                f"| `{field.name}` | {field.number} | `{_type(field)}` | "
                f"{_label(field) or '-'}{oneof} | {_cell(note)} |"
            )
    else:
        lines.append("No fields.")
    lines.append("")
    for i, enum in enumerate(msg.enum_type):
        lines += _enum(enum, (*path, _NESTED_ENUM, i), notes, f"{name}.")
    for i, nested in enumerate(msg.nested_type):
        lines += _message(nested, (*path, _NESTED, i), notes, f"{name}.")
    return lines


def _short(type_name: str) -> str:
    return type_name.lstrip(".").removeprefix("meshforge.v1.")


def render_file(
    file: descriptor_pb2.FileDescriptorProto, status: str | None = None
) -> list[str]:
    notes = comments(file)
    lines = [f"## `{file.name}`", ""]
    if status:
        lines += [f"> **Status:** {status}", ""]
    lines += _block(notes.get((_SYNTAX,)))
    lines += [f"Package `{file.package}`.", ""]
    for si, service in enumerate(file.service):
        spath = (_SERVICE, si)
        lines += [f"### service `{service.name}`", "", *_block(notes.get(spath))]
        lines += ["| RPC | Request | Response | Description |", "|---|---|---|---|"]
        for mi, method in enumerate(service.method):
            req = ("stream " if method.client_streaming else "") + _short(
                method.input_type
            )
            resp = ("stream " if method.server_streaming else "") + _short(
                method.output_type
            )
            note = notes.get((*spath, _METHOD, mi), "")
            lines.append(f"| `{method.name}` | `{req}` | `{resp}` | {_cell(note)} |")
        lines.append("")
    if file.message_type:
        lines += ["### Messages", ""]
        for i, msg in enumerate(file.message_type):
            lines += _message(msg, (_MESSAGE, i), notes)
    if file.enum_type:
        lines += ["### Enums", ""]
        for i, enum in enumerate(file.enum_type):
            lines += _enum(enum, (_ENUM, i), notes, "")
    return lines


def render(
    files: Sequence[descriptor_pb2.FileDescriptorProto],
    *,
    status: Mapping[str, str],
) -> str:
    """The Markdown reference of the project's own ``files`` (by name)."""
    own = sorted(
        (f for f in files if f.name.startswith("meshforge/")), key=lambda f: f.name
    )
    lines = [
        "# gRPC reference",
        "",
        "<!-- Generated by scripts/gen_api_docs.py from proto/. DO NOT EDIT. -->",
        "",
        "The agent <-> controller session, enrolment and (deferred) GPU contracts, from "
        "`proto/meshforge/v1/*.proto` compiled by protoc. The `.proto` files are the "
        "source of truth (ARCHITECTURE §8); the Python stubs are generated by "
        "`scripts/gen_protos.py`.",
        "",
        "| File | Services | Messages | Enums | Status |",
        "|---|---|---|---|---|",
    ]
    for f in own:
        services = ", ".join(f"`{s.name}`" for s in f.service) or "-"
        lines.append(
            f"| [`{f.name}`](#{_file_anchor(f.name)}) | {services} | "
            f"{len(f.message_type)} | {len(f.enum_type)} | "
            f"{_cell(status.get(f.name, 'current').split('.')[0])} |"
        )
    lines.append("")
    for f in own:
        lines += render_file(f, status.get(f.name))
    return "\n".join(lines).rstrip("\n") + "\n"


def _file_anchor(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum() or c in "-_ ").replace(
        " ", "-"
    )
