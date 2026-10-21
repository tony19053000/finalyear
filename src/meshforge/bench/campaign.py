"""Campaign spec and its deterministic expansion (V0.9 G3, roadmap 0.9.3, D216).

A **campaign** measures workload families x parameters x conditions x
strategies x nodes, each configuration repeated (>= 3) so its noise can be
judged (D218). The spec is YAML (``docs/BENCHMARKS.md`` "Campaign spec",
``examples/bench/``)::

    version: 1
    name: cpu-baseline
    seed: 42
    families:
      - family: cpu.matmul
        params:
          n: [1024, 2048]                      # a list: these values
          repeats: {min: 8, max: 32, int: true, steps: 3}   # a range
          dtype: float64                       # one value
    conditions: [none, cpu_load:2, cache:cold]   # a sweep axis: one per run
    on_unsupported: error       # or skip: runs a node cannot do become skipped
    session_label: wifi         # set by the human, copied to every record
    strategies: [local, mode1]
    nodes: all                  # or a list of node ids / names
    origin: laptop-a            # the submitter; default: every node in turn
    repetitions: 3
    sampler: grid               # grid | random | lhs (+ samples: N)
    budget: {max_runs: 500, max_hours: 8}

**Pure.** No I/O, no clock, no controller import (``meshforge.bench`` imports
only ``common``; import-linter holds it): :func:`expand` turns ``(spec, nodes)``
into the run list, and the same spec, seed and node ids give the same list,
byte for byte (:func:`runs_json`) - on any machine and any Python version,
because every random choice comes from a SHA-256 counter stream
(:class:`Stream`), not from :mod:`random`.

**Expansion.**

1. *Parameter points* per family: ``grid`` = the cross product of every
   parameter's values (a range needs ``steps``); ``random`` = ``samples``
   independent draws; ``lhs`` = a Latin hypercube of ``samples`` points (each
   non-fixed parameter's range is cut into ``samples`` equal strata and every
   stratum is used exactly once - our own few lines, no scipy). A range with
   ``log: true`` is spaced/sampled on a log scale; ``int: true`` rounds.
   Every point is validated by the family's own parameter model and its
   5 s - 3 min window (G2); a refused point is a spec error naming it, unless
   ``on_invalid: drop`` - then it is dropped and *counted*
   (:attr:`Expansion.dropped`), never silently. Duplicate points (two draws
   that round to the same values) are merged and counted too.
2. *Groups* = (family, params, condition): ``group_id`` = digest of those
   (the label unit, D218), with the condition in its canonical form
   (:mod:`.conditions`; G4, D241: a run has **one** condition, the spec's
   list is a sweep axis). Each point's workload seed is derived from (spec
   seed, the point's ``none`` group), so every strategy, repetition **and
   condition** of a point runs on the same synthetic data - and a spec with
   only ``none`` expands exactly as before G4.
3. *Configurations* = group x strategy x node x origin: ``local`` runs on the
   origin itself; ``mode1`` runs on every other selected node, submitted from
   the origin. Without ``origin`` every selected node is the origin in turn.
   ``config_id`` = digest of (group, strategy, node, origin).
4. *Order*: ``repetitions`` rounds; each round visits **every** configuration
   once, in an order shuffled by (seed, round) - so slow drift over a night
   (thermal, battery, other load) spreads over all configurations instead of
   biasing whichever ran last (D216).
5. *Budget*: more runs than ``budget.max_runs``, or more estimated hours than
   ``budget.max_hours`` (the family's ``expected_runtime_s`` on the reference
   mid laptop + :data:`RUN_OVERHEAD_S` per run + what the condition adds:
   fillers, a warm-up run, a pause), is an error **with the count** - never a
   silent truncation.
6. *Support* (G4): a configuration whose node cannot do its condition
   (:func:`.conditions.unsupported_reason`: the node's ``bench-conditions``
   tag, its threads, ``unavailable`` only for ``mode1``) is an error listing
   every node x condition, or - with ``on_unsupported: skip`` - a run with a
   ``skip_reason`` that is stored ``skipped`` (counted, never silent).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from meshforge.common.errors import FailureType, MeshForgeError

from .conditions import (
    Condition,
    ConditionError,
    ConditionKind,
    extra_seconds,
    parse_condition,
    unsupported_reason,
)
from .workloads import MID_LAPTOP, ParamsError, UnknownFamilyError, get_family
from .workloads.base import Family

__all__ = [
    "CONDITIONS_V1",
    "HARD_MAX_RUNS",
    "MAX_SPEC_BYTES",
    "RUN_OVERHEAD_S",
    "SESSION_LABEL_RE",
    "SPEC_VERSION",
    "Budget",
    "CampaignSpec",
    "CampaignSpecError",
    "Expansion",
    "FamilySweep",
    "NodeRef",
    "PlannedRun",
    "Range",
    "Sampler",
    "Strategy",
    "Stream",
    "expand",
    "load_spec",
    "parse_spec",
    "run_dict",
    "runs_json",
    "spec_digest",
]

SPEC_VERSION: Final = 1
MAX_SPEC_BYTES: Final = 256 * 1024
"""A spec is a short YAML file; anything larger is refused before parsing."""
HARD_MAX_RUNS: Final = 100_000
"""Upper bound of ``budget.max_runs`` (and of any expansion): a typo in a grid
cannot build a million-run list in memory."""
MAX_POINTS_PER_FAMILY: Final = 10_000
MAX_SAMPLES: Final = 10_000
MAX_REPETITIONS: Final = 100
MAX_NAME_CHARS: Final = 64
RUN_OVERHEAD_S: Final = 15.0
"""Per-run seconds added to the compute hint for the budget's hour estimate:
queue + stage + env (cached after the first run on a node) + collect, measured
at ~5-10 s on the harness (G2 cluster test); 15 s keeps the estimate honest."""
CONDITIONS_V1: Final = tuple(k.value for k in ConditionKind)
"""Condition names this version accepts (G4, D217; ``name[:arg]``,
:mod:`meshforge.common.bench_conditions`)."""
SESSION_LABEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
"""``session_label``: e.g. ``wifi``, ``ethernet``, ``lab-2026-10-18``."""
_NAME_RE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ID_HEX: Final = 24
"""Hex digits of a group/config id (96 bits): short enough to read, no
collision in any campaign this fabric can run."""


class CampaignSpecError(MeshForgeError, ValueError):
    """The spec is invalid, or its expansion breaks a rule (budget, nodes).
    ``problems`` lists every problem found (``where: why``)."""

    default_failure_type: ClassVar[FailureType] = FailureType.INVALID_INPUT

    def __init__(self, problems: Sequence[str], **details: Any) -> None:
        listed = list(problems)
        super().__init__(
            "invalid campaign: " + "; ".join(listed[:10]),
            details={"problems": listed, **details},
        )
        self.problems = listed


class Strategy(StrEnum):
    """V0.9 strategies (D215): the submitter's own node, or Mode 1 elsewhere.
    V0.10 G8 (D271): ``mode3`` - a split family run over a node set
    (``mode3_sets``; every selected node when none is given). For a split
    family ``local`` / ``mode1`` mean "don't split" on that node."""

    LOCAL = "local"
    MODE1 = "mode1"
    MODE3 = "mode3"


MAX_MODE3_SETS: Final = 16


class Sampler(StrEnum):
    GRID = "grid"
    RANDOM = "random"
    LHS = "lhs"


type Scalar = StrictInt | StrictFloat | StrictStr | StrictBool


class Range(BaseModel):
    """``{min, max, log?, int?, steps?}`` - a numeric parameter range."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    min: StrictInt | StrictFloat
    max: StrictInt | StrictFloat
    log: StrictBool = False
    integer: StrictBool = Field(default=False, alias="int")
    steps: Annotated[int, Field(ge=2, le=MAX_POINTS_PER_FAMILY, strict=True)] | None = (
        None
    )
    """Grid points (``grid`` sampler only; endpoints included)."""

    @model_validator(mode="after")
    def _check(self) -> Range:
        if not (math.isfinite(self.min) and math.isfinite(self.max)):
            raise ValueError("min and max must be finite")
        if self.min > self.max:
            raise ValueError("min is larger than max")
        if self.log and self.min <= 0:
            raise ValueError("a log range needs min > 0")
        if self.integer and not (
            float(self.min).is_integer() and float(self.max).is_integer()
        ):
            raise ValueError("an int range needs integer min and max")
        return self

    def at(self, unit: float) -> int | float:
        """The value at ``unit`` in [0, 1] of the range (log-scaled if ``log``)."""
        low, high = float(self.min), float(self.max)
        if self.log:
            value = math.exp(math.log(low) + unit * (math.log(high) - math.log(low)))
        else:
            value = low + unit * (high - low)
        value = min(max(value, low), high)
        if self.integer:
            return round(value)
        return float(f"{value:.6g}")


