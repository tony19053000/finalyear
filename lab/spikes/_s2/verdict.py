"""Turn S2 result envelopes into a machine-readable verdict per direction.

One run measures one direction: the client dials the server, so the direction
key is ``"<client> -> <server>"``. A pair of laptops needs two runs (one each
way); a direction nobody ran is reported **TBD**, never guessed.

Verdict values: ``yes`` · ``no`` (with the diagnosis and the fix) ·
``flaky k/n`` · ``INVALID (n<5)`` (measured, but not enough repetitions to be
a lab result) · ``INVALID (malformed)`` (the result file's case entry is not a
usable object) · ``TBD`` (not tested).

Only **measured** repetitions decide a verdict. ``run_reps`` records warmup and
measured failures in one ``errors`` list, tagged with ``phase``; warmup samples
"never enter the summary" and warmup failures likewise never demote a case that
ran every requested repetition cleanly. They are not dropped either: they are
surfaced as ``warmup_notes`` on the direction, under "Diagnoses and fixes".
An error with no ``phase`` at all is counted as measured - the conservative
reading, so no unknown input can buy a ``yes``.

A row is also judged on **what it can prove**: a run whose client and server
were the same machine (loopback address, or identical hostnames) is marked
``same machine`` in the table and its outbound-only verdict stays ``TBD`` -
a loopback path crosses no firewall. A run whose two sides report the same
operating system is not a cross-OS result and says so; neither flag changes a
reachability cell, they only stop a pasted table from passing as evidence it
is not.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import host_label
from spikes._harness.report import cell, fmt, link_name
from spikes._harness.reps import PHASE_WARMUP

YES: Final = "yes"
NO: Final = "no"
TBD: Final = "TBD"
INVALID: Final = f"INVALID (n<{MIN_VALID_REPS})"
#: A result file whose case entry is not an object at all (hand-edited, or
#: written by a buggier version): reported, never crashed on, never ``yes``.
MALFORMED: Final = "INVALID (malformed)"
#: Suffix on the ``link`` cell of a row measured against the same machine.
SAME_MACHINE_MARK: Final = "⚠ same machine"
#: Detail of the outbound-only ``TBD`` on a same-machine run.
LOOPBACK_DETAIL: Final = "loopback path crosses no firewall"
#: Detail of an outbound-only ``yes``: what was declared vs what is built in.
OUTBOUND_YES_DETAIL: Final = (
    "operator declared no inbound rule (--no-inbound-rule); "
    "client binds no listening socket by construction"
)

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


def _error_records(case: Mapping[str, Any]) -> list[Any]:
    """Every recorded failure of a case, however malformed.

    A malformed ``errors`` value is never *dropped*: it is kept as one opaque
    failure, so no unreadable result file can buy a ``yes``.
    """
    errors = case.get("errors")
    if errors is None or errors == []:
        return []
    if isinstance(errors, Sequence) and not isinstance(errors, str | bytes):
        return list(errors)
    return [errors]


def _is_warmup(err: Any) -> bool:
    """Warmup failures do not count; anything unlabelled counts as measured."""
    return isinstance(err, Mapping) and str(err.get("phase", "")) == PHASE_WARMUP


def measured_errors(case: Mapping[str, Any]) -> list[Any]:
    """The failures of *requested* (measured) repetitions - what a cell judges."""
    return [e for e in _error_records(case) if not _is_warmup(e)]


def warmup_errors(case: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Failures of warmup repetitions: reported, but never part of a verdict."""
    return [e for e in _error_records(case) if _is_warmup(e)]


def describe_error(err: Any) -> str:
    if not isinstance(err, Mapping):
        return f"malformed error record: {err!r}"
    return f"{err.get('failure_type')}: {err.get('message')}"


def _first_failure(case: Mapping[str, Any]) -> str:
    """The first *measured* failure; a warmup-only failure as a last resort."""
    errors = measured_errors(case) or _error_records(case)
    return describe_error(errors[0]) if errors else ""


