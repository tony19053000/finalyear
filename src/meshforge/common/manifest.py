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
      network: none                      # none (= default) | on (needs the owner's allow_network)
    priority: 0                          # V0.6 D106: -10 (last) .. 10 (first)
    policy: cost_model                   # V0.6 D107: placement policy (empty = fabric default)
    retry:                               # V0.6 D110 (retries run from V0.6 G3)
      max_attempts: 3                    # 1-10 attempts in total
      backoff_s: 5                       # 0-300 s before the 2nd attempt, doubling
      on_lost: true                      # false: never re-run after a lost node (D117)
    secrets:                             # V0.7 G6 (D144): YOUR secrets (compute secret set)
      - API_KEY                          # file /run/secrets/API_KEY (or $MESHFORGE_SECRETS_DIR)
      - {name: DB_PASS, as_env: true}    # ... and also the env var DB_PASS
    split:                               # V0.10 (D260): Mode 3 task splitting
      over: {glob: "images/*.jpg"}       # exactly one of glob | list | list_file | range | lines
      chunk: auto                        # or {items: 50}
      combine: concat                    # concat | merge-csv | merge-json | {script: [argv]}
      min_nodes: 2                       # 2..64; max_nodes: absent = every suitable node
      speculative: true                  # tail copies of slow chunks (D266)
      deterministic: true                # copies of one chunk must give equal bytes (D269)

Rules:

* ``entrypoint`` as a string is split with :func:`shlex.split` (POSIX rules) into an
  argv list; the list is executed directly. No shell is ever involved, so ``|``,
  ``;``, ``&&``, ``$VAR`` and globs reach the program as literal arguments.
