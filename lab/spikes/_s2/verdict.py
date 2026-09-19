"""Turn S2 result envelopes into a machine-readable verdict per direction.

One run measures one direction: the client dials the server, so the direction
key is ``"<client> -> <server>"``. A pair of laptops needs two runs (one each
way); a direction nobody ran is reported **TBD**, never guessed.

Verdict values: ``yes`` · ``no`` (with the diagnosis and the fix) ·
``flaky k/n`` · ``INVALID (n<5)`` (measured, but not enough repetitions to be
a lab result) · ``TBD`` (not tested).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import host_label
from spikes._harness.report import cell, fmt, link_name

YES: Final = "yes"
NO: Final = "no"
TBD: Final = "TBD"
INVALID: Final = f"INVALID (n<{MIN_VALID_REPS})"

#: The four fixed cases every direction is judged on, in report order.
FIXED_CASES: Final[tuple[tuple[str, str], ...]] = (
    ("http-ping", "HTTP GET"),
    ("http-blob", "HTTP stream"),
    ("grpc-unary", "gRPC unary"),
    ("grpc-stream", "gRPC server-stream"),
)


@dataclass(frozen=True, slots=True)
class Verdict:
    value: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.value == YES

    def cell(self) -> str:
        return f"{self.value}{f' — {self.detail}' if self.detail else ''}"

    def to_json(self) -> dict[str, str]:
        return {"value": self.value, "detail": self.detail}


def _first_failure(case: Mapping[str, Any]) -> str:
    errors = case.get("errors") or []
    if not errors:
        return ""
    err = errors[0]
    return f"{err.get('failure_type')}: {err.get('message')}"


def case_verdict(
    case: Mapping[str, Any] | None, *, min_reps: int = MIN_VALID_REPS
) -> Verdict:
    """``yes`` when every requested repetition succeeded, else why not."""
    if case is None:
        return Verdict(TBD, "case not run")
    samples = case.get("samples") or []
    if not samples:
        return Verdict(NO, _first_failure(case) or "no successful repetition")
    if case.get("errors"):
        return Verdict(
            f"flaky {len(samples)}/{len(samples) + len(case['errors'])}",
            _first_failure(case),
        )
    if len(samples) < min_reps:
        return Verdict(INVALID, f"only n={len(samples)} repetitions")
    return Verdict(YES)


def session_verdict(
    case: Mapping[str, Any] | None, *, min_reps: int = MIN_VALID_REPS
) -> Verdict:
    """Did the long-lived bidi stream survive the idle gap, every time?"""
    if case is None:
        return Verdict(TBD, "case not run")
    samples = case.get("samples") or []
    if not samples:
        return Verdict(NO, _first_failure(case) or "no successful repetition")
    survived = [s for s in samples if s.get("survived")]
    dropped = [s for s in samples if not s.get("survived")]
    if not survived:
        return Verdict(NO, _drop_detail(dropped))
    if dropped:
        return Verdict(f"flaky {len(survived)}/{len(samples)}", _drop_detail(dropped))
    if case.get("errors"):
        return Verdict(
            f"flaky {len(samples)}/{len(samples) + len(case['errors'])}",
            _first_failure(case),
        )
    if len(samples) < min_reps:
        return Verdict(INVALID, f"only n={len(samples)} repetitions")
    return Verdict(YES)


def _drop_detail(dropped: Sequence[Mapping[str, Any]]) -> str:
    """Why the idle stream died, and what to change about it."""
    failure = (dropped[0].get("failure") or {}) if dropped else {}
    message = str(failure.get("message") or "stream did not survive the idle gap")
    fix = failure.get("fix")
    return f"{message} [fix: {fix}]" if fix else message


def _mean(case: Mapping[str, Any] | None, metric: str) -> float | None:
    if case is None:
        return None
    summary = (case.get("summary") or {}).get(metric) or {}
    value = summary.get("mean")
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


@dataclass(slots=True)
class DirectionVerdict:
    """Everything the report says about one direction (client -> server)."""

    direction: str
    client: str
    server: str
    link: str
    client_os: str
    server_os: str
    client_native: bool
    server_native: bool
    cases: dict[str, Verdict] = field(default_factory=dict)
    sessions: dict[str, Verdict] = field(default_factory=dict)
    outbound_only: Verdict = Verdict(TBD)
    metrics: dict[str, float | None] = field(default_factory=dict)
    run_errors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def native_pair(self) -> bool:
        return self.client_native and self.server_native

    def fixes(self) -> list[str]:
        """The actionable diagnoses of everything that is not ``yes``."""
        out: list[str] = []
        for name, verdict in {**self.cases, **self.sessions}.items():
            if verdict.value != YES and verdict.detail:
                out.append(f"{name}: {verdict.detail}")
        return out + self.run_errors

    def to_json(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "client": self.client,
            "server": self.server,
            "link": self.link,
            "client_os": self.client_os,
            "server_os": self.server_os,
            "native_pair": self.native_pair,
            "cases": {k: v.to_json() for k, v in self.cases.items()},
            "sessions": {k: v.to_json() for k, v in self.sessions.items()},
            "outbound_only": self.outbound_only.to_json(),
            "metrics": self.metrics,
            "fixes": self.fixes(),
            "sources": self.sources,
        }


def _os_text(facts: Mapping[str, Any]) -> str:
    system = str(facts.get("system") or facts.get("os") or "?")
    release = str(facts.get("release") or facts.get("os_release") or "")
    wsl = (facts.get("wsl") or {}) if isinstance(facts.get("wsl"), dict) else {}
    suffix = " (WSL)" if wsl.get("is_wsl") else ""
    return f"{system} {release}".strip() + suffix


def _native(facts: Mapping[str, Any]) -> bool:
    wsl = facts.get("wsl")
    return not (isinstance(wsl, dict) and wsl.get("is_wsl"))


def relabel(name: str, labels: Mapping[str, str] | None) -> str:
    """Report label for a recorded host name (``--hosts LABEL=HOST``).

    Matching is case-insensitive because Windows reports its hostname in
    upper case while the operator usually types it in lower case; an unknown
    name keeps the name it was recorded under, never a guessed label.
    """
    if not labels:
        return name
    return labels.get(name.casefold(), name)


def direction_verdict(
    doc: Mapping[str, Any], labels: Mapping[str, str] | None = None
) -> DirectionVerdict:
    """Judge one result envelope (one direction, one link)."""
    extra = doc.get("extra") or {}
    params = doc.get("params") or {}
    cases: dict[str, Any] = doc.get("cases") or {}
    client = relabel(host_label(doc), labels)
    server = relabel(
        str(params.get("server_name") or params.get("server") or "?"), labels
    )
    local = extra.get("local") or {}
    peer = extra.get("peer_identity") or {}
    verdict = DirectionVerdict(
        direction=f"{client} -> {server}",
        client=client,
        server=server,
        link=str(doc.get("link", "")),
        client_os=_os_text(local),
        server_os=_os_text(peer),
        client_native=_native(local),
        server_native=_native(peer),
        sources=[str(doc.get("_source", ""))] if doc.get("_source") else [],
    )
    for case_id, _ in FIXED_CASES:
        verdict.cases[case_id] = case_verdict(cases.get(case_id))
    for case_id in sorted(cases):
        if case_id.startswith("grpc-bidi-"):
            verdict.sessions[case_id] = session_verdict(cases[case_id])
    verdict.outbound_only = _outbound_verdict(extra, verdict)
    verdict.metrics = {
        "http_ttfb_ms": _mean(cases.get("http-ping"), "ttfb_ms"),
        "http_mbps": _mean(cases.get("http-blob"), "mbps"),
        "grpc_rtt_ms": _mean(cases.get("grpc-unary"), "rtt_ms"),
        "grpc_mbps": _mean(cases.get("grpc-stream"), "mbps"),
    }
    verdict.run_errors = [
        f"{e.get('failure_type')}: {e.get('message')}"
        + (f" [fix: {e['fix']}]" if e.get("fix") else "")
        for e in doc.get("errors") or []
    ]
    return verdict


def _outbound_verdict(extra: Mapping[str, Any], verdict: DirectionVerdict) -> Verdict:
    """ADR-0002: the client needs no inbound port. Proven only when the
    operator confirms no inbound rule was added *and* the gRPC cases worked."""
    info = extra.get("outbound_only") or {}
    unary = verdict.cases.get("grpc-unary", Verdict(TBD))
    sessions = list(verdict.sessions.values())
    if not info.get("no_inbound_rule_declared"):
        return Verdict(
            TBD, "run the client with --no-inbound-rule to record this claim"
        )
    parts = {
        "gRPC unary": _status([unary]),
        "the long-lived session": _status(sessions),
    }
    if all(status == "ok" for status in parts.values()):
        return Verdict(YES, "no listening socket and no inbound rule on the client")
    if any(status == "failed" for status in parts.values()):
        return Verdict(NO, "gRPC unary and/or the long-lived session did not work")
    # Nothing failed, but nothing proves it either: a case that never ran (or
    # ran too few times) must not be reported as a model that was tested and
    # failed - nor as one that worked.
    unknown = [name for name, status in parts.items() if status != "ok"]
    return Verdict(TBD, f"{' and '.join(unknown)} produced no valid result")


def _status(verdicts: Sequence[Verdict]) -> str:
    """``ok`` (at least one clean pass) · ``failed`` (something was measured and
    did not work) · ``unknown`` (only TBD / INVALID - never tested properly)."""
    if any(v.ok for v in verdicts):
        return "ok"
    if any(v.value == NO or v.value.startswith("flaky") for v in verdicts):
        return "failed"
    return "unknown"


def direction_verdicts(
    envelopes: Iterable[Mapping[str, Any]],
    labels: Mapping[str, str] | None = None,
) -> dict[str, DirectionVerdict]:
    """Verdicts keyed by ``"<client> -> <server>"`` + link.

    When the same direction and link was measured more than once (a re-run
    under a different file name, or two result folders), the **newest** run by
    ``timestamp_utc`` wins - not whichever file the directory listing produced
    last - and every file that covered the direction is kept in ``sources`` so
    the report can say which run the row came from. Ties keep the first seen.

    ``labels`` (case-folded host name -> report label) renames the hosts, so
    the rows use the same names as the expected-direction list.
    """
    out: dict[str, DirectionVerdict] = {}
    stamps: dict[str, str] = {}
    for doc in envelopes:
        verdict = direction_verdict(doc, labels)
        key = f"{verdict.direction} [{verdict.link}]"
        stamp = str(doc.get("timestamp_utc") or "")
        previous = out.get(key)
        if previous is None:
            out[key], stamps[key] = verdict, stamp
            continue
        sources = previous.sources + verdict.sources
        if stamp > stamps[key]:
            verdict.sources = sources
            out[key], stamps[key] = verdict, stamp
        else:
            previous.sources = sources
    return out


def expected_directions(labels: Sequence[str]) -> list[str]:
    """Every ordered pair of the given hosts (A -> B and B -> A)."""
    return [f"{a} -> {b}" for a in labels for b in labels if a != b]


def _session_columns(verdicts: Iterable[DirectionVerdict]) -> list[str]:
    seen: list[str] = []
    for verdict in verdicts:
        for case_id in verdict.sessions:
            if case_id not in seen:
                seen.append(case_id)
    return sorted(seen)


def render_verdicts(
    verdicts: Mapping[str, DirectionVerdict], expected: Sequence[str] = ()
) -> str:
    """The markdown block the report and ``docs/LAB.md`` §3 quote."""
    rows = [verdicts[k] for k in sorted(verdicts)]
    session_cols = _session_columns(rows)
    columns = [
        "direction",
        "link",
        "client OS",
        "server OS",
        *[label for _, label in FIXED_CASES],
        *[cell(c.replace("grpc-bidi-", "bidi ")) for c in session_cols],
        "outbound-only",
    ]
    header = "| " + " | ".join(columns) + " |\n|" + "---|" * len(columns) + "\n"
    lines: list[str] = []
    for verdict in rows:
        # Every cell below is free text from a result file (host labels, OS
        # strings, OS error messages): a literal "|" or newline in one of them
        # would silently shift the whole table, so they all go through cell().
        cells = [
            verdict.direction,
            link_name(verdict.link),
            verdict.client_os,
            verdict.server_os,
            *[verdict.cases[c].cell() for c, _ in FIXED_CASES],
            *[verdict.sessions.get(c, Verdict(TBD)).cell() for c in session_cols],
            verdict.outbound_only.cell(),
        ]
        lines.append("| " + " | ".join(cell(c) for c in cells) + " |")
    tested = {v.direction for v in rows}
    lines += [
        "| " + " | ".join([cell(direction), *([TBD] * (len(columns) - 1))]) + " |"
        for direction in expected
        if direction not in tested
    ]
    body = "\n".join(lines) + "\n" if lines else "| (no results) |\n"
    metrics = _render_metrics(rows)
    fixes = _render_fixes(rows)
    return (
        "#### Reachability verdicts (per direction: client → server)\n\n"
        + header
        + body
        + "\n"
        + metrics
        + fixes
    )


def _render_metrics(rows: Sequence[DirectionVerdict]) -> str:
    text = (
        "#### Latency and throughput (means)\n\n"
        "| direction | link | HTTP TTFB (ms) | HTTP stream (Mbit/s) | "
        "gRPC unary RTT (ms) | gRPC stream (Mbit/s) |\n"
        "|---|---|---|---|---|---|\n"
    )
    for verdict in rows:
        m = verdict.metrics
        text += (
            f"| {cell(verdict.direction)} | {cell(link_name(verdict.link))} | "
            f"{fmt(m.get('http_ttfb_ms'))} | {fmt(m.get('http_mbps'), 1)} | "
            f"{fmt(m.get('grpc_rtt_ms'))} | {fmt(m.get('grpc_mbps'), 1)} |\n"
        )
    return text + ("" if rows else "| (no results) | | | | | |\n") + "\n"


def _render_fixes(rows: Sequence[DirectionVerdict]) -> str:
    items = [
        f"- **{cell(verdict.direction)}** {cell(fix)}"
        for verdict in rows
        for fix in verdict.fixes()
    ]
    non_native = [
        f"- **{cell(verdict.direction)}**: one side is not native "
        f"({cell(verdict.client_os)} -> {cell(verdict.server_os)}); "
        "ADR-0009 needs a native run."
        for verdict in rows
        if not verdict.native_pair
    ]
    body = "\n".join(items + non_native) or "- none"
    return "#### Diagnoses and fixes\n\n" + body + "\n"