def _malformed(case: object) -> Verdict:
    """A case entry that is not an object at all: say so, never crash on it."""
    return Verdict(
        MALFORMED,
        f"result file is malformed: the case entry is "
        f"{type(case).__name__}, not an object",
    )


def case_verdict(case: object, *, min_reps: int = MIN_VALID_REPS) -> Verdict:
    """``yes`` when every requested (measured) repetition succeeded, else why not.

    ``case`` is typed ``object`` on purpose: it comes straight from a JSON
    result file, and anything that is not a mapping is judged, not crashed on.
    """
    if case is None:
        return Verdict(TBD, "case not run")
    if not isinstance(case, Mapping):
        return _malformed(case)
    samples = case.get("samples") or []
    if not samples:
        return Verdict(NO, _first_failure(case) or "no successful repetition")
    errors = measured_errors(case)
    if errors:
        return Verdict(
            f"flaky {len(samples)}/{len(samples) + len(errors)}",
            describe_error(errors[0]),
        )
    if len(samples) < min_reps:
        return Verdict(INVALID, f"only n={len(samples)} repetitions")
    return Verdict(YES)


def session_verdict(case: object, *, min_reps: int = MIN_VALID_REPS) -> Verdict:
    """Did the long-lived bidi stream survive the idle gap, every time?"""
    if case is None:
        return Verdict(TBD, "case not run")
    if not isinstance(case, Mapping):
        return _malformed(case)
    samples = case.get("samples") or []
    if not samples:
        return Verdict(NO, _first_failure(case) or "no successful repetition")
    survived = [s for s in samples if isinstance(s, Mapping) and s.get("survived")]
    dropped = [s for s in samples if not (isinstance(s, Mapping) and s.get("survived"))]
    if not survived:
        return Verdict(NO, _drop_detail(dropped))
    if dropped:
        return Verdict(f"flaky {len(survived)}/{len(samples)}", _drop_detail(dropped))
    errors = measured_errors(case)
    if errors:
        return Verdict(
            f"flaky {len(samples)}/{len(samples) + len(errors)}",
            describe_error(errors[0]),
        )
    if len(samples) < min_reps:
        return Verdict(INVALID, f"only n={len(samples)} repetitions")
    return Verdict(YES)


def _drop_detail(dropped: Sequence[Any]) -> str:
    """Why the idle stream died, and what to change about it."""
    first = dropped[0] if dropped else None
    failure = (first.get("failure") or {}) if isinstance(first, Mapping) else {}
    message = str(failure.get("message") or "stream did not survive the idle gap")
    fix = failure.get("fix")
    return f"{message} [fix: {fix}]" if fix else message