* Unknown keys are refused with their YAML path (``resources.gpus: unknown key``).
* ``split`` (V0.10, D260; refused before V0.10 so a job that asked for splitting
  never quietly ran whole): ``over`` names the items - ``glob`` (files of the
  project tree or of a named input's mount; a bare string is a glob),
  ``list`` (at most :data:`MAX_SPLIT_LIST` strings, unique), ``list_file`` (one
  item per line), ``range`` (``{start, stop, step}`` integers) or ``lines``
  (the records of a text/CSV file, ``header: true`` keeps the first line out of
  the items and repeats it into every chunk). The controller resolves the items
  **once, at submit** (:mod:`meshforge.common.split_items`). Only a split job's
  ``entrypoint`` may use the placeholders ``{items_file}``, ``{chunk_index}`` and
  ``{out}``; ``combine.script`` may use ``{parts}`` and ``{out}``. ``split`` is
  left out of the canonical form when absent (older jobs keep their hash).
* ``inputs`` (V0.5 G5, D92): each ``ref`` names a registered dataset/model
  (``compute data push``) by ``name@<version>`` or ``name@sha256:<tree id>``;
  ``mount`` is a plain relative folder (no ``..``, no glob, not under
  ``.meshforge``) and no mount may contain another. The controller resolves the
  refs at submit and refuses a mount that overlaps a project file; the agent
  copies the input there **read-only**.
* ``priority``, ``policy`` and ``retry`` (V0.6 D106/D107/D110) are scheduling
  requests, not part of what runs: like ``inputs`` they are left out of the
  canonical form while they hold their defaults, so a job that does not use them
  hashes exactly as before V0.6. ``policy`` names one of :data:`POLICIES`; the
  controller's ``[controller.planner] default_policy`` applies when it is absent
  and the submit request's ``placement.policy`` overrides it.
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
    SerializerFunctionWrapHandler,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.secret_names import (
    MAX_SECRET_NAME_CHARS,
    SecretNameError,
    check_env_name,
    normalise_secret_name,
)

__all__ = [
    "DEFAULT_BACKOFF_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_S",
    "INFERRED_ENTRYPOINTS",
    "INPUT_NAME_RE",
    "MANIFEST_FILE_NAME",
    "MAX_ARGV_ITEMS",
    "MAX_ARG_CHARS",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_S",
    "MAX_CPUS",
    "MAX_INPUTS",
    "MAX_MANIFEST_BYTES",
    "MAX_MOUNT_CHARS",
    "MAX_NAME_CHARS",
    "MAX_OUTPUTS",
    "MAX_OUTPUT_CHARS",
    "MAX_PRIORITY",
    "MAX_RAM_GB",
    "MAX_SECRETS",
    "MAX_SPLIT_CHUNKS",
    "MAX_SPLIT_ITEMS",
    "MAX_SPLIT_ITEM_CHARS",
    "MAX_SPLIT_LIST",
    "MAX_SPLIT_NODES",
    "MAX_TIMEOUT_S",
    "MAX_VRAM_GB",
    "MIN_PRIORITY",
    "MIN_TIMEOUT_S",
    "POLICIES",
    "SPLIT_COMBINE_BUILTINS",
    "SPLIT_COMBINE_PLACEHOLDERS",
    "SPLIT_ENTRYPOINT_PLACEHOLDERS",
    "ChunkSpec",
    "CombineSpec",
    "EnvSpec",
    "InputSpec",
    "LimitsSpec",
    "Manifest",
    "ManifestError",
    "OverKind",
    "PolicyName",
    "ResourcesSpec",
    "RetrySpec",
    "SecretSpec",
    "SplitOver",
    "SplitRange",
    "SplitSpec",
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
MAX_SECRETS: Final = 32
"""Most secrets one job may name (V0.7 G6)."""
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
MIN_PRIORITY: Final = -10
MAX_PRIORITY: Final = 10
DEFAULT_MAX_ATTEMPTS: Final = 3
MAX_ATTEMPTS: Final = 10
DEFAULT_BACKOFF_S: Final = 5
MAX_BACKOFF_S: Final = 300
type PolicyName = Literal[
    "random",
    "round_robin",
    "least_loaded",
    "strongest_suitable",
    "resource_score",
    "cache_affinity",
    "cost_model",
]
POLICIES: Final[tuple[str, ...]] = (
    "random",
    "round_robin",
    "least_loaded",
    "strongest_suitable",
    "resource_score",
    "cache_affinity",
    "cost_model",
)
"""Placement policies (V0.6 D107), the wire spelling everywhere: manifest,
``POST /v1/jobs`` ``placement.policy``, ``compute run --policy``, the
controller's ``default_policy`` and ``plan_decision.policy``."""
INFERRED_ENTRYPOINTS: Final = ("main.py", "__main__.py")
"""Looked for, in this order, when the manifest gives no ``entrypoint``."""

MAX_SPLIT_ITEMS: Final = 100_000
"""Hard ceiling on the items of one split job (D260); the controller's
``[controller.split] max_items`` may only lower it."""
MAX_SPLIT_CHUNKS: Final = 10_000
"""Hard ceiling on the chunks of one split job (``[controller.split] max_chunks``)."""
MAX_SPLIT_LIST: Final = 10_000
"""Most strings an inline ``split.over.list`` may hold."""
MAX_SPLIT_ITEM_CHARS: Final = 1024
"""Longest ``list`` / ``list_file`` item (characters)."""
MAX_SPLIT_NODES: Final = 64
MAX_SAFE_INT: Final = 2**53 - 1
"""``range`` bounds: integers a JSON reader in any language reads exactly."""
SPLIT_ENTRYPOINT_PLACEHOLDERS: Final = ("{items_file}", "{chunk_index}", "{out}")
"""Substituted per chunk in a split job's ``entrypoint`` (D260)."""
SPLIT_COMBINE_PLACEHOLDERS: Final = ("{parts}", "{out}")
"""Substituted in ``split.combine.script`` (D268)."""
SPLIT_COMBINE_BUILTINS: Final = ("concat", "merge-csv", "merge-json")
_ALL_PLACEHOLDERS: Final = ("{items_file}", "{chunk_index}", "{out}", "{parts}")
_REJECTED_KEYS: Final[dict[str, str]] = {}
"""Keys recognised and refused with the version that brings them (empty since
V0.10 accepted ``split``; kept for the next one)."""
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


def _to_argv(value: object, what: str) -> object:
    """A string is split with POSIX shell rules (never run by a shell); a list
    is checked item by item. Anything else is left for the type check."""
    if isinstance(value, str):
        if len(value) > MAX_ARG_CHARS * 4:
            raise ValueError(f"{what} string is too long")
        try:
            value = shlex.split(value, comments=False, posix=True)
        except ValueError:
            raise ValueError(
                f"cannot split the {what} (unbalanced quotes or a trailing backslash)"
            ) from None
        if not value:
            raise ValueError(f"{what} is empty")
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ARGV_ITEMS:
            raise ValueError(f"at most {MAX_ARGV_ITEMS} arguments")
        argv = tuple(_check_arg(item) for item in value)
        if argv and not argv[0]:
            raise ValueError("the program (first argument) must not be empty")
        return argv
    return value


def _refuse_placeholders(
    argv: tuple[str, ...], placeholders: tuple[str, ...], where: str
) -> None:
    for item in argv:
        for name in placeholders:
            if name in item:
                raise ValueError(
                    f"the placeholder {name} is only substituted in {where}"
                )


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
    network: Literal["default", "none", "on"] = "default"
    """V0.7 G5 (D143): ``default`` = ``none`` - a job gets no network unless it
    asks ``on`` **and** the node's owner allows it (``[agent.policy]
    allow_network``; else the planner/agent refuse ``policy_network``)."""

    @property
    def wants_network(self) -> bool:
        return self.network == "on"

    @field_validator("timeout", mode="before")
    @classmethod
    def _duration(cls, value: object) -> int:
        return parse_duration(value)


class RetrySpec(_Spec):
    """``retry:`` (V0.6 D110/D117) - how often a failed attempt is tried again."""

    max_attempts: int = Field(
        default=DEFAULT_MAX_ATTEMPTS, ge=1, le=MAX_ATTEMPTS, strict=True
    )
    """Attempts in total, the first one included (1 = never retried)."""
    backoff_s: int = Field(
        default=DEFAULT_BACKOFF_S, ge=0, le=MAX_BACKOFF_S, strict=True
    )
    """Wait before the second attempt; doubled for every later one, capped at
    :data:`MAX_BACKOFF_S`."""
    on_lost: bool = Field(default=True, strict=True)
    """False: an attempt lost with its node is not re-run (a non-idempotent job
    that must never run twice, D117)."""


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


class SecretSpec(_Spec):
    """One ``secrets:`` entry (V0.7 G6, D144): the job owner's secret ``name``
    (:mod:`meshforge.common.secret_names` rules), delivered as the file
    ``/run/secrets/NAME`` (Docker) or ``$MESHFORGE_SECRETS_DIR/NAME``; also as
    the environment variable ``NAME`` when ``as_env`` (never by default)."""

    name: str = Field(min_length=1, max_length=MAX_SECRET_NAME_CHARS + 8)
    as_env: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _bare_name(cls, data: object) -> object:
        return {"name": data} if isinstance(data, str) else data

    @field_validator("name")
    @classmethod
    def _canonical(cls, value: str) -> str:
        try:
            return normalise_secret_name(value)
        except SecretNameError as exc:
            raise ValueError(exc.message) from None

    @model_validator(mode="after")
    def _env_name(self) -> Self:
        if self.as_env:
            try:
                check_env_name(self.name)
            except SecretNameError as exc:
                raise ValueError(exc.message) from None
        return self


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


class SplitRange(_Spec):
    """``split.over.range``: the integers ``start, start+step, ...`` before
    ``stop`` (Python ``range`` rules; ``step`` may be negative, never 0)."""

    start: int = Field(default=0, ge=-MAX_SAFE_INT, le=MAX_SAFE_INT, strict=True)
    stop: int = Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT, strict=True)
    step: int = Field(default=1, ge=-MAX_SAFE_INT, le=MAX_SAFE_INT, strict=True)

    @field_validator("step")
    @classmethod
    def _nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("must not be 0")
        return value

    @property
    def count(self) -> int:
        """How many items, computed without building them."""
        return len(range(self.start, self.stop, self.step))


type OverKind = Literal["glob", "list", "list_file", "range", "lines"]
_OVER_KINDS: Final[tuple[OverKind, ...]] = (
    "glob",
    "list",
    "list_file",
    "range",
    "lines",
)


def _check_split_path(value: str, *, allow_glob: bool) -> str:
    _check_relative_path(value, allow_glob=allow_glob)
    if any(part in ("", ".") for part in value.split("/")):
        raise ValueError(
            "name files with a plain path (no empty or '.' parts, no trailing '/'; "
            "a whole folder is folder/**)"
        )
    return value


def _check_item_text(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("an item must not be empty or blank")
    if len(value) > MAX_SPLIT_ITEM_CHARS:
        raise ValueError(f"an item is at most {MAX_SPLIT_ITEM_CHARS} characters")
    if "\x00" in value:
        raise ValueError("an item must not contain a NUL byte")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("an item must be valid UTF-8 text") from None
    return value


class SplitOver(_Spec):
    """``split.over``: exactly one source of items (D260). A bare string is a
    ``glob``. Items from ``glob`` are the matching **files** (relative POSIX
    paths, sorted by their UTF-8 bytes); ``list`` and ``list_file`` keep the
    order they are written in and must not repeat an item."""

    glob: str | None = Field(default=None, min_length=1, max_length=MAX_PATH_CHARS)
    list: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_SPLIT_LIST
    )
    list_file: str | None = Field(default=None, min_length=1, max_length=MAX_PATH_CHARS)
    range: SplitRange | None = None
    lines: str | None = Field(default=None, min_length=1, max_length=MAX_PATH_CHARS)
    header: bool = Field(default=False, strict=True)
    """``lines`` only: the first line is a header, not an item."""

    @model_validator(mode="before")
    @classmethod
    def _bare_glob(cls, data: object) -> object:
        return {"glob": data} if isinstance(data, str) else data

    @field_validator("glob")
    @classmethod
    def _glob(cls, value: str | None) -> str | None:
        return None if value is None else _check_split_path(value, allow_glob=True)

    @field_validator("list_file", "lines")
    @classmethod
    def _file(cls, value: str | None) -> str | None:
        return None if value is None else _check_split_path(value, allow_glob=False)

    @field_validator("list")
    @classmethod
    def _items(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        seen: set[str] = set()
        for index, item in enumerate(value):
            _check_item_text(item)
            if item in seen:
                raise ValueError(f"item [{index}] repeats an earlier item")
            seen.add(item)
        return value

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [kind for kind in _OVER_KINDS if getattr(self, kind) is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of "
                + ", ".join(_OVER_KINDS)
                + (f" (got {', '.join(given)})" if given else "")
            )
        if self.header and self.kind != "lines":
            raise ValueError("header applies to lines only")
        return self

    @property
    def kind(self) -> OverKind:
        for kind in _OVER_KINDS:
            if getattr(self, kind) is not None:
                return kind
        raise AssertionError("validated SplitOver has a source")  # pragma: no cover

    @model_serializer(mode="wrap")
    def _compact(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Only the given source (and ``header`` for ``lines``): the canonical
        form names what was asked, nothing else."""
        data: dict[str, Any] = handler(self)
        out: dict[str, Any] = {
            k: v for k, v in data.items() if k in _OVER_KINDS and v is not None
        }
        if self.kind == "lines":
            out["header"] = self.header
        return out


class ChunkSpec(_Spec):
    """``split.chunk``: ``auto`` (the planner sizes chunks, D265) or
    ``{items: N}``. Serialised back as ``"auto"`` / ``{"items": N}``."""

    items: int | None = Field(default=None, ge=1, le=MAX_SPLIT_ITEMS, strict=True)
    """None = auto."""

    @model_validator(mode="before")
    @classmethod
    def _word(cls, data: object) -> object:
        if isinstance(data, str):
            if data != "auto":
                raise ValueError("must be auto or {items: N}")
            return {}
        if isinstance(data, Mapping) and "items" in data and data["items"] is None:
            raise ValueError("must be auto or {items: N}")
        return data

    @property
    def auto(self) -> bool:
        return self.items is None

    @model_serializer(mode="plain")
    def _canonical(self) -> str | dict[str, int]:
        return "auto" if self.items is None else {"items": self.items}


class CombineSpec(_Spec):
    """``split.combine``: a built-in (``concat``, ``merge-csv``, ``merge-json``)
    written as a word, or ``{script: [argv]}`` (a string is split like the
    entrypoint) run with ``{parts}`` and ``{out}`` substituted (D268)."""

    builtin: Literal["concat", "merge-csv", "merge-json"] | None = None
    script: tuple[str, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_ARGV_ITEMS
    )

    @model_validator(mode="before")
    @classmethod
    def _word(cls, data: object) -> object:
        if isinstance(data, str):
            if data not in SPLIT_COMBINE_BUILTINS:
                raise ValueError(
                    "must be "
                    + ", ".join(SPLIT_COMBINE_BUILTINS)
                    + " or {script: [argv]}"
                )
            return {"builtin": data}
        if isinstance(data, Mapping) and "builtin" in data:
            raise ValueError("write a built-in combine as a word: combine: concat")
        return data

    @field_validator("script", mode="before")
    @classmethod
    def _argv(cls, value: object) -> object:
        return None if value is None else _to_argv(value, "script")

    @field_validator("script")
    @classmethod
    def _placeholders(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None:
            _refuse_placeholders(
                value,
                ("{items_file}", "{chunk_index}"),
                "the entrypoint, not split.combine.script",
            )
        return value

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.builtin is None) == (self.script is None):
            raise ValueError("give a built-in word or {script: [argv]}")
        return self

    @model_serializer(mode="plain")
    def _canonical(self) -> str | dict[str, list[str]]:
        if self.builtin is not None:
            return self.builtin
        return {"script": list(self.script or ())}


class SplitSpec(_Spec):
    """``split:`` (V0.10, D260) - see the module docstring."""

    over: SplitOver
    chunk: ChunkSpec = Field(default_factory=ChunkSpec)
    combine: CombineSpec = Field(
        default_factory=lambda: CombineSpec.model_validate("concat")
    )
    min_nodes: int = Field(default=2, ge=2, le=MAX_SPLIT_NODES, strict=True)
    """The smallest node set worth splitting over (fewer = run it unsplit)."""
    max_nodes: int | None = Field(default=None, ge=2, le=MAX_SPLIT_NODES, strict=True)
    """None = every suitable node."""
    speculative: bool = Field(default=True, strict=True)
    deterministic: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def _node_bounds(self) -> Self:
        if self.max_nodes is not None and self.max_nodes < self.min_nodes:
            raise ValueError("max_nodes must be at least min_nodes")
        return self


class Manifest(_Spec):
    """A validated V0.4 manifest (see module docstring)."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    split: SplitSpec | None = None
    """V0.10 (D260): Mode 3 task splitting; None = the job runs whole. Declared
    before ``entrypoint`` on purpose: the entrypoint's placeholder check reads
    it. Omitted from the canonical form when None."""
    entrypoint: tuple[str, ...] = Field(min_length=1, max_length=MAX_ARGV_ITEMS)
    """The argv to execute - already split, never passed to a shell."""
    env: EnvSpec = Field(default_factory=EnvSpec)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    outputs: tuple[str, ...] = Field(default=(), max_length=MAX_OUTPUTS)
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    inputs: tuple[InputSpec, ...] = Field(default=(), max_length=MAX_INPUTS)
    """Named inputs (V0.5 D92); omitted from the canonical form when empty, so a
    job without inputs hashes exactly as it did before V0.5 G5."""
    priority: int = Field(default=0, ge=MIN_PRIORITY, le=MAX_PRIORITY, strict=True)
    """Queue priority (V0.6 D106); higher runs first, waiting ages it upwards."""
    policy: PolicyName | None = None
    """Placement policy for this job (V0.6 D107); None = the fabric default."""
    retry: RetrySpec = Field(default_factory=RetrySpec)
    """V0.6 D110."""
    secrets: tuple[SecretSpec, ...] = Field(default=(), max_length=MAX_SECRETS)
    """V0.7 G6 (D144): the job owner's secrets this job reads - names only, the
    values never travel with the manifest; omitted from the canonical form
    when empty."""

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
        return _to_argv(value, "entrypoint")

    @field_validator("entrypoint")
    @classmethod
    def _placeholders(
        cls, value: tuple[str, ...], info: ValidationInfo
    ) -> tuple[str, ...]:
        # ``split`` is declared before ``entrypoint``, so it is in ``info.data``
        # when it validated (absent = it failed and reports its own problem).
        if "split" not in info.data:
            return value
        if info.data["split"] is None:
            _refuse_placeholders(
                value, _ALL_PLACEHOLDERS, "a split: job (add split:, or remove it)"
            )
        else:
            _refuse_placeholders(
                value, ("{parts}",), "split.combine.script, not the entrypoint"
            )
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

    @field_validator("secrets")
    @classmethod
    def _unique_secrets(cls, value: tuple[SecretSpec, ...]) -> tuple[SecretSpec, ...]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("each secret may be named once")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        """Every field, defaults filled in, JSON types only - except the
        V0.5-V0.10 additions (``inputs``, ``priority``, ``policy``, ``retry``,
        ``secrets``, ``split``),
        which appear only when they differ from their defaults (see the module
        docstring: an older manifest keeps its request hash)."""
        exclude: set[str] = set()
        if not self.inputs:
            exclude.add("inputs")
        if self.priority == 0:
            exclude.add("priority")
        if self.policy is None:
            exclude.add("policy")
        if self.retry == RetrySpec():
            exclude.add("retry")
        if not self.secrets:
            exclude.add("secrets")
        if self.split is None:
            exclude.add("split")
        return self.model_dump(mode="json", exclude=exclude or None)

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
