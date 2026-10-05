"""The job manifest ``compute.yaml`` (brief D46, ARCHITECTURE §6.1).

One Pydantic model read by the CLI (when it packs a project), the controller (when
a job is submitted, as JSON) and the agent (when it runs one) - which is why it
lives in ``common``, the only package all three may import.

V0.4 subset::

    name: image-classify                 # optional; default = the project folder
    entrypoint: python train.py --epochs 3   # or an argv list; NEVER a shell
    env:
      base: cuda-pytorch                 # python-cpu | cuda-pytorch
      python: "3.12"                     # recorded; the agent checks it fits the base
      requirements: requirements.txt     # a file inside the project
    resources:
      gpu: preferred                     # required | preferred | none
      vram_gb: 4
      ram_gb: 8
      cpus: 4
    outputs: [results/, "*.csv"]         # relative paths/globs
    inputs:                              # V0.5 named inputs (D92), at most 16
      - ref: imagenet-mini@3             # name@version or name@sha256:<tree id>
        mount: data/imagenet             # read-only folder inside the workdir
    limits:
      timeout: 2h                        # 90s, 30m, 2h, 1h30m or an integer of seconds
      network: default                   # default | none

Rules:

* ``entrypoint`` as a string is split with :func:`shlex.split` (POSIX rules) into an
  argv list; the list is executed directly. No shell is ever involved, so ``|``,
  ``;``, ``&&``, ``$VAR`` and globs reach the program as literal arguments.
* Unknown keys are refused with their YAML path (``resources.gpus: unknown key``).
  ``split:`` is *recognised and refused* with the version that brings it - a job
  that asked for splitting must never quietly run whole.
* ``inputs`` (V0.5 G5, D92): each ``ref`` names a registered dataset/model
  (``compute data push``) by ``name@<version>`` or ``name@sha256:<tree id>``;
  ``mount`` is a plain relative folder (no ``..``, no glob, not under
  ``.meshforge``) and no mount may contain another. The controller resolves the
  refs at submit and refuses a mount that overlaps a project file; the agent
  copies the input there **read-only**.
* No manifest, or a manifest without ``entrypoint``: the entrypoint is inferred -
  ``main.py``, else ``__main__.py``, else an ``invalid_input`` naming both.
* The file is read with ``yaml.safe_load`` semantics (no Python tags), at most
  :data:`MAX_MANIFEST_BYTES`, with YAML aliases and duplicate keys refused (an alias
  can multiply a small document into a huge one; a duplicate key silently drops one
  of the two values).

:meth:`Manifest.canonical_json` is the stable serialisation used for request
hashing (``POST /v1/jobs`` idempotency): sorted keys, no whitespace, every default
filled in, so two submissions that mean the same thing hash the same.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "INFERRED_ENTRYPOINTS",
    "INPUT_NAME_RE",
    "MANIFEST_FILE_NAME",
    "MAX_ARGV_ITEMS",
    "MAX_ARG_CHARS",
    "MAX_CPUS",
    "MAX_INPUTS",
    "MAX_MANIFEST_BYTES",
    "MAX_MOUNT_CHARS",
    "MAX_NAME_CHARS",
    "MAX_OUTPUTS",
    "MAX_OUTPUT_CHARS",
    "MAX_RAM_GB",
    "MAX_TIMEOUT_S",
    "MAX_VRAM_GB",
    "MIN_TIMEOUT_S",
    "EnvSpec",
    "InputSpec",
    "LimitsSpec",
    "Manifest",
    "ManifestError",
    "ResourcesSpec",
    "check_mount",
    "infer_entrypoint",
    "load_manifest",
    "mounts_overlap",
    "parse_duration",
    "parse_manifest_data",
    "parse_manifest_text",
    "sanitize_name",
]

MANIFEST_FILE_NAME: Final = "compute.yaml"
MAX_MANIFEST_BYTES: Final = 64 * 1024
"""A manifest is a few hundred bytes; 64 KiB is generous and still bounded."""
MAX_NAME_CHARS: Final = 64
MAX_ARGV_ITEMS: Final = 256
MAX_ARG_CHARS: Final = 4096
MAX_OUTPUTS: Final = 64
MAX_OUTPUT_CHARS: Final = 512
MAX_INPUTS: Final = 16
MAX_MOUNT_CHARS: Final = 256
INPUT_NAME_RE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""A named input's name (``compute data push <name>``): lower case, <= 64."""
_INPUT_REF_RE: Final = re.compile(
    r"(?P<name>[a-z0-9][a-z0-9._-]{0,63})@"
    r"(?:(?P<version>[1-9][0-9]{0,8})|(?P<tree>sha256:[0-9a-f]{64}))"
)
MAX_PATH_CHARS: Final = 512
MAX_VRAM_GB: Final = 1024.0
MAX_RAM_GB: Final = 4096.0
MAX_CPUS: Final = 1024.0
MIN_TIMEOUT_S: Final = 1
MAX_TIMEOUT_S: Final = 7 * 24 * 3600
"""One week: a limit, not a promise; the controller backstop is timeout + 30 s."""
DEFAULT_TIMEOUT_S: Final = 3600
INFERRED_ENTRYPOINTS: Final = ("main.py", "__main__.py")
"""Looked for, in this order, when the manifest gives no ``entrypoint``."""