type ParamValue = Scalar | list[Scalar] | Range


class FamilySweep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: StrictStr
    params: dict[StrictStr, ParamValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _finite(self) -> FamilySweep:
        """D-2 (fix r1): NaN / infinity is never a parameter value (YAML
        ``.nan`` / ``.inf``): JSON cannot carry it and no family accepts it."""
        for name, value in self.params.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError(f"params.{name}: {item!r} is not a finite number")
        return self


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_runs: Annotated[int, Field(ge=1, le=HARD_MAX_RUNS, strict=True)]
    max_hours: Annotated[float, Field(gt=0, le=24 * 30, allow_inf_nan=False)] | None = (
        None
    )


class CampaignSpec(BaseModel):
    """A validated campaign spec (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    name: StrictStr
    seed: Annotated[int, Field(ge=0, le=2**63 - 1, strict=True)]
    families: Annotated[list[FamilySweep], Field(min_length=1, max_length=64)]
    conditions: Annotated[list[StrictStr], Field(min_length=1, max_length=16)] = Field(
        default_factory=lambda: ["none"]
    )
    strategies: Annotated[list[Strategy], Field(min_length=1)] = Field(
        default_factory=lambda: [Strategy.LOCAL]
    )
    nodes: Literal["all"] | Annotated[list[StrictStr], Field(min_length=1)] = "all"
    mode3_sets: (
        Annotated[
            list[Annotated[list[StrictStr], Field(min_length=2, max_length=64)]],
            Field(min_length=1, max_length=MAX_MODE3_SETS),
        ]
        | None
    ) = None
    """V0.10 G8: the node sets a ``mode3`` run is forced onto (ids or names,
    each a subset of ``nodes``); None = one set of every selected node."""
    origin: StrictStr | None = None
    on_unsupported: Literal["error", "skip"] = "error"
    """G4: a node x condition the node cannot do - an error, or skipped runs."""
    session_label: StrictStr | None = None
    """G4 (D241): Wi-Fi vs Ethernet etc. - a label the human sets for the
    whole session, copied to every record; not a condition."""
    repetitions: Annotated[int, Field(ge=3, le=MAX_REPETITIONS, strict=True)] = 3
    sampler: Sampler = Sampler.GRID
    samples: Annotated[int, Field(ge=1, le=MAX_SAMPLES, strict=True)] | None = None
    on_invalid: Literal["error", "drop"] = "error"
    budget: Budget

    @model_validator(mode="after")
    def _check(self) -> CampaignSpec:
        problems: list[str] = []
        if not _NAME_RE.fullmatch(self.name):
            problems.append(
                "name: 1-64 of a-z 0-9 . _ - (starting with a letter or digit)"
            )
        if self.sampler is Sampler.GRID and self.samples is not None:
            problems.append("samples: only for the random and lhs samplers")
        if self.sampler is not Sampler.GRID and self.samples is None:
            problems.append(f"samples: required by the {self.sampler.value} sampler")
        canonical: list[str] = []
        for index, text in enumerate(self.conditions):
            try:
                canonical.append(parse_condition(text).canonical())
            except ConditionError as exc:
                problems.append(f"conditions[{index}]: {exc}")
        if len(set(canonical)) != len(canonical):
            problems.append("conditions: listed twice")
        if self.session_label is not None and not SESSION_LABEL_RE.fullmatch(
            self.session_label
        ):
            problems.append(
                "session_label: 1-32 of A-Z a-z 0-9 . _ - (starting with a "
                "letter or digit)"
            )
        if len(set(self.strategies)) != len(self.strategies):
            problems.append("strategies: listed twice")
        if isinstance(self.nodes, list) and len(set(self.nodes)) != len(self.nodes):
            problems.append("nodes: listed twice")
        problems.extend(self._mode3_problems(canonical))
        for index, sweep in enumerate(self.families):
            problems.extend(_sweep_problems(index, sweep, self.sampler))
        if problems:
            raise ValueError(" | ".join(problems))
        return self

    def _mode3_problems(self, conditions: list[str]) -> list[str]:
        """V0.10 G8 rules for split families and ``mode3`` (D271)."""
        problems: list[str] = []
        mode3 = Strategy.MODE3 in self.strategies
        if self.mode3_sets is not None and not mode3:
            problems.append("mode3_sets: only with strategy mode3")
        for index, group in enumerate(self.mode3_sets or ()):
            if len(set(group)) != len(group):
                problems.append(f"mode3_sets[{index}]: a node is listed twice")
        split = [s.family for s in self.families if _is_split_family(s.family) is True]
        if mode3 and not split:
            problems.append(
                "strategies: mode3 needs a split family (split.*) in families"
            )
        if split and any(c != "none" for c in conditions):
            problems.append(
                "conditions: split families run under condition none only "
                "(V0.10; conditions on lanes are not defined yet)"
            )
        return problems


def _is_split_family(name: str) -> bool | None:
    """None = no such family (reported by ``_sweep_problems``)."""
    try:
        return bool(get_family(name).split)
    except UnknownFamilyError:
        return None


def _sweep_problems(index: int, sweep: FamilySweep, sampler: Sampler) -> list[str]:
    where = f"families[{index}]"
    try:
        family = get_family(sweep.family)
    except UnknownFamilyError:
        return [f"{where}.family: unknown workload family {sweep.family!r}"]
    known = family.params_model.model_fields
    problems: list[str] = []
    for name, value in sweep.params.items():
        if name not in known:
            problems.append(
                f"{where}.params.{name}: {family.name} has no such parameter "
                f"(it has {', '.join(sorted(known))})"
            )
        elif isinstance(value, list) and not value:
            problems.append(f"{where}.params.{name}: an empty list")
        elif isinstance(value, list) and len({_key(v) for v in value}) != len(value):
            problems.append(f"{where}.params.{name}: a value is listed twice")
        elif isinstance(value, Range) and sampler is Sampler.GRID and not value.steps:
            problems.append(f"{where}.params.{name}: a range needs steps under grid")
        elif isinstance(value, Range) and sampler is not Sampler.GRID and value.steps:
            problems.append(f"{where}.params.{name}: steps is for the grid sampler")
    return problems


def _key(value: Scalar) -> str:
    return json.dumps(value)


# ---------------------------------------------------------------- loading


def load_spec(text: str | bytes) -> CampaignSpec:
    """Parse and validate a YAML spec. Raises :class:`CampaignSpecError`."""
    data = text.encode("utf-8") if isinstance(text, str) else text
    if len(data) > MAX_SPEC_BYTES:
        raise CampaignSpecError([f"<spec>: larger than {MAX_SPEC_BYTES} bytes"])
    try:
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "<spec>"
        raise CampaignSpecError([f"{where}: not valid YAML"]) from None
    if not isinstance(raw, dict):
        raise CampaignSpecError(["<spec>: must be a YAML mapping"])
    return parse_spec(raw)


def parse_spec(raw: Mapping[str, Any]) -> CampaignSpec:
    """Validate an already-parsed spec mapping. Raises :class:`CampaignSpecError`."""
    try:
        return CampaignSpec.model_validate(raw)
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors(include_url=False, include_input=False):
            where = ".".join(str(p) for p in err["loc"]) or "<spec>"
            message = str(err["msg"]).removeprefix("Value error, ")
            problems.extend(
                f"{where}: {part}" if where != "<spec>" else part
                for part in message.split(" | ")
            )
        raise CampaignSpecError(problems) from None


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def spec_digest(spec: CampaignSpec) -> str:
    """``sha256:`` of the spec's canonical JSON: two specs with the same digest
    expand identically against the same nodes (comments and key order in the
    YAML do not count)."""
    data = spec.model_dump(mode="json", by_alias=True)
    if data.get("mode3_sets") is None:
        # V0.10 G8: absent = the pre-V0.10 spec's digest, unchanged
        data.pop("mode3_sets", None)
    body = _canonical(data)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


# ---------------------------------------------------------------- randomness


class Stream:
    """Deterministic uniform numbers from SHA-256(key || counter).

    Python's :mod:`random` is stable in practice but its algorithms are not a
    published contract; a dataset that must re-expand identically years later
    (D222) uses this instead. ~1 us per draw - campaigns draw thousands."""

    def __init__(self, *parts: object) -> None:
        self._key = hashlib.sha256(_canonical(list(parts)).encode()).digest()
        self._counter = 0

    def _next64(self) -> int:
        block = hashlib.sha256(self._key + self._counter.to_bytes(8, "little"))
        self._counter += 1
        return int.from_bytes(block.digest()[:8], "little")

    def uniform(self) -> float:
        """A float in [0, 1) with 53 random bits."""
        return (self._next64() >> 11) / float(1 << 53)

    def below(self, n: int) -> int:
        """An int in [0, n), unbiased (rejection sampling)."""
        if n < 1:
            raise ValueError("n must be >= 1")
        limit = (1 << 64) - ((1 << 64) % n)
        while True:
            value = self._next64()
            if value < limit:
                return value % n

    def shuffle[T](self, items: list[T]) -> None:
        """Fisher-Yates, in place."""
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]


# ---------------------------------------------------------------- expansion


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A node a campaign may use: its id and (optional) display name."""

    node_id: str
    name: str | None = None
    conditions: frozenset[ConditionKind] | None = None
    """G4: the agent condition kinds the node advertises (``bench-conditions``
    tag); None = unknown, not checked (an offline ``bench plan --node``)."""
    threads: int | None = None
    """CPU threads (bounds ``cpu_load:N``); None = unknown."""


@dataclass(frozen=True, slots=True)
class PlannedRun:
    """One run of the campaign, in execution order (``seq`` from 1)."""

    seq: int
    group_id: str
    config_id: str
    repetition: int
    family: str
    params: dict[str, Any]
    seed: int
    """The workload's seed (same for every run of the group)."""
    conditions: tuple[str, ...]
    """G4: exactly one canonical condition (``("none",)`` = the baseline)."""
    strategy: str
    node_id: str
    """Where the run is pinned."""
    origin_node_id: str
    """The submitter (``local``: equal to ``node_id``)."""
    expected_s: float
    """Compute hint on the reference mid laptop (s), without overhead."""
    skip_reason: str | None = None
    """G4, ``on_unsupported: skip``: why the node cannot run this condition
    (the run is stored ``skipped``); None = a normal run."""
    node_set: tuple[str, ...] = ()
    """V0.10 G8: a ``mode3`` run's node set (sorted ids; ``node_id`` is its
    first); empty for ``local`` / ``mode1``."""

    @property
    def condition(self) -> str:
        """The run's one condition (canonical)."""
        return self.conditions[0]


@dataclass(frozen=True, slots=True)
class Expansion:
    """:func:`expand`'s answer: the runs plus what was dropped and the cost."""

    runs: tuple[PlannedRun, ...]
    groups: int
    configs: int
    dropped: int = 0
    """Parameter points the family refused (``on_invalid: drop`` only)."""
    duplicates: int = 0
    """Points merged because they equalled an earlier one."""
    estimated_hours: float = 0.0
    problems: tuple[str, ...] = field(default=())
    """Why points were dropped (first few)."""
    unsupported: int = 0
    """G4: runs stored ``skipped`` because their node cannot do the
    condition (``on_unsupported: skip``)."""


@dataclass(frozen=True, slots=True)
class _Group:
    group_id: str
    family: str
    params: dict[str, Any]
    seed: int
    expected_s: float
    condition: Condition
    split: bool = False
    """V0.10 G8: a split family (its configurations may include mode3)."""


def _digest(prefix: str, value: object) -> str:
    try:
        text = _canonical(value)
    except (ValueError, TypeError):  # NaN/inf or a non-JSON value: the spec's fault
        raise CampaignSpecError(
            [f"{_describe(value)}: not a JSON value (NaN, infinity?)"]
        ) from None
    return prefix + hashlib.sha256(text.encode()).hexdigest()[:_ID_HEX]


def _describe(value: object) -> str:
    """A point for an error message - never raises (NaN / inf allowed here)."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (ValueError, TypeError, RecursionError):  # pragma: no cover - defensive
        return repr(value)


def _grid_values(value: ParamValue) -> list[Any]:
    if isinstance(value, Range):
        steps = value.steps or 2
        out: list[Any] = []
        for i in range(steps):
            point = value.at(i / (steps - 1))
            if point not in out:
                out.append(point)
        return out
    if isinstance(value, list):
        return list(value)
    return [value]


def _draw(value: ParamValue, unit: float) -> Any:
    """The value of a non-fixed parameter at ``unit`` in [0, 1)."""
    if isinstance(value, Range):
        return value.at(unit)
    if isinstance(value, list):
        return value[min(int(unit * len(value)), len(value) - 1)]
    return value


def _points(spec: CampaignSpec, index: int, sweep: FamilySweep) -> list[dict[str, Any]]:
    names = sorted(sweep.params)
    if spec.sampler is Sampler.GRID:
        axes = [_grid_values(sweep.params[n]) for n in names]
        total = math.prod(len(a) for a in axes)
        if total > MAX_POINTS_PER_FAMILY:
            raise CampaignSpecError(
                [
                    f"families[{index}]: the grid has {total} points, more than "
                    f"{MAX_POINTS_PER_FAMILY}"
                ],
                points=total,
            )
        return [
            dict(zip(names, combo, strict=True)) for combo in itertools.product(*axes)
        ]
    samples = spec.samples or 1
    free = [n for n in names if isinstance(sweep.params[n], Range | list)]
    fixed = {n: sweep.params[n] for n in names if n not in free}
    stream = Stream("points", spec.seed, index, sweep.family, spec.sampler.value)
    strata: dict[str, list[int]] = {}
    if spec.sampler is Sampler.LHS:
        for name in free:  # one permutation of the strata per dimension
            order = list(range(samples))
            stream.shuffle(order)
            strata[name] = order
    points: list[dict[str, Any]] = []
    for i in range(samples):
        point = dict(fixed)
        for name in free:
            if spec.sampler is Sampler.LHS:
                unit = (strata[name][i] + stream.uniform()) / samples
            else:
                unit = stream.uniform()
            point[name] = _draw(sweep.params[name], unit)
        points.append(point)
    return points


def _groups(
    spec: CampaignSpec,
) -> tuple[list[_Group], int, int, list[str]]:
    groups: list[_Group] = []
    seen: set[str] = set()
    dropped = duplicates = 0
    problems: list[str] = []
    conditions = sorted(
        {c.canonical(): c for c in map(parse_condition, spec.conditions)}.items()
    )
    for index, sweep in enumerate(spec.families):
        family: Family[Any] = get_family(sweep.family)
        for point in _points(spec, index, sweep):
            try:
                params = family.params_dict(family.parse_params(point))
            except ParamsError as exc:
                where = f"families[{index}] {family.name} {_describe(point)}"
                if spec.on_invalid == "error":
                    raise CampaignSpecError(
                        [f"{where}: {p}" for p in exc.problems]
                        + ["(narrow the ranges, or set on_invalid: drop)"]
                    ) from None
                dropped += 1
                if len(problems) < 20:
                    problems.append(f"{where}: {'; '.join(exc.problems)}")
                continue
            # one seed per point, the same for every condition: derived from
            # the point's ``none`` group (the pre-G4 identity, so a none-only
            # spec keeps its ids and seeds)
            baseline = _digest(
                "grp_",
                {"family": family.name, "params": params, "conditions": ["none"]},
            )
            if baseline in seen:
                duplicates += 1
                continue
            seen.add(baseline)
            seed = int(
                hashlib.sha256(f"{spec.seed}:{baseline}".encode()).hexdigest()[:16], 16
            ) % (2**32)
            expected = float(family.expected_runtime_s(params, MID_LAPTOP))
            for text, condition in conditions:
                identity = {
                    "family": family.name,
                    "params": params,
                    "conditions": [text],
                }
                groups.append(
                    _Group(
                        group_id=_digest("grp_", identity),
                        family=family.name,
                        params=params,
                        seed=family.check_seed(seed),
                        expected_s=expected,
                        condition=condition,
                        split=bool(family.split),
                    )
                )
    return groups, dropped, duplicates, problems


def _resolve(
    spec: CampaignSpec, nodes: Sequence[NodeRef]
) -> tuple[list[str], str | None, list[tuple[str, ...]]]:
    """(selected node ids sorted, origin id or None, the ``mode3`` node sets -
    each sorted, duplicates merged; every selected node when the spec names
    none)."""
    by_id = {n.node_id: n for n in nodes}
    by_name: dict[str, list[str]] = {}
    for n in nodes:
        if n.name:
            by_name.setdefault(n.name, []).append(n.node_id)
    problems: list[str] = []

    def one(ref: str, where: str) -> str | None:
        if ref in by_id:
            return ref
        named = by_name.get(ref, [])
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            problems.append(
                f"{where}: name {ref!r} matches {len(named)} nodes; use an id"
            )
        else:
            problems.append(f"{where}: no node {ref!r}")
        return None

    if spec.nodes == "all":
        selected = sorted(by_id)
    else:
        found = [one(ref, f"nodes[{i}]") for i, ref in enumerate(spec.nodes)]
        selected = sorted({f for f in found if f is not None})
    origin = None if spec.origin is None else one(spec.origin, "origin")
    if origin is not None and origin not in selected:
        problems.append(f"origin: {spec.origin!r} is not one of the campaign's nodes")
    if not selected and not problems:
        problems.append("nodes: no node to run on")
    sets: set[tuple[str, ...]] = set()
    if Strategy.MODE3 in spec.strategies:
        if spec.mode3_sets is None:
            if len(selected) >= 2:
                sets.add(tuple(selected))
            else:
                problems.append("strategies: mode3 needs at least two nodes")
        for i, group in enumerate(spec.mode3_sets or ()):
            found = [one(ref, f"mode3_sets[{i}][{j}]") for j, ref in enumerate(group)]
            ids = sorted({f for f in found if f is not None})
            outside = [n for n in ids if n not in selected]
            if outside:
                problems.append(
                    f"mode3_sets[{i}]: {', '.join(outside)} is not one of the "
                    "campaign's nodes"
                )
            elif len(ids) < 2 and None not in found:
                problems.append(f"mode3_sets[{i}]: a set needs two different nodes")
            elif None not in found:
                sets.add(tuple(ids))
    if problems:
        raise CampaignSpecError(problems)
    return selected, origin, sorted(sets)


type _Config = tuple[str, str, str, tuple[str, ...]]
"""``(strategy, node, origin, node_set)``; ``node_set`` empty except mode3
(whose ``node`` is the set's first id)."""


def _configs(
    spec: CampaignSpec,
    selected: list[str],
    origin: str | None,
    sets: Sequence[tuple[str, ...]] = (),
) -> list[_Config]:
    """The configurations' placements, sorted."""
    origins = [origin] if origin is not None else selected
    out: set[_Config] = set()
    for src in origins:
        for strategy in spec.strategies:
            if strategy is Strategy.LOCAL:
                out.add((strategy.value, src, src, ()))
            elif strategy is Strategy.MODE3:
                out.update((strategy.value, s[0], src, s) for s in sets)
            else:
                out.update((strategy.value, n, src, ()) for n in selected if n != src)
    return sorted(out)


def _config_id(group: _Group, t: _Config) -> str:
    identity: dict[str, Any] = {
        "group": group.group_id,
        "strategy": t[0],
        "node": t[1],
        "origin": t[2],
    }
    if t[3]:  # V0.10 G8: only mode3 names a set (older config ids unchanged)
        identity["node_set"] = list(t[3])
    return _digest("cfg_", identity)


def expand(spec: CampaignSpec, nodes: Sequence[NodeRef]) -> Expansion:
    """The campaign's runs, in execution order (see the module docstring).
    Raises :class:`CampaignSpecError`."""
    selected, origin, sets = _resolve(spec, nodes)
    groups, dropped, duplicates, problems = _groups(spec)
    triples = _configs(spec, selected, origin, sets)
    configs: list[tuple[str, _Group, _Config]] = [
        (_config_id(g, t), g, t)
        for g in groups
        for t in triples
        # V0.10 G8: mode3 splits; only a split family can be split
        if t[0] != Strategy.MODE3.value or g.split
    ]
    configs.sort(key=lambda c: c[0])
    skips = _support(spec, configs, {n.node_id: n for n in nodes})
    total = len(configs) * spec.repetitions
    if total == 0:
        raise CampaignSpecError(
            [
                "the spec expands to 0 runs (mode1 needs a second node; or every "
                "parameter point was dropped)"
            ]
        )
    if total > spec.budget.max_runs:
        raise CampaignSpecError(
            [
                f"the spec expands to {total} runs ({len(configs)} configurations x "
                f"{spec.repetitions} repetitions), more than budget.max_runs "
                f"{spec.budget.max_runs}"
            ],
            runs=total,
        )
    seconds = spec.repetitions * sum(
        c[1].expected_s
        + RUN_OVERHEAD_S
        + extra_seconds(c[1].condition, c[1].expected_s + RUN_OVERHEAD_S)
        for c in configs
        if c[0] not in skips
    )
    hours = seconds / 3600.0
    if spec.budget.max_hours is not None and hours > spec.budget.max_hours:
        raise CampaignSpecError(
            [
                f"the spec needs about {hours:.1f} h on the reference mid laptop "
                f"({total} runs), more than budget.max_hours {spec.budget.max_hours:g}"
            ],
            runs=total,
            hours=round(hours, 2),
        )
    runs: list[PlannedRun] = []
    for repetition in range(1, spec.repetitions + 1):
        order = list(configs)
        Stream("order", spec.seed, repetition).shuffle(order)
        for config_id, group, (strategy, node, src, node_set) in order:
            runs.append(
                PlannedRun(
                    seq=len(runs) + 1,
                    group_id=group.group_id,
                    config_id=config_id,
                    repetition=repetition,
                    family=group.family,
                    params=dict(group.params),
                    seed=group.seed,
                    conditions=(group.condition.canonical(),),
                    strategy=strategy,
                    node_id=node,
                    origin_node_id=src,
                    expected_s=round(group.expected_s, 3),
                    skip_reason=skips.get(config_id),
                    node_set=node_set,
                )
            )
    return Expansion(
        runs=tuple(runs),
        groups=len(groups),
        configs=len(configs),
        dropped=dropped,
        duplicates=duplicates,
        estimated_hours=round(hours, 3),
        problems=tuple(problems),
        unsupported=len(skips) * spec.repetitions,
    )


def _support(
    spec: CampaignSpec,
    configs: Sequence[tuple[str, _Group, _Config]],
    nodes: Mapping[str, NodeRef],
) -> dict[str, str]:
    """``config_id -> reason`` of the configurations whose node cannot do
    their condition (``on_unsupported: skip``); raises
    :class:`CampaignSpecError` listing every node x condition otherwise."""
    reasons: dict[str, str] = {}
    listed: dict[tuple[str, str], str] = {}
    for config_id, group, (strategy, node, _src, _set) in configs:
        ref = nodes.get(node)
        reason = unsupported_reason(
            group.condition,
            strategy=strategy,
            node_kinds=None if ref is None else ref.conditions,
            node_threads=None if ref is None else ref.threads,
        )
        if reason is None:
            continue
        reasons[config_id] = reason
        listed.setdefault((node, group.condition.canonical()), reason)
    if reasons and spec.on_unsupported == "error":
        raise CampaignSpecError(
            [
                f"node {node} x condition {condition}: {reason}"
                for (node, condition), reason in sorted(listed.items())
            ]
            + ["(drop the condition or the node, or set on_unsupported: skip)"],
            unsupported=[
                {"node_id": node, "condition": condition}
                for node, condition in sorted(listed)
            ],
        )
    return reasons


def run_dict(run: PlannedRun) -> dict[str, Any]:
    out = asdict(run)
    out["conditions"] = list(run.conditions)
    if run.node_set:
        out["node_set"] = list(run.node_set)
    else:
        # V0.10 G8: absent for local / mode1 - the pre-V0.10 run JSON is unchanged
        del out["node_set"]
    if run.skip_reason is None:
        # G4: absent for a normal run - the pre-G4 run JSON is unchanged
        del out["skip_reason"]
    return out


def runs_json(runs: Sequence[PlannedRun]) -> str:
    """Canonical JSON of a run list (the determinism tests compare its bytes)."""
    return _canonical([run_dict(r) for r in runs])