def _mean(case: Any, metric: str) -> float | None:
    if not isinstance(case, Mapping):
        return None
    summary = _mapping(_mapping(case.get("summary")).get(metric))
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
    #: client and server were one machine (loopback address / same hostname)
    same_host: bool = False
    #: the two sides reported *different* operating systems (both known)
    cross_os: bool = True
    cases: dict[str, Verdict] = field(default_factory=dict)
    sessions: dict[str, Verdict] = field(default_factory=dict)
    outbound_only: Verdict = Verdict(TBD)
    metrics: dict[str, float | None] = field(default_factory=dict)
    run_errors: list[str] = field(default_factory=list)
    #: warmup-only failures: reported for honesty, never part of a verdict
    warmup_notes: list[str] = field(default_factory=list)
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

    def notes(self) -> list[str]:
        """Things worth knowing that do **not** change any verdict."""
        return list(self.warmup_notes)

    def caveats(self) -> list[str]:
        """Why this row cannot answer the spike's question, if it cannot.

        These never change a cell: a loopback run really did reach its peer.
        They say what the row is evidence *of* - and ADR-0009 needs a native,
        cross-OS, two-machine run.
        """
        out: list[str] = []
        if self.same_host:
            out.append("not a cross-OS result: same machine (loopback)")
        if not self.cross_os:
            pair = f"{self.client_os} -> {self.server_os}"
            if _known_os(self.client_os) and _known_os(self.server_os):
                out.append(f"same OS on both sides ({pair}) — does not answer ADR-0009")
            else:
                out.append(
                    f"OS of one side not recorded ({pair}) — cannot count as a "
                    "cross-OS result"
                )
        if not self.native_pair:
            out.append(
                f"one side is not native ({self.client_os} -> {self.server_os}); "
                "ADR-0009 needs a native run."
            )
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "client": self.client,
            "server": self.server,
            "link": self.link,
            "client_os": self.client_os,
            "server_os": self.server_os,
            "native_pair": self.native_pair,
            "same_host": self.same_host,
            "cross_os": self.cross_os,
            "caveats": self.caveats(),
            "cases": {k: v.to_json() for k, v in self.cases.items()},
            "sessions": {k: v.to_json() for k, v in self.sessions.items()},
            "outbound_only": self.outbound_only.to_json(),
            "metrics": self.metrics,
            "fixes": self.fixes(),
            "warmup_notes": self.warmup_notes,
            "sources": self.sources,
        }


def _mapping(value: object) -> Mapping[str, Any]:
    """``value`` if it is an object, else empty: a malformed result file is
    reported as unknown facts, never crashed on."""
    return value if isinstance(value, Mapping) else {}


def _os_text(facts: Mapping[str, Any]) -> str:
    system = str(facts.get("system") or facts.get("os") or "?")
    release = str(facts.get("release") or facts.get("os_release") or "")
    wsl = (facts.get("wsl") or {}) if isinstance(facts.get("wsl"), dict) else {}
    suffix = " (WSL)" if wsl.get("is_wsl") else ""
    return f"{system} {release}".strip() + suffix


def _native(facts: Mapping[str, Any]) -> bool:
    wsl = facts.get("wsl")
    return not (isinstance(wsl, dict) and wsl.get("is_wsl"))


def _known_os(text: str) -> bool:
    return bool(text) and not text.startswith("?")


def _system(facts: Mapping[str, Any]) -> str:
    return str(facts.get("system") or facts.get("os") or "").strip()


def _is_loopback(address: object) -> bool:
    """``127.x`` / ``::1`` / ``localhost``; anything unparseable is not."""
    text = str(address or "").strip()
    if not text:
        return False
    if text.casefold() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _hostname(facts: Mapping[str, Any]) -> str:
    return str(facts.get("hostname") or "").strip().casefold()


def is_same_host(doc: Mapping[str, Any]) -> bool:
    """Were client and server the same machine?

    True when the dialled address is loopback, when the client's own address
    towards the peer *is* the peer's address, or when both sides report the
    same (non-empty) hostname. Hostnames are compared case-insensitively
    (Windows reports upper case). Nothing here is a guess: every input is a
    recorded fact from the envelope.
    """
    extra = _mapping(doc.get("extra"))
    endpoint = _mapping(extra.get("endpoint"))
    remote = endpoint.get("remote_address")
    local = endpoint.get("local_address")
    if _is_loopback(remote):
        return True
    if remote and local and str(remote).strip() == str(local).strip():
        return True
    host = doc.get("host") or {}
    peer = extra.get("peer_identity") or {}
    if not isinstance(host, Mapping) or not isinstance(peer, Mapping):
        return False
    mine, theirs = _hostname(host), _hostname(peer)
    return bool(mine) and mine == theirs