_REJECTED_KEYS: Final[dict[str, str]] = {
    "split": "task splitting (Mode 3) arrives in V0.10; this job would otherwise "
    "run whole on one node",
}
_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PYTHON_RE: Final = re.compile(r"3\.\d{1,2}")
_DURATION_RE: Final = re.compile(
    r"(?:(?P<d>\d{1,4})d)?(?:(?P<h>\d{1,5})h)?(?:(?P<m>\d{1,6})m)?(?:(?P<s>\d{1,7})s)?"
)
_WINDOWS_DRIVE_RE: Final = re.compile(r"[A-Za-z]:")


class ManifestError(MeshForgeError, ValueError):
    """The manifest is missing something, malformed, or asks for an unsupported feature.

    ``problems`` lists ``(yaml_path, message)`` pairs; ``yaml_path`` is dotted with
    list indices in brackets (``outputs[2]``), or ``<root>``. The message never
    contains a value taken from the file (it may be long or unprintable).
    """

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        self.problems = problems
        summary = "; ".join(f"{path}: {msg}" for path, msg in problems[:10])
        more = f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""
        super().__init__(
            f"invalid {MANIFEST_FILE_NAME}: {summary}{more}",
            details={"problems": [{"path": p, "message": m} for p, m in problems]},
        )

    @classmethod
    def one(cls, path: str, message: str) -> ManifestError:
        return cls([(path, message)])


# ------------------------------------------------------------------ helpers


def parse_duration(value: object) -> int:
    """``"90s"``/``"30m"``/``"2h"``/``"1h30m"``/``"1d"`` or an int -> whole seconds.

    Raises ``ValueError`` (Pydantic turns it into a located problem).
    """
    if isinstance(value, bool):
        raise ValueError("a duration is a string like 30m or 2h, or whole seconds")  # noqa: TRY004
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        text = value.strip().lower()
        if text.isdigit() and len(text) <= 8:
            seconds = int(text)
        else:
            match = _DURATION_RE.fullmatch(text) if len(text) <= 32 else None
            if not text or match is None or not any(match.groupdict().values()):
                raise ValueError(
                    "a duration looks like 90s, 30m, 2h, 1h30m or 1d (or whole seconds)"
                )
            parts = {k: int(v) for k, v in match.groupdict().items() if v}
            seconds = (
                parts.get("d", 0) * 86400
                + parts.get("h", 0) * 3600
                + parts.get("m", 0) * 60
                + parts.get("s", 0)
            )
    else:
        raise ValueError("a duration is a string like 30m or 2h, or whole seconds")  # noqa: TRY004
    if not MIN_TIMEOUT_S <= seconds <= MAX_TIMEOUT_S:
        raise ValueError(
            f"must be between {MIN_TIMEOUT_S} s and {MAX_TIMEOUT_S} s (7 days)"
        )
    return seconds


def _check_relative_path(value: str, *, allow_glob: bool) -> str:
    """A POSIX path relative to the project root that cannot leave it."""
    if not value or value.strip() != value:
        raise ValueError("must be a non-empty path without surrounding spaces")
    if len(value) > MAX_PATH_CHARS:
        raise ValueError(f"at most {MAX_PATH_CHARS} characters")
    if any(not ch.isprintable() for ch in value):
        raise ValueError("must not contain control characters")
    if "\\" in value:
        raise ValueError("use '/' as the separator, not '\\'")
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        raise ValueError("must be relative to the project folder, not absolute")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise ValueError("must not contain '..'")
    if not allow_glob and any(ch in value for ch in "*?["):
        raise ValueError("must be a plain path, not a glob")
    return value


