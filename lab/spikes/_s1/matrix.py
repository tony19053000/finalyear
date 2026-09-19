"""Aggregate S1 result envelopes into the docs/LAB.md §2 tables (pure functions).

* Wi-Fi and Ethernet are processed separately and never mixed (``link`` of each
  envelope, given by the operator).
* Bandwidth: rows = sender, columns = receiver; the cell is the **receiver
  view**, mean ± stdev over repetitions. A client run fills two cells
  (upload = client -> server, download = server -> client). If several runs
  cover the same directed pair (e.g. A ran against B and B against A), their
  repetitions are pooled and the cell's ``n`` says how many there are.
* RTT / jitter / loss per unordered pair: mean ± stdev of the per-repetition
  mean RTT, mean jitter, mean loss %, pooled over both directions' runs.
* A cell with n < 5 is ``INVALID (n=k)``; a pair nobody measured is ``TBD``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import LINKS, host_label
from spikes._harness.report import fmt, link_name
from spikes._harness.stats import Summary, summarize

SPIKE: Final = "S1"
#: Relative sender/receiver difference above which both views are reported.
VIEW_DIFF_THRESHOLD: Final = 0.05
#: A cell above this share of the slower host's loopback ceiling may be tool-limited.
CEILING_SHARE: Final = 0.5

type Env = dict[str, Any]
type BwKey = tuple[str, int, str, str]  # link, streams, sender, receiver
type PairKey = tuple[str, str, str]  # link, host a, host b (sorted)


@dataclass(slots=True)
class Pools:
    """Raw repetition values per matrix cell, keyed by link."""

    receiver: dict[BwKey, list[float]]
    sender: dict[BwKey, list[float]]
    rtt: dict[PairKey, list[float]]
    jitter: dict[PairKey, list[float]]
    loss: dict[PairKey, list[float]]
    hosts: set[str]
    streams: dict[str, set[int]]


def _nums(samples: Iterable[Mapping[str, Any]], key: str) -> list[float]:
    return [
        float(s[key])
        for s in samples
        if isinstance(s.get(key), int | float) and not isinstance(s.get(key), bool)
    ]


def collect(envelopes: Sequence[Env]) -> Pools:
    pools = Pools(
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
        set(),
        defaultdict(set),
    )
    for doc in envelopes:
        if doc.get("spike") != SPIKE or doc.get("link") not in LINKS:
            continue
        link = doc["link"]
        pools.hosts.add(client_name(doc))
        for case in doc["cases"].values():
            attrs = case.get("attrs", {})
            samples = case.get("samples", [])
            if attrs.get("kind") == "tcp":
                sender, receiver = str(attrs["sender"]), str(attrs["receiver"])
                streams = int(attrs["streams"])
                pools.hosts.update((sender, receiver))
                pools.streams[link].add(streams)
                key = (link, streams, sender, receiver)
                pools.receiver[key] += _nums(samples, "receiver_mbps")
                pools.sender[key] += _nums(samples, "sender_mbps")
            elif attrs.get("kind") == "udp":
                a, b = sorted((str(attrs["client"]), str(attrs["server"])))
                pools.hosts.update((a, b))
                pkey = (link, a, b)
                pools.rtt[pkey] += _nums(samples, "rtt_mean_ms")
                pools.jitter[pkey] += _nums(samples, "jitter_ms")
                pools.loss[pkey] += _nums(samples, "loss_pct")
    return pools


def client_name(doc: Env) -> str:
    """The client's matrix identity: ``params.client_name`` (``--name``), else
    the hostname recorded in the envelope."""
    name = (doc.get("params") or {}).get("client_name")
    return str(name) if isinstance(name, str) and name else host_label(doc)


def _natural(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def order_hosts(hosts: Iterable[str], labels: Mapping[str, str]) -> list[str]:
    """Labelled hosts first (by label, naturally: L2 < L10), then the rest."""
    everything = set(hosts) | set(labels)
    return sorted(
        everything,
        key=lambda h: (0, _natural(labels[h])) if h in labels else (1, _natural(h)),
    )


def display(host: str, labels: Mapping[str, str]) -> str:
    return labels.get(host, host)


def cell(values: Sequence[float] | None, digits: int = 1) -> str:
    if not values:
        return "TBD"
    s = summarize(values)
    if s.n < MIN_VALID_REPS:
        return f"INVALID (n={s.n})"
    return f"{fmt(s.mean, digits)} ± {fmt(s.stdev, digits)} (n={s.n})"


def mean_cell(values: Sequence[float] | None, digits: int = 2) -> str:
    if not values:
        return "TBD"
    s = summarize(values)
    if s.n < MIN_VALID_REPS:
        return f"INVALID (n={s.n})"
    return fmt(s.mean, digits)


def _ceilings(envelopes: Sequence[Env]) -> dict[str, float]:
    """Best loopback ceiling (Mbit/s, receiver view) measured on each host."""
    best: dict[str, float] = {}
    for doc in envelopes:
        ceiling = (doc.get("extra") or {}).get("loopback_ceiling") or {}
        value = ceiling.get("receiver_mbps") if ceiling.get("ok", True) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            host = client_name(doc)
            best[host] = max(best.get(host, 0.0), float(value))
    return best


def bandwidth_table(
    pools: Pools,
    link: str,
    streams: int,
    hosts: Sequence[str],
    labels: Mapping[str, str],
) -> str:
    head = "| from \\ to | " + " | ".join(display(h, labels) for h in hosts) + " |"
    lines = [head, "|---|" + "---|" * len(hosts)]
    for sender in hosts:
        cells = [
            "—"
            if sender == receiver
            else cell(pools.receiver.get((link, streams, sender, receiver)))
            for receiver in hosts
        ]
        lines.append(f"| {display(sender, labels)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pair_table(
    pools: Pools, link: str, hosts: Sequence[str], labels: Mapping[str, str]
) -> str:
    lines = ["| pair | RTT (ms) | jitter (ms) | loss (%) |", "|---|---|---|---|"]
    for i, a in enumerate(hosts):
        for b in hosts[i + 1 :]:
            x, y = sorted((a, b))
            key = (link, x, y)
            name = f"{display(a, labels)}–{display(b, labels)}"  # noqa: RUF001 (LAB.md)
            lines.append(
                f"| {name} | {cell(pools.rtt.get(key), 2)} | "
                f"{mean_cell(pools.jitter.get(key), 3)} | {mean_cell(pools.loss.get(key), 2)} |"
            )
    return "\n".join(lines)


def view_differences(pools: Pools, link: str, labels: Mapping[str, str]) -> list[str]:
    rows: list[str] = []
    for key in sorted(k for k in pools.receiver if k[0] == link):
        recv, send = (
            summarize(pools.receiver[key]),
            summarize(pools.sender.get(key, [])),
        )
        if recv.mean is None or send.mean is None or recv.mean <= 0:
            continue
        diff = (send.mean - recv.mean) / recv.mean
        if abs(diff) > VIEW_DIFF_THRESHOLD:
            _, streams, s, r = key
            rows.append(
                f"| {display(s, labels)}→{display(r, labels)} x{streams} | "
                f"{fmt(recv.mean, 1)} | {fmt(send.mean, 1)} | {diff * 100:+.1f}% |"
            )
    return rows


def ceiling_warnings(
    pools: Pools, link: str, ceilings: Mapping[str, float], labels: Mapping[str, str]
) -> list[str]:
    out: list[str] = []
    for key in sorted(k for k in pools.receiver if k[0] == link):
        _, streams, s, r = key
        known = [ceilings[h] for h in (s, r) if h in ceilings]
        mean = summarize(pools.receiver[key]).mean
        if known and mean is not None and mean > CEILING_SHARE * min(known):
            out.append(
                f"- {display(s, labels)}→{display(r, labels)} x{streams}: "
                f"{fmt(mean, 1)} Mbit/s is over {CEILING_SHARE:.0%} of the slower host's "
                f"loopback ceiling ({fmt(min(known), 1)} Mbit/s): the Python tool may "
                "be the limit; confirm with iperf3."
            )
    return out


def _iperf_rows(envelopes: Sequence[Env], labels: Mapping[str, str]) -> list[str]:
    rows: list[str] = []
    for doc in envelopes:
        server = str((doc.get("params") or {}).get("server_name") or "?")
        client = client_name(doc)
        for run in (doc.get("extra") or {}).get("iperf3", []) or []:
            if not isinstance(run, dict):
                continue
            up = run.get("direction") == "upload"
            s, r = (client, server) if up else (server, client)
            result = (
                f"{fmt(run.get('receiver_mbps'), 1)} | {fmt(run.get('sender_mbps'), 1)}"
                if run.get("ok")
                else f"error: {run.get('error')} | —"
            )
            rows.append(
                f"| {link_name(doc['link'])} | {display(s, labels)}→{display(r, labels)} "
                f"x{run.get('streams')} | {result} |"
            )
    return rows


def render_matrix(
    envelopes: Sequence[Env], labels: Mapping[str, str] | None = None
) -> str:
    """Markdown for both links: bandwidth matrices, pair table, honesty notes."""
    labels = dict(labels or {})
    pools = collect(envelopes)
    hosts = order_hosts(pools.hosts, labels)
    ceilings = _ceilings(envelopes)
    out: list[str] = ["### Network matrices (docs/LAB.md §2)", ""]
    if labels:
        legend = ", ".join(f"{labels[h]} = {h}" for h in hosts if h in labels)
        out += [f"Host labels: {legend}.", ""]
    out += [
        "Cells: mean ± stdev (n = measured repetitions, warmup excluded). "
        f"n < {MIN_VALID_REPS} is INVALID; TBD = not measured. "
        "Bandwidth is TCP goodput counted at the receiver, row = sender.",
        "",
    ]
    for link in LINKS:
        out += [f"#### {link_name(link)}", ""]
        streams_seen = sorted(pools.streams.get(link, set())) or [1]
        for streams in streams_seen:
            noun = "stream" if streams == 1 else "parallel streams"
            out += [
                f"**Bandwidth (Mbit/s, TCP, {streams} {noun}, row = sender)**",
                "",
                bandwidth_table(pools, link, streams, hosts, labels),
                "",
            ]
        out += [
            "**RTT (ms, mean ± stdev of per-rep means) / jitter (ms) / loss (%)**",
            "",
            pair_table(pools, link, hosts, labels),
            "",
        ]
        diffs = view_differences(pools, link, labels)
        if diffs:
            out += [
                f"Sender and receiver views differ by more than "
                f"{VIEW_DIFF_THRESHOLD:.0%} here:",
                "",
                "| direction | receiver view (Mbit/s) | sender view (Mbit/s) | diff |",
                "|---|---|---|---|",
                *diffs,
                "",
            ]
        elif any(k[0] == link for k in pools.receiver):
            out += [
                f"Sender and receiver views agree within {VIEW_DIFF_THRESHOLD:.0%} "
                "in every measured cell.",
                "",
            ]
        warnings = ceiling_warnings(pools, link, ceilings, labels)
        if warnings:
            out += [*warnings, ""]
    out += ["#### Tool ceiling (loopback, this tool, 1 TCP stream)", ""]
    if ceilings:
        out += ["| host | receiver view (Mbit/s) |", "|---|---|"]
        out += [
            f"| {display(h, labels)} | {fmt(ceilings[h], 1)} |"
            for h in sorted(ceilings)
        ]
    else:
        out.append("Not measured (client run with `--ceiling-seconds 0`).")
    out.append("")
    iperf_rows = _iperf_rows(envelopes, labels)
    out += ["#### iperf3 cross-check (single run each, not part of the matrix)", ""]
    if iperf_rows:
        out += [
            "| link | direction | receiver (Mbit/s) | sender (Mbit/s) |",
            "|---|---|---|---|",
            *iperf_rows,
        ]
    else:
        out.append("Not run (iperf3 not used or not installed).")
    out.append("")
    return "\n".join(out)


def summary_of(values: Sequence[float]) -> Summary:
    return summarize(values)