def is_cross_os(local: Mapping[str, Any], peer: Mapping[str, Any]) -> bool:
    """Both operating systems known **and** different (``Linux`` vs ``Windows``).

    An unknown side is conservatively *not* cross-OS: a row that cannot show
    which two systems it joined cannot be evidence for ADR-0009.
    """
    mine, theirs = _system(local), _system(peer)
    return bool(mine) and bool(theirs) and mine.casefold() != theirs.casefold()


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
    extra = _mapping(doc.get("extra"))
    params = _mapping(doc.get("params"))
    raw_cases = doc.get("cases")
    cases: dict[str, Any] = dict(raw_cases) if isinstance(raw_cases, Mapping) else {}
    client = relabel(host_label(doc), labels)
    server = relabel(
        str(params.get("server_name") or params.get("server") or "?"), labels
    )
    local = _mapping(extra.get("local"))
    peer = _mapping(extra.get("peer_identity"))
    verdict = DirectionVerdict(
        direction=f"{client} -> {server}",
        client=client,
        server=server,
        link=str(doc.get("link", "")),
        client_os=_os_text(local),
        server_os=_os_text(peer),
        client_native=_native(local),
        server_native=_native(peer),
        same_host=is_same_host(doc),
        cross_os=is_cross_os(local, peer),
        sources=[str(doc.get("_source", ""))] if doc.get("_source") else [],
    )
    for case_id, _ in FIXED_CASES:
        verdict.cases[case_id] = case_verdict(cases.get(case_id))
    for case_id in sorted(cases):
        if case_id.startswith("grpc-bidi-"):
            verdict.sessions[case_id] = session_verdict(cases[case_id])
    verdict.warmup_notes = _warmup_notes(cases)
    verdict.outbound_only = _outbound_verdict(extra, verdict)
    verdict.metrics = {
        "http_ttfb_ms": _mean(cases.get("http-ping"), "ttfb_ms"),
        "http_mbps": _mean(cases.get("http-blob"), "mbps"),
        "grpc_rtt_ms": _mean(cases.get("grpc-unary"), "rtt_ms"),
        "grpc_mbps": _mean(cases.get("grpc-stream"), "mbps"),
    }
    verdict.run_errors = [
        describe_error(e) + (f" [fix: {e['fix']}]" if e.get("fix") else "")
        for e in doc.get("errors") or []
        if isinstance(e, Mapping)
    ]
    return verdict


def _warmup_notes(cases: Mapping[str, Any]) -> list[str]:
    """Warmup failures, per case: never a verdict, but never hidden either."""
    notes: list[str] = []
    for case_id in sorted(cases):
        case = cases[case_id]
        if not isinstance(case, Mapping):
            continue
        notes.extend(
            f"{case_id}: warmup repetition {err.get('rep')} failed "
            f"({describe_error(err)}); warmup is excluded from the verdict"
            for err in warmup_errors(case)
        )
    return notes


def _outbound_verdict(extra: Mapping[str, Any], verdict: DirectionVerdict) -> Verdict:
    """ADR-0002: the client needs no inbound port. Proven only when the
    operator confirms no inbound rule was added *and* the gRPC cases worked
    *and* there was a firewall in the path at all (never on one machine)."""
    if verdict.same_host:
        return Verdict(TBD, LOOPBACK_DETAIL)
    info = _mapping(extra.get("outbound_only"))
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
        return Verdict(YES, OUTBOUND_YES_DETAIL)
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


def link_cell(verdict: DirectionVerdict) -> str:
    """The ``link`` cell, with the same-machine marker a reader cannot miss."""
    text = link_name(verdict.link)
    return f"{text} {SAME_MACHINE_MARK}" if verdict.same_host else text


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
            link_cell(verdict),
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
            f"| {cell(verdict.direction)} | {cell(link_cell(verdict))} | "
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
    notes = [
        f"- **{cell(verdict.direction)}** (note, not counted) {cell(note)}"
        for verdict in rows
        for note in verdict.notes()
    ]
    caveats = [
        f"- **{cell(verdict.direction)}**: {cell(caveat)}"
        for verdict in rows
        for caveat in verdict.caveats()
    ]
    body = "\n".join(items + caveats + notes) or "- none"
    return "#### Diagnoses and fixes\n\n" + body + "\n"
