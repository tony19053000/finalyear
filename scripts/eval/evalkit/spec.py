"""The evaluation campaign file (YAML, D417/D419).

::

    name: v1-eval-wifi            # [a-z0-9][a-z0-9._-]* - names the raw folder's runs
    seed: 20261027                # every random choice (block order, B2's seed) follows it
    link: wifi                    # wifi | ethernet | loopback (A7 compares two campaigns)
    repetitions: 10               # >= 10 for evidence (D417); fewer only for a smoke run
    jobs_per_run: 1               # identical jobs submitted together per run (makespan)
    run_timeout_s: 1800           # a run not finished by then is recorded as timed out
    objective: min_time           # cost_model arms' objective
    origin: null                  # the Local laptop's node id; null = this machine's agent
    smoke: false                  # true: sizes below the 5 s benchmark window are allowed;
                                  #   the report marks the campaign "smoke - not evidence"
    arms: [B0, B1, B2, B3, B4, B5, B6]
    families:
      - family: cpu.primes
        sizes:
          s: {limit: 100000000, repeats: 4}
          l: {limit: 300000000, repeats: 4}
      - family: split.hash_files
        sizes:
          s: {files: 256, size_kb: 256, rounds: 100}

Validation happens before anything runs: every family exists, every size's
params pass the family's own model (and, unless ``smoke``, its 5 s - 3 min
window), every arm is known. The spec's digest (canonical JSON of the parsed
model) pins a raw folder to its campaign: a resume with a different file is
refused.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from evalkit.arms import ARMS
from evalkit.canonical import sha256_bytes

__all__ = [
    "MIN_EVIDENCE_REPETITIONS",
    "CampaignSpec",
    "FamilySizes",
    "SpecError",
    "load_spec",
    "parse_spec",
    "spec_digest",
]

MIN_EVIDENCE_REPETITIONS: Final = 10
"""D417: each (family, size, arm) cell needs at least this many repetitions
for the campaign to count as evidence (fewer is allowed and reported)."""

_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_SIZE_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,15}$"


class SpecError(ValueError):
    """The campaign file is not valid (``problems`` lists every reason)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FamilySizes(_Model):
    family: str = Field(min_length=1, max_length=64)
    sizes: dict[str, dict[str, Any]] = Field(min_length=1, max_length=16)

    @field_validator("sizes")
    @classmethod
    def _labels(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        bad = [k for k in value if not re.fullmatch(_SIZE_PATTERN, k)]
        if bad:
            raise ValueError(f"size labels must match {_SIZE_PATTERN}: {bad}")
        return value


class CampaignSpec(_Model):
    name: str = Field(pattern=_NAME_PATTERN)
    seed: int = Field(ge=0, le=2**31 - 1)
    link: Literal["wifi", "ethernet", "loopback"]
    repetitions: int = Field(ge=1, le=100)
    jobs_per_run: int = Field(default=1, ge=1, le=16)
    run_timeout_s: float = Field(default=1800.0, gt=0, le=86_400.0)
    objective: Literal["min_time", "min_network", "prefer_local"] = "min_time"
    origin: str | None = Field(default=None, min_length=1, max_length=40)
    smoke: bool = False
    arms: tuple[str, ...] = Field(min_length=1, max_length=len(ARMS))
    families: tuple[FamilySizes, ...] = Field(min_length=1, max_length=32)

    @field_validator("arms")
    @classmethod
    def _arms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [a for a in value if a not in ARMS]
        if unknown:
            raise ValueError(f"unknown arm(s) {unknown}; known: {sorted(ARMS)}")
        if len(set(value)) != len(value):
            raise ValueError("an arm is listed twice")
        return value

    @property
    def evidence(self) -> bool:
        """Counts as evidence (not smoke, enough repetitions, D417)."""
        return not self.smoke and self.repetitions >= MIN_EVIDENCE_REPETITIONS

    def cells(self) -> list[tuple[str, str]]:
        """``(family, size)`` pairs in file order."""
        return [(f.family, size) for f in self.families for size in f.sizes]

    def params(self, family: str, size: str) -> dict[str, Any]:
        for item in self.families:
            if item.family == family:
                return dict(item.sizes[size])
        raise KeyError(family)


def _family_problems(spec: CampaignSpec) -> list[str]:
    from meshforge.bench.workloads import ParamsError, UnknownFamilyError, get_family

    problems: list[str] = []
    seen: set[str] = set()
    for item in spec.families:
        if item.family in seen:
            problems.append(f"family {item.family} is listed twice")
        seen.add(item.family)
        try:
            family = get_family(item.family)
        except UnknownFamilyError:
            problems.append(f"unknown workload family {item.family}")
            continue
        for size, params in item.sizes.items():
            try:
                if spec.smoke:
                    family._model(params)
                else:
                    family.parse_params(params)
            except ParamsError as exc:
                problems.append(
                    f"{item.family} size {size}: " + "; ".join(exc.problems)
                )
    return problems


def parse_spec(raw: Mapping[str, Any]) -> CampaignSpec:
    """A validated spec, or :class:`SpecError` with every problem."""
    try:
        spec = CampaignSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecError(
            [
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            ]
        ) from None
    problems = _family_problems(spec)
    if problems:
        raise SpecError(problems)
    return spec


def load_spec(text: str) -> CampaignSpec:
    """Parse the YAML text of a campaign file (``safe_load``)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError([f"not valid YAML: {exc}"]) from None
    if not isinstance(raw, Mapping):
        raise SpecError(["the campaign file must be a mapping"])
    return parse_spec(raw)


def spec_digest(spec: CampaignSpec) -> str:
    """``sha256:<hex>`` of the parsed spec's canonical JSON."""
    text = json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + sha256_bytes(text.encode("utf-8"))
