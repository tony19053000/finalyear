"""Benchmark conditions: the typed form of ``name[:arg]`` (V0.9 G4, roadmap 0.9.5, D217/D241).

A benchmark *condition* is the state a run is measured under - background CPU
load, a busy GPU, a cold cache, a queue in front of it, a shaped network, an
unavailable origin. A campaign lists conditions as a **sweep axis**: every run
has exactly **one** condition (``none`` = the baseline), never a stack.

Spelling (case-sensitive, no spaces)::

    none
    cpu_load:N            N busy cores (1..256; the node's thread count bounds it)
    gpu_load:P            the GPU busy P % of the time (10..100)
    queue:K               K filler jobs queued ahead of the run on its node (1..32)
    cache:cold|warm       the run's input blobs evicted first / a warm-up run first
    netem:delay=50ms,loss=1%,rate=10mbit
                          egress shaping toward the controller (Linux, opt-in);
                          any non-empty subset, delay <= 500 ms, loss <= 20 %,
                          rate >= 1 mbit
    unavailable:30s       the run's origin is paused for T s (1..300) before a
                          mode1 run is submitted

:func:`parse_condition` turns the text into one of the frozen models below;
:meth:`Condition.canonical` is its normal form (``netem:loss=1.0%,delay=50ms``
-> ``netem:delay=50ms,loss=1%``), so equal conditions digest equal in a
``group_id``. Everything here is pure and lives in ``common`` because the
agent (which applies most conditions) and the campaign expansion
(:mod:`meshforge.bench.conditions`) must read one grammar without importing
each other.

**Who applies what.** ``cpu_load``, ``gpu_load``, ``netem`` and ``cache`` are
applied, verified and reverted by the agent of the run's node
(:data:`AGENT_KINDS`), which advertises what it can do in one capability tag,
``bench-conditions:<kind>,<kind>...`` (:func:`capability_tag` /
:func:`advertised_kinds`). ``queue`` and ``unavailable`` are the controller's
(filler jobs; a paused origin). ``cache:warm`` is both: the controller submits
the warm-up run, the agent verifies every input was a cache hit.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AGENT_KINDS",
    "CAPABILITY_PREFIX",
    "CONDITION_MAX_CHARS",
    "MAX_CPU_LOAD",
    "MAX_NETEM_DELAY_MS",
    "MAX_NETEM_LOSS_PCT",
    "MAX_QUEUE",
    "MAX_UNAVAILABLE_S",
    "MIN_GPU_LOAD",
    "MIN_NETEM_RATE_MBIT",
    "NONE",
    "CacheCondition",
    "Condition",
    "ConditionError",
    "ConditionKind",
    "CpuLoad",
    "GpuLoad",
    "Netem",
    "NoCondition",
    "QueueCondition",
    "Unavailable",
    "advertised_kinds",
    "capability_tag",
    "parse_condition",
]

MAX_CPU_LOAD: Final = 256
MIN_GPU_LOAD: Final = 10
MAX_QUEUE: Final = 32
MAX_NETEM_DELAY_MS: Final = 500.0
MAX_NETEM_LOSS_PCT: Final = 20.0
MIN_NETEM_RATE_MBIT: Final = 1.0
MAX_NETEM_RATE_MBIT: Final = 100_000.0
MAX_UNAVAILABLE_S: Final = 300
CONDITION_MAX_CHARS: Final = 64
"""A condition string is short (it fits the run record's short text column)."""
CAPABILITY_PREFIX: Final = "bench-conditions:"
"""The agent's capability tag: ``bench-conditions:cache,cpu_load,...``."""

_INT_RE: Final = re.compile(r"[0-9]{1,6}")
_DURATION_RE: Final = re.compile(r"(?P<n>[0-9]{1,4})(?P<unit>s|m)?")
_DELAY_RE: Final = re.compile(r"(?P<n>[0-9]{1,6}(?:\.[0-9]{1,3})?)(?P<unit>ms|s)")
_LOSS_RE: Final = re.compile(r"(?P<n>[0-9]{1,3}(?:\.[0-9]{1,3})?)%")
_RATE_RE: Final = re.compile(
    r"(?P<n>[0-9]{1,6}(?:\.[0-9]{1,3})?)(?P<unit>kbit|mbit|gbit)"
)


class ConditionError(ValueError):
    """A condition string is not one this grammar accepts (the message says why)."""


class ConditionKind(StrEnum):
    NONE = "none"
    CPU_LOAD = "cpu_load"
    GPU_LOAD = "gpu_load"
    QUEUE = "queue"
    CACHE = "cache"
    NETEM = "netem"
    UNAVAILABLE = "unavailable"


AGENT_KINDS: Final = frozenset(
    {
        ConditionKind.CPU_LOAD,
        ConditionKind.GPU_LOAD,
        ConditionKind.NETEM,
        ConditionKind.CACHE,
    }
)
"""Applied / verified on the run's node; the agent must advertise the kind."""


def _num(value: float) -> str:
    """A number in its shortest exact form (``50.0`` -> ``50``, ``0.5`` -> ``0.5``)."""
    return f"{value:g}"


class Condition(BaseModel):
    """One condition (a frozen model); :meth:`canonical` is its normal form."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClassVar[ConditionKind]

    def canonical(self) -> str:  # pragma: no cover - every subclass overrides
        raise NotImplementedError

    @property
    def needs_agent(self) -> bool:
        """Applied or verified by the agent of the run's node."""
        return self.kind in AGENT_KINDS

    def as_dict(self) -> dict[str, Any]:
        """JSON form (the kind plus its arguments)."""
        return {"kind": self.kind.value, **self.model_dump(mode="json")}


class NoCondition(Condition):
    kind: ClassVar[ConditionKind] = ConditionKind.NONE

    def canonical(self) -> str:
        return "none"


class CpuLoad(Condition):
    """``cores`` busy-loop processes at the lowest OS priority."""

    kind: ClassVar[ConditionKind] = ConditionKind.CPU_LOAD
    cores: Annotated[int, Field(ge=1, le=MAX_CPU_LOAD, strict=True)]

    def canonical(self) -> str:
        return f"cpu_load:{self.cores}"


class GpuLoad(Condition):
    """A CUDA kernel loop busy ``percent`` % of the time."""

    kind: ClassVar[ConditionKind] = ConditionKind.GPU_LOAD
    percent: Annotated[int, Field(ge=MIN_GPU_LOAD, le=100, strict=True)]

    def canonical(self) -> str:
        return f"gpu_load:{self.percent}"


class QueueCondition(Condition):
    """``jobs`` filler benchmark jobs pinned to the run's node, queued first."""

    kind: ClassVar[ConditionKind] = ConditionKind.QUEUE
    jobs: Annotated[int, Field(ge=1, le=MAX_QUEUE, strict=True)]

    def canonical(self) -> str:
        return f"queue:{self.jobs}"


class CacheCondition(Condition):
    kind: ClassVar[ConditionKind] = ConditionKind.CACHE
    mode: Literal["cold", "warm"]

    def canonical(self) -> str:
        return f"cache:{self.mode}"


class Netem(Condition):
    """Egress shaping with ``tc netem`` (any non-empty subset of the three)."""

    kind: ClassVar[ConditionKind] = ConditionKind.NETEM
    delay_ms: Annotated[float, Field(ge=0, le=MAX_NETEM_DELAY_MS, allow_inf_nan=False)]
    loss_pct: Annotated[float, Field(ge=0, le=MAX_NETEM_LOSS_PCT, allow_inf_nan=False)]
    rate_mbit: (
        Annotated[
            float,
            Field(ge=MIN_NETEM_RATE_MBIT, le=MAX_NETEM_RATE_MBIT, allow_inf_nan=False),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _something(self) -> Netem:
        if self.delay_ms == 0 and self.loss_pct == 0 and self.rate_mbit is None:
            raise ValueError("netem needs at least one of delay, loss, rate")
        return self

    def canonical(self) -> str:
        parts: list[str] = []
        if self.delay_ms:
            parts.append(f"delay={_num(self.delay_ms)}ms")
        if self.loss_pct:
            parts.append(f"loss={_num(self.loss_pct)}%")
        if self.rate_mbit is not None:
            parts.append(f"rate={_num(self.rate_mbit)}mbit")
        return "netem:" + ",".join(parts)

    def tc_args(self) -> list[str]:
        """The ``netem`` options for ``tc qdisc replace ... root netem <args>``
        - built from validated numbers only, never from the user's text."""
        args: list[str] = []
        if self.delay_ms:
            args += ["delay", f"{_num(self.delay_ms)}ms"]
        if self.loss_pct:
            args += ["loss", f"{_num(self.loss_pct)}%"]
        if self.rate_mbit is not None:
            args += ["rate", f"{_num(self.rate_mbit)}mbit"]
        return args


class Unavailable(Condition):
    """The run's origin node is paused for ``seconds`` before a mode1 run."""

    kind: ClassVar[ConditionKind] = ConditionKind.UNAVAILABLE
    seconds: Annotated[int, Field(ge=1, le=MAX_UNAVAILABLE_S, strict=True)]

    def canonical(self) -> str:
        return f"unavailable:{self.seconds}s"


NONE: Final = NoCondition()


def _int_arg(name: str, arg: str) -> int:
    if not _INT_RE.fullmatch(arg):
        raise ConditionError(f"{name}: {arg!r} is not a whole number")
    return int(arg)


def _cache(arg: str) -> CacheCondition:
    if arg == "cold":
        return CacheCondition(mode="cold")
    if arg == "warm":
        return CacheCondition(mode="warm")
    raise ConditionError("cache: the argument is cold or warm")


def _seconds(arg: str) -> int:
    match = _DURATION_RE.fullmatch(arg)
    if match is None:
        raise ConditionError(f"unavailable: {arg!r} is not <n>s or <n>m")
    return int(match["n"]) * (60 if match["unit"] == "m" else 1)


def _netem(arg: str) -> Netem:
    values: dict[str, float] = {}
    for part in arg.split(","):
        key, sep, value = part.partition("=")
        if not sep or key not in ("delay", "loss", "rate"):
            raise ConditionError(
                f"netem: {part!r} is not delay=<ms>, loss=<pct>% or rate=<n>mbit"
            )
        if key in values:
            raise ConditionError(f"netem: {key} is given twice")
        if key == "delay":
            match = _DELAY_RE.fullmatch(value)
            if match is None:
                raise ConditionError(f"netem: delay {value!r} is not <n>ms or <n>s")
            scale = 1000.0 if match["unit"] == "s" else 1.0
            values[key] = float(match["n"]) * scale
        elif key == "loss":
            match = _LOSS_RE.fullmatch(value)
            if match is None:
                raise ConditionError(f"netem: loss {value!r} is not <n>%")
            values[key] = float(match["n"])
        else:
            match = _RATE_RE.fullmatch(value)
            if match is None:
                raise ConditionError(
                    f"netem: rate {value!r} is not <n>kbit, <n>mbit or <n>gbit"
                )
            scale = {"kbit": 0.001, "mbit": 1.0, "gbit": 1000.0}[match["unit"]]
            values[key] = round(float(match["n"]) * scale, 6)
    if any(not math.isfinite(v) for v in values.values()):  # pragma: no cover
        raise ConditionError("netem: values must be finite")
    try:
        return Netem(
            delay_ms=values.get("delay", 0.0),
            loss_pct=values.get("loss", 0.0),
            rate_mbit=values.get("rate"),
        )
    except ValueError as exc:
        raise ConditionError(f"netem: {_first_error(exc)}") from None


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors(include_url=False)[0]
        where = ".".join(str(p) for p in first["loc"])
        message = str(first["msg"]).removeprefix("Value error, ")
        return f"{where} {message}" if where else message
    return str(exc)  # pragma: no cover


def parse_condition(text: object) -> Condition:
    """The condition ``text`` names. Raises :class:`ConditionError`."""
    if not isinstance(text, str):
        raise ConditionError("a condition is a string such as cpu_load:2")
    if not text or len(text) > CONDITION_MAX_CHARS:
        raise ConditionError(f"a condition is 1-{CONDITION_MAX_CHARS} characters")
    name, sep, arg = text.partition(":")
    try:
        kind = ConditionKind(name)
    except ValueError:
        raise ConditionError(f"unknown condition {name!r}") from None
    if kind is ConditionKind.NONE:
        if sep:
            raise ConditionError("none takes no argument")
        return NONE
    if not sep or not arg:
        raise ConditionError(f"{name} needs an argument (e.g. {_EXAMPLES[kind]})")
    try:
        if kind is ConditionKind.CPU_LOAD:
            return CpuLoad(cores=_int_arg(name, arg))
        if kind is ConditionKind.GPU_LOAD:
            return GpuLoad(percent=_int_arg(name, arg.removesuffix("%")))
        if kind is ConditionKind.QUEUE:
            return QueueCondition(jobs=_int_arg(name, arg))
        if kind is ConditionKind.CACHE:
            return _cache(arg)
        if kind is ConditionKind.NETEM:
            return _netem(arg)
        return Unavailable(seconds=_seconds(arg))
    except ConditionError:
        raise
    except ValueError as exc:
        raise ConditionError(f"{name}: {_first_error(exc)}") from None


_EXAMPLES: Final = {
    ConditionKind.CPU_LOAD: "cpu_load:2",
    ConditionKind.GPU_LOAD: "gpu_load:50",
    ConditionKind.QUEUE: "queue:4",
    ConditionKind.CACHE: "cache:cold",
    ConditionKind.NETEM: "netem:delay=50ms,loss=1%",
    ConditionKind.UNAVAILABLE: "unavailable:30s",
}


def capability_tag(kinds: Iterable[ConditionKind | str]) -> str | None:
    """The agent's capability tag for ``kinds`` (agent kinds only, sorted);
    None when there is none to advertise."""
    names = sorted({str(ConditionKind(k)) for k in kinds} & set(AGENT_KINDS))
    return CAPABILITY_PREFIX + ",".join(names) if names else None


def advertised_kinds(capabilities: Iterable[str]) -> frozenset[ConditionKind]:
    """The agent kinds a node's capability tags advertise (unknown names and a
    malformed tag are ignored: an older/newer agent simply does less)."""
    out: set[ConditionKind] = set()
    for tag in capabilities:
        if not isinstance(tag, str) or not tag.startswith(CAPABILITY_PREFIX):
            continue
        for name in tag.removeprefix(CAPABILITY_PREFIX).split(","):
            try:
                kind = ConditionKind(name)
            except ValueError:
                continue
            if kind in AGENT_KINDS:
                out.add(kind)
    return frozenset(out)
