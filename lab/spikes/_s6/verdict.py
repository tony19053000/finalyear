"""Turn S6 result envelopes into per-laptop verdicts and a cost table.

Judged **only from the JSON**, never from live state, with the S2.md §4
honesty rules: only measured repetitions count, ``n < 5`` is ``INVALID``,
nothing missing is guessed (``TBD``), a corrupt result-file shape is reported
as ``INVALID (malformed)`` / ``?`` and never crashed on.

Per laptop:

* ``docker``: ``usable`` (docker-start ran every requested repetition, n >= 5)
  · ``unusable (<failure_type>: ... [fix: ...])`` · ``flaky k/n`` ·
  ``INVALID (n<5)`` · ``skipped`` · ``INVALID (malformed)`` · ``TBD``.
* ``gpu-in-container`` (docker-gpu-smi) and ``torch-cuda-in-container``
  (docker-gpu-torch): ``yes`` · ``no (<reason> [fix])`` · ``n/a (no NVIDIA GPU
  on host)`` · ``skipped`` · ``flaky k/n`` · ``INVALID (n<5)`` · ``TBD``.
* ``runtime recommendation (ADR-0004)``: ``docker`` only when docker is
  ``usable`` **and** (gpu-in-container is ``yes`` **or** the host has no NVIDIA
  GPU); ``venv-fallback`` when docker is unusable or gpu-in-container is ``no``
  on a GPU host; otherwise ``TBD`` with the reason. Never ``docker`` from an
  INVALID row.
* a cost table (``mean ± stdev (n=k)`` per case) and the V0.5 criterion 5
  calibration line, computed from valid rows only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import host_label
from spikes._harness.report import fmt
from spikes._harness.reps import PHASE_WARMUP
from spikes._harness.stats import Summary

YES: Final = "yes"
NO: Final = "no"
TBD: Final = "TBD"
SKIPPED: Final = "skipped"
NOT_APPLICABLE: Final = "n/a"
USABLE: Final = "usable"
UNUSABLE: Final = "unusable"
RECOMMEND_DOCKER: Final = "docker"
RECOMMEND_VENV: Final = "venv-fallback"
INVALID: Final = f"INVALID (n<{MIN_VALID_REPS})"
MALFORMED: Final = "INVALID (malformed)"
SKIPPED_TYPE: Final = "skipped_by_flag"
#: Setup failures that say nothing about the runtime itself (the case was not
#: measured): the verdict stays ``TBD`` with the reason, never ``no``.
NOT_MEASURED_TYPES: Final = frozenset(
    {
        "insufficient_disk",
        "image_pull_failed",
        "image_remove_failed",
        "timeout",
        "budget_exhausted",
    }
)
#: V0.5 success criterion 5: warm env cache -> job start overhead <= 3 s (calibrate).
TARGET_START_OVERHEAD_S: Final = 3.0

#: (case id, metric, row label) of the cost table, in report order.
COST_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("docker-start", "start_to_exit_ms", "container start to exit, sandboxed (ms)"),
    ("venv-start", "start_to_exit_ms", "venv process start to exit (ms)"),
    ("docker-pull-cold", "pull_s", "small image cold pull (s)"),
    ("venv-cold", "total_s", "venv create + install, cold cache (s)"),
    ("venv-warm", "total_s", "venv create + install, warm cache (s)"),
    (
        "docker-gpu-torch",
        "start_to_ready_ms",
        "PyTorch-CUDA container start to CUDA ready (ms)",
    ),
)


@dataclass(frozen=True, slots=True)
class Verdict:
    value: str
    detail: str = ""

    def cell(self) -> str:
        return f"{self.value}{f' — {self.detail}' if self.detail else ''}"

    def to_json(self) -> dict[str, str]:
        return {"value": self.value, "detail": self.detail}


# --------------------------------------------------------- shape guards
def _mapping(value: object) -> Mapping[str, Any]:
    """``value`` if it is an object, else empty: a malformed result file is
    reported as unknown facts, never crashed on."""
    return value if isinstance(value, Mapping) else {}


def _records(value: object) -> list[Any]:
    """A list-typed field as a list; a malformed value is kept as one opaque
    record (never dropped, so it can never buy a ``yes``)."""
    if value is None or value == []:
        return []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return [value]


def _is_warmup(err: Any) -> bool:
    return isinstance(err, Mapping) and str(err.get("phase", "")) == PHASE_WARMUP


def measured_errors(case: Mapping[str, Any]) -> list[Any]:
    return [e for e in _records(case.get("errors")) if not _is_warmup(e)]


def describe_error(err: Any) -> str:
    if not isinstance(err, Mapping):
        return f"malformed error record: {err!r}"
    return f"{err.get('failure_type')}: {err.get('message')}"


def _failure_type(err: Any) -> str:
    return str(err.get("failure_type")) if isinstance(err, Mapping) else "?"


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


# -------------------------------------------------------------- per case
def case_verdict(case: object, *, min_reps: int = MIN_VALID_REPS) -> Verdict:
    """``yes`` when every measured repetition succeeded with n >= min_reps."""
    if case is None:
        return Verdict(TBD, "case not run")
    if not isinstance(case, Mapping):
        return Verdict(MALFORMED, f"case entry is {type(case).__name__}, not an object")
    raw = case.get("samples")
    if raw is not None and not (
        isinstance(raw, Sequence) and not isinstance(raw, str | bytes)
    ):
        return Verdict(MALFORMED, f"samples is {type(raw).__name__}, not a list")
    samples = _records(raw)
    errors = measured_errors(case)
    if not samples:
        if errors and _failure_type(errors[0]) == SKIPPED_TYPE:
            reason = errors[0].get("message") if isinstance(errors[0], Mapping) else ""
            return Verdict(SKIPPED, str(reason or ""))
        if errors and _failure_type(errors[0]) in NOT_MEASURED_TYPES:
            return Verdict(TBD, f"not measured: {describe_error(errors[0])}")
        return Verdict(
            NO, describe_error(errors[0]) if errors else "no successful repetition"
        )
    if errors:
        return Verdict(
            f"flaky {len(samples)}/{len(samples) + len(errors)}",
            describe_error(errors[0]),
        )
    if len(samples) < min_reps:
        return Verdict(INVALID, f"only n={len(samples)} repetitions")
    return Verdict(YES)


def case_summary(case: object, metric: str) -> Summary | None:
    """The stored summary of one metric, or ``None`` when absent/malformed."""
    raw = _mapping(_mapping(case).get("summary")).get(metric)
    if not isinstance(raw, Mapping):
        return None
    try:
        return Summary.from_json(raw)
    except (TypeError, ValueError, AttributeError):
        return None


def cost_cell(case: object, metric: str) -> str:
    """``mean ± stdev (n=k)`` · ``**INVALID (n=k)** ...`` · ``skipped`` ·
    ``err: <failure_type>`` · ``INVALID (malformed)`` · ``TBD``."""
    if case is None:
        return TBD
    verdict = case_verdict(case)
    if verdict.value == SKIPPED:
        return SKIPPED
    if verdict.value == MALFORMED:
        return MALFORMED
    summary = case_summary(case, metric)
    if summary is None or summary.n == 0:
        errors = measured_errors(_mapping(case))
        return f"err: {_failure_type(errors[0])}" if errors else TBD
    text = f"{fmt(summary.mean, 2)} ± {fmt(summary.stdev, 2)} (n={summary.n})"
    if summary.n < MIN_VALID_REPS:
        return f"**INVALID (n={summary.n})** {text}"
    return text


def valid_mean(case: object, metric: str) -> float | None:
    """Mean of a metric, only when the row is valid (n >= 5, no failures)."""
    if case_verdict(case).value != YES:
        return None
    summary = case_summary(case, metric)
    if summary is None or not summary.valid:
        return None
    return summary.mean


# ------------------------------------------------------------ per laptop
@dataclass(slots=True)
class HostVerdict:
    host: str
    os_text: str
    gpu_on_host: bool | None
    docker: Verdict
    gpu_in_container: Verdict
    torch_cuda: Verdict
    recommendation: Verdict
    costs: dict[str, str] = field(default_factory=dict)
    calibration: str = ""
    docker_facts: dict[str, str] = field(default_factory=dict)
    quick: bool = False
    sources: list[str] = field(default_factory=list)
    timestamp: str = ""

    def fixes(self) -> list[str]:
        """Every cell that is not a clean result, with its detail."""
        out: list[str] = []
        for name, verdict in (
            ("docker", self.docker),
            ("gpu-in-container", self.gpu_in_container),
            ("torch-cuda-in-container", self.torch_cuda),
            ("runtime recommendation", self.recommendation),
        ):
            if verdict.value in (YES, USABLE, RECOMMEND_DOCKER, NOT_APPLICABLE):
                continue
            if verdict.value == RECOMMEND_VENV or verdict.detail:
                out.append(f"{name}: {verdict.cell()}")
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "os": self.os_text,
            "gpu_on_host": self.gpu_on_host,
            "docker": self.docker.to_json(),
            "gpu_in_container": self.gpu_in_container.to_json(),
            "torch_cuda_in_container": self.torch_cuda.to_json(),
            "recommendation": self.recommendation.to_json(),
            "costs": self.costs,
            "calibration": self.calibration,
            "docker_facts": self.docker_facts,
            "quick": self.quick,
            "sources": self.sources,
        }


def _os_text(doc: Mapping[str, Any]) -> str:
    facts = _mapping(_mapping(doc.get("extra")).get("facts"))
    host = _mapping(doc.get("host"))
    system = str(facts.get("system") or host.get("os") or "?")
    release = str(facts.get("release") or host.get("os_release") or "")
    suffix = " (WSL)" if facts.get("is_wsl") else ""
    return f"{system} {release}".strip() + suffix


def gpu_on_host(doc: Mapping[str, Any]) -> bool | None:
    """``extra.facts.gpu.present`` as a bool, ``None`` when unreadable."""
    gpu = _mapping(_mapping(_mapping(doc.get("extra")).get("facts")).get("gpu"))
    present = gpu.get("present")
    return present if isinstance(present, bool) else None


def _facts_failure(doc: Mapping[str, Any]) -> str:
    """``extra.docker``'s typed failure as text, or ``""`` when docker was ok
    (or the facts block is unreadable / a deliberate skip)."""
    dfacts = _mapping(_mapping(doc.get("extra")).get("docker"))
    if dfacts.get("ok") is True:
        return ""
    failure = dfacts.get("failure_type")
    if not isinstance(failure, str) or not failure or failure == SKIPPED_TYPE:
        return ""
    detail = f"{failure}: {dfacts.get('message')}"
    fix = dfacts.get("fix")
    return f"{detail} [fix: {fix}]" if isinstance(fix, str) and fix else detail


def docker_verdict(doc: Mapping[str, Any]) -> Verdict:
    """``usable`` from a clean docker-start; ``unusable`` from a failed one or
    from a typed ``docker info`` failure; anything else as measured."""
    cases = _mapping(doc.get("cases"))
    start = case_verdict(cases.get("docker-start"))
    facts_failure = _facts_failure(doc)
    if start.value == YES:
        dfacts = _mapping(_mapping(doc.get("extra")).get("docker"))
        if dfacts.get("ok") is True:
            return Verdict(USABLE)
        return Verdict(USABLE, "docker-start ran, but extra.docker.ok is not true (?)")
    if start.value == NO:
        return Verdict(UNUSABLE, facts_failure or start.detail)
    if start.value == TBD and facts_failure:
        return Verdict(UNUSABLE, facts_failure)
    return start  # skipped / flaky / INVALID / malformed / TBD, as measured


def gpu_verdict(doc: Mapping[str, Any], case_id: str) -> Verdict:
    present = gpu_on_host(doc)
    if present is False:
        return Verdict(NOT_APPLICABLE, "no NVIDIA GPU on host")
    verdict = case_verdict(_mapping(doc.get("cases")).get(case_id))
    if verdict.value == NO and "no_gpu_on_host" in verdict.detail:
        return Verdict(NOT_APPLICABLE, "no NVIDIA GPU on host")
    return verdict


def recommendation(
    docker: Verdict, gpu: Verdict, *, gpu_present: bool | None
) -> Verdict:
    """ADR-0004 runtime choice for this laptop (see module docstring)."""
    if docker.value == USABLE:
        if gpu.value == YES or gpu_present is False or gpu.value == NOT_APPLICABLE:
            return Verdict(RECOMMEND_DOCKER)
        if gpu.value == NO:
            return Verdict(
                RECOMMEND_VENV, f"GPU host but gpu-in-container: {gpu.cell()}"
            )
        return Verdict(TBD, f"docker usable but gpu-in-container is {gpu.cell()}")
    if docker.value == UNUSABLE:
        return Verdict(RECOMMEND_VENV, f"docker {docker.cell()}")
    if docker.value.startswith("flaky"):
        return Verdict(RECOMMEND_VENV, f"docker {docker.cell()}")
    return Verdict(TBD, f"docker is {docker.cell()}")


def calibration_line(doc: Mapping[str, Any], host: str) -> str:
    """V0.5 criterion 5: warm env job start overhead vs the <= 3 s target."""
    cases = _mapping(doc.get("cases"))
    venv_ms = valid_mean(cases.get("venv-start"), "start_to_exit_ms")
    docker_ms = valid_mean(cases.get("docker-start"), "start_to_exit_ms")
    torch_ms = valid_mean(cases.get("docker-gpu-torch"), "start_to_ready_ms")
    target = TARGET_START_OVERHEAD_S

    def part(name: str, ms: float | None, case_id: str, metric: str) -> str:
        if ms is None:
            return f"{name} {TBD} ({case_id}: {cost_cell(cases.get(case_id), metric)})"
        seconds = ms / 1000.0
        state = "within" if seconds <= target else "EXCEEDS"
        return f"{name} {seconds:.2f} s ({state})"

    text = (
        f"warm env job start overhead on {host}: "
        f"{part('venv', venv_ms, 'venv-start', 'start_to_exit_ms')}, "
        f"{part('container', docker_ms, 'docker-start', 'start_to_exit_ms')}"
        f" — vs the ≤ {target:g} s target"
    )
    if torch_ms is not None:
        text += (
            f"; PyTorch-CUDA container start to CUDA ready {torch_ms / 1000.0:.2f} s"
        )
    return text


def docker_facts_cells(doc: Mapping[str, Any]) -> dict[str, str]:
    """Short strings for the Docker facts table (``?`` when unreadable)."""
    dfacts = _mapping(_mapping(doc.get("extra")).get("docker"))
    facts = _mapping(_mapping(doc.get("extra")).get("facts"))
    gpu = _mapping(facts.get("gpu"))

    def text(value: object) -> str:
        if isinstance(value, list):
            return ", ".join(str(v) for v in value) or "—"
        return str(value) if value not in (None, "") else "?"

    versions = (
        f"{text(dfacts.get('client_version'))} / {text(dfacts.get('server_version'))}"
    )
    daemon_os = text(dfacts.get("operating_system"))
    if dfacts.get("docker_desktop"):
        daemon_os += (
            " (WSL2 backend)" if dfacts.get("wsl2_backend") else " (Hyper-V/other)"
        )
    runtimes = text(dfacts.get("runtimes"))
    if dfacts.get("nvidia_runtime") is True:
        runtimes += " (nvidia present)"
    elif dfacts.get("runtimes"):
        runtimes += " (no nvidia runtime key)"
    devices = gpu.get("devices")
    return {
        "versions": versions,
        "daemon_os": daemon_os,
        "kernel": text(dfacts.get("kernel_version")),
        "runtimes": runtimes,
        "cgroup": text(dfacts.get("cgroup_version")),
        "driver": text(dfacts.get("driver")),
        "wsl_host": "yes" if facts.get("is_wsl") else ("no" if facts else "?"),
        "host_gpu": text(devices)
        if isinstance(devices, list) and devices
        else ("none" if gpu.get("present") is False else "?"),
    }


def host_verdict(doc: Mapping[str, Any], host: str) -> HostVerdict:
    present = gpu_on_host(doc)
    docker = docker_verdict(doc)
    gpu = gpu_verdict(doc, "docker-gpu-smi")
    torch = gpu_verdict(doc, "docker-gpu-torch")
    cases = _mapping(doc.get("cases"))
    costs = {
        label: cost_cell(cases.get(case_id), metric)
        for case_id, metric, label in COST_ROWS
    }
    extra = _mapping(doc.get("extra"))
    return HostVerdict(
        host=host,
        os_text=_os_text(doc),
        gpu_on_host=present,
        docker=docker,
        gpu_in_container=gpu,
        torch_cuda=torch,
        recommendation=recommendation(docker, gpu, gpu_present=present),
        costs=costs,
        calibration=calibration_line(doc, host),
        docker_facts=docker_facts_cells(doc),
        quick=extra.get("quick") is True,
        sources=[str(doc.get("_source") or host_label(doc))],
        timestamp=str(doc.get("timestamp_utc") or ""),
    )


def display_name(doc: Mapping[str, Any], labels: Mapping[str, str] | None) -> str:
    """``labels`` (hostname -> short name) wins, case-insensitively (Windows
    hostnames are upper-case); else the label stored in the result."""
    host = _mapping(doc.get("host"))
    hostname = str(host.get("hostname", ""))
    folded = {k.casefold(): v for k, v in (labels or {}).items()}
    return folded.get(hostname.casefold()) or host_label(doc)


def host_verdicts(
    envelopes: Sequence[Mapping[str, Any]], labels: Mapping[str, str] | None = None
) -> list[HostVerdict]:
    """One verdict per laptop; the newest run (``timestamp_utc``) wins when a
    laptop has several files, and every file is listed in ``sources``.
    Laptops named in ``labels`` without a result are ``TBD`` rows."""
    by_host: dict[str, HostVerdict] = {}
    for doc in envelopes:
        name = display_name(doc, labels)
        candidate = host_verdict(doc, name)
        current = by_host.get(name)
        if current is None:
            by_host[name] = candidate
            continue
        keep, drop = (
            (candidate, current)
            if candidate.timestamp >= current.timestamp
            else (current, candidate)
        )
        keep.sources = sorted(set(keep.sources) | set(drop.sources))
        by_host[name] = keep
    for name in (labels or {}).values():
        by_host.setdefault(name, _tbd_host(name))
    return [by_host[k] for k in sorted(by_host)]


def _tbd_host(name: str) -> HostVerdict:
    tbd = Verdict(TBD, "no result file")
    return HostVerdict(
        host=name,
        os_text="?",
        gpu_on_host=None,
        docker=tbd,
        gpu_in_container=tbd,
        torch_cuda=tbd,
        recommendation=Verdict(TBD, "no result file"),
        costs={label: TBD for _c, _m, label in COST_ROWS},
        calibration=f"warm env job start overhead on {name}: {TBD} (no result file)",
        docker_facts={},
    )