def _check_arg(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("every argv item must be a string")  # noqa: TRY004
    if len(value) > MAX_ARG_CHARS:
        raise ValueError(f"an argument is at most {MAX_ARG_CHARS} characters")
    if "\x00" in value:
        raise ValueError("an argument must not contain a NUL byte")
    return value


# ------------------------------------------------------------------- models


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EnvSpec(_Spec):
    """``env:`` - which base image and what to install on top of it."""

    base: Literal["python-cpu", "cuda-pytorch"] = "python-cpu"
    python: str | None = Field(
        default=None,
        description="Recorded; the executing agent checks it matches the base image.",
    )
    requirements: str | None = Field(
        default=None, description="A requirements file inside the project."
    )

    @field_validator("python", mode="before")
    @classmethod
    def _python(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, float):  # `python: 3.12` unquoted in YAML
            raise ValueError('quote the version: python: "3.12"')  # noqa: TRY004
        if not isinstance(value, str) or not _PYTHON_RE.fullmatch(value):
            raise ValueError('must look like "3.12"')
        return value

    @field_validator("requirements")
    @classmethod
    def _requirements(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_relative_path(value, allow_glob=False)


class ResourcesSpec(_Spec):
    """``resources:`` - what the job needs from the node that runs it."""

    gpu: Literal["required", "preferred", "none"] = "none"
    vram_gb: float | None = Field(default=None, gt=0, le=MAX_VRAM_GB)
    ram_gb: float | None = Field(default=None, gt=0, le=MAX_RAM_GB)
    cpus: float | None = Field(default=None, gt=0, le=MAX_CPUS)

    @model_validator(mode="after")
    def _vram_needs_gpu(self) -> Self:
        if self.vram_gb is not None and self.gpu == "none":
            raise ValueError("vram_gb is set but gpu is none; set gpu too")
        return self


class LimitsSpec(_Spec):
    """``limits:`` - enforced by the executing agent."""

    timeout: Annotated[int, Field(ge=MIN_TIMEOUT_S, le=MAX_TIMEOUT_S)] = (
        DEFAULT_TIMEOUT_S
    )
    """Seconds (the YAML may say ``2h``)."""
    network: Literal["default", "none"] = "default"

    @field_validator("timeout", mode="before")
    @classmethod
    def _duration(cls, value: object) -> int:
        return parse_duration(value)


class InputSpec(_Spec):
    """One ``inputs:`` entry (V0.5 D92)."""

    ref: str = Field(min_length=3, max_length=140)
    mount: str = Field(min_length=1, max_length=MAX_MOUNT_CHARS)

    @field_validator("ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        if not _INPUT_REF_RE.fullmatch(value):
            raise ValueError(
                "must be name@<version> or name@sha256:<64 hex> (name: lower-case "
                "letters, digits, '.', '_', '-')"
            )
        return value

    @field_validator("mount")
    @classmethod
    def _mount(cls, value: str) -> str:
        return check_mount(value)

    @property
    def name(self) -> str:
        return self.ref.split("@", 1)[0]

    @property
    def version(self) -> int | None:
        """The ``@<version>`` part (None for a ``@sha256:`` ref)."""
        match = _INPUT_REF_RE.fullmatch(self.ref)
        version = match.group("version") if match is not None else None
        return int(version) if version else None

    @property
    def tree_id(self) -> str | None:
        """The ``@sha256:<hex>`` part (None for a ``@<version>`` ref)."""
        match = _INPUT_REF_RE.fullmatch(self.ref)
        return match.group("tree") if match is not None else None


def check_mount(value: str) -> str:
    """A named input's mount: a plain relative folder inside the workdir, not
    under ``.meshforge``, at most :data:`MAX_MOUNT_CHARS` (``ValueError``)."""
    if len(value) > MAX_MOUNT_CHARS:
        raise ValueError(f"at most {MAX_MOUNT_CHARS} characters")
    _check_relative_path(value, allow_glob=False)
    parts = PurePosixPath(value).parts
    if not parts or any(p in (".", "") for p in value.split("/")):
        raise ValueError("must be a plain folder path (no '.', no empty parts)")
    if parts[0] == ".meshforge":
        raise ValueError("must not be under .meshforge (the agent's own folder)")
    return value


def mounts_overlap(a: str, b: str) -> bool:
    """One folder is the other or contains it (``data`` and ``data/x``)."""
    pa, pb = PurePosixPath(a).parts, PurePosixPath(b).parts
    shorter = min(len(pa), len(pb))
    return pa[:shorter] == pb[:shorter]


class Manifest(_Spec):
    """A validated V0.4 manifest (see module docstring)."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    entrypoint: tuple[str, ...] = Field(min_length=1, max_length=MAX_ARGV_ITEMS)
    """The argv to execute - already split, never passed to a shell."""
    env: EnvSpec = Field(default_factory=EnvSpec)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    outputs: tuple[str, ...] = Field(default=(), max_length=MAX_OUTPUTS)
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    inputs: tuple[InputSpec, ...] = Field(default=(), max_length=MAX_INPUTS)
    """Named inputs (V0.5 D92); omitted from the canonical form when empty, so a
    job without inputs hashes exactly as it did before V0.5 G5."""

    @model_validator(mode="before")
    @classmethod
    def _refuse_future_keys(cls, data: object) -> object:
        if isinstance(data, Mapping):
            for key, why in _REJECTED_KEYS.items():
                if key in data:
                    raise ValueError(f"{key}: not supported yet: {why}")
        return data

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError(
                "letters, digits, '.', '_' and '-' only, starting with a letter or digit"
            )
        return value

    @field_validator("entrypoint", mode="before")
    @classmethod
    def _argv(cls, value: object) -> object:
        if isinstance(value, str):
            if len(value) > MAX_ARG_CHARS * 4:
                raise ValueError("entrypoint string is too long")
            try:
                value = shlex.split(value, comments=False, posix=True)
            except ValueError:
                raise ValueError(
                    "cannot split the entrypoint (unbalanced quotes or a trailing "
                    "backslash)"
                ) from None
            if not value:
                raise ValueError("entrypoint is empty")
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_ARGV_ITEMS:
                raise ValueError(f"at most {MAX_ARGV_ITEMS} arguments")
            argv = tuple(_check_arg(item) for item in value)
            if argv and not argv[0]:
                raise ValueError("the program (first argument) must not be empty")
            return argv
        return value

    @field_validator("outputs", mode="before")
    @classmethod
    def _outputs(cls, value: object) -> object:
        if isinstance(value, str):
            return (value,)  # `outputs: results/` is one output
        return value

    @field_validator("outputs")
    @classmethod
    def _output_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) > MAX_OUTPUT_CHARS:
                raise ValueError(f"an output is at most {MAX_OUTPUT_CHARS} characters")
            _check_relative_path(item, allow_glob=True)
        return value

    @field_validator("inputs")
    @classmethod
    def _input_mounts(cls, value: tuple[InputSpec, ...]) -> tuple[InputSpec, ...]:
        for i, first in enumerate(value):
            for second in value[i + 1 :]:
                if mounts_overlap(first.mount, second.mount):
                    raise ValueError(
                        f"mounts {first.mount!r} and {second.mount!r} overlap "
                        "(one folder would hide the other)"
                    )
        return value

    def canonical_dict(self) -> dict[str, Any]:
        """Every field, defaults filled in, JSON types only (``inputs`` only
        when there are some - see the field)."""
        return self.model_dump(
            mode="json", exclude={"inputs"} if not self.inputs else None
        )

    def canonical_json(self) -> str:
        """Stable text form: sorted keys, no whitespace (request hashing, D50)."""
        return json.dumps(
            self.canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


# ------------------------------------------------------------------ loading


def _yaml_path(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "<root>"


def _problems(exc: ValidationError) -> list[tuple[str, str]]:
    problems: list[tuple[str, str]] = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = tuple(err.get("loc", ()))
        if err.get("type") == "extra_forbidden":
            problems.append((_yaml_path(loc), "unknown key"))
            continue
        message = str(err.get("msg", "invalid"))
        message = message.removeprefix("Value error, ")
        problems.append((_yaml_path(loc), message))
    return problems


def parse_manifest_data(
    data: object, *, default_name: str = "job", project_dir: Path | None = None
) -> Manifest:
    """Validate an already-parsed document (YAML mapping or the API's JSON object).

    ``default_name`` fills ``name`` when absent. When ``project_dir`` is given, a
    missing ``entrypoint`` is inferred from the files in it and ``env.requirements``
    must exist there; without it (the controller has no project tree) a missing
    ``entrypoint`` is an error.
    """
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise ManifestError.one("<root>", "the manifest must be a mapping of keys")
    for key, why in _REJECTED_KEYS.items():
        if key in data:
            raise ManifestError.one(key, f"not supported yet: {why}")
    document: dict[str, Any] = dict(data)
    if "name" not in document:
        document["name"] = sanitize_name(default_name)
    if "entrypoint" not in document:
        if project_dir is None:
            raise ManifestError.one("entrypoint", "missing")
        document["entrypoint"] = list(infer_entrypoint(project_dir))
    try:
        manifest = Manifest.model_validate(document)
    except ValidationError as exc:
        raise ManifestError(_problems(exc)) from None
    if project_dir is not None and manifest.env.requirements is not None:
        _check_in_tree(project_dir, manifest.env.requirements, "env.requirements")
    return manifest


def sanitize_name(text: str) -> str:
    """A folder name made into a valid ``name`` (``My Project!`` -> ``My-Project``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:MAX_NAME_CHARS]
    return cleaned or "job"


def infer_entrypoint(project_dir: Path) -> tuple[str, ...]:
    """``("python", "main.py")``, else ``("python", "__main__.py")``, else an error."""
    for candidate in INFERRED_ENTRYPOINTS:
        path = project_dir / candidate
        if path.is_file() and not path.is_symlink():
            return ("python", candidate)
    raise ManifestError.one(
        "entrypoint",
        "missing, and it cannot be inferred: looked for "
        + ", then ".join(INFERRED_ENTRYPOINTS)
        + f" in the project folder (or add entrypoint: to {MANIFEST_FILE_NAME})",
    )


def _check_in_tree(project_dir: Path, relative: str, yaml_path: str) -> None:
    root = project_dir.resolve()
    target = (project_dir / relative).resolve()
    if not target.is_relative_to(root):
        raise ManifestError.one(yaml_path, "points outside the project folder")
    if not target.is_file():
        raise ManifestError.one(yaml_path, "no such file in the project folder")


class _StrictSafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that refuses aliases and duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases (*name) are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:  # unhashable key; the base class reports it
                duplicate = False
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate key", key_node.start_mark
                )
            if not isinstance(key, (list, dict)):
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_manifest_text(
    text: str, *, default_name: str = "job", project_dir: Path | None = None
) -> Manifest:
    """Parse YAML text (already size-checked by the caller)."""
    try:
        data = yaml.load(text, Loader=_StrictSafeLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "?"
        problem = (exc.problem or exc.context or "invalid YAML").strip()
        raise ManifestError.one(
            "<root>", f"not valid YAML at {where}: {problem}"
        ) from None
    except (yaml.YAMLError, RecursionError):
        raise ManifestError.one("<root>", "not valid YAML") from None
    return parse_manifest_data(data, default_name=default_name, project_dir=project_dir)


def load_manifest(
    project_dir: Path, *, max_bytes: int = MAX_MANIFEST_BYTES
) -> Manifest:
    """Read ``<project_dir>/compute.yaml`` (or infer everything when it is absent)."""
    path = project_dir / MANIFEST_FILE_NAME
    default_name = sanitize_name(project_dir.resolve().name)
    if not path.exists() and not path.is_symlink():
        return parse_manifest_data(
            {}, default_name=default_name, project_dir=project_dir
        )
    if path.is_symlink() or not path.is_file():
        raise ManifestError.one(
            "<root>", f"{MANIFEST_FILE_NAME} must be a regular file"
        )
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError as exc:
        raise ManifestError.one(
            "<root>", f"cannot read {MANIFEST_FILE_NAME}: {exc.strerror or exc}"
        ) from None
    if len(raw) > max_bytes:
        raise ManifestError.one(
            "<root>", f"{MANIFEST_FILE_NAME} is larger than {max_bytes} bytes"
        )
    try:
        text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError:
        raise ManifestError.one("<root>", "not UTF-8 text") from None
    return parse_manifest_text(text, default_name=default_name, project_dir=project_dir)
