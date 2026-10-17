"""``python scripts/ui_perf`` / ``python -m scripts.ui_perf``: seed a running
fabric, measure, print the table and the verdict (see the package docstring)."""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import socket
import ssl
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from .clocks import MAX_CLOCK_BOUND_MS
from .measure import run
from .report import EXIT_CODES, check, machine, table, verdict
from .scenarios import MIXED_CANCELLED, Api, seed_bulk, seed_history, tiny_project

if TYPE_CHECKING:
    from playwright.sync_api import Browser

DESCRIPTION: Final = (
    "Dashboard performance under load (V0.8 G7, D174; skew-safe V0.9 G0)."
)


def ssl_context(ca_file: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_file))


def spki_pin(base: str, context: ssl.SSLContext) -> str:
    """base64 SHA-256 of the controller's public key, after verifying its
    certificate against the fabric CA (Chromium then trusts exactly it)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    parts = urlsplit(base)
    host, port = parts.hostname or "", parts.port or 443
    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    if der is None:
        raise RuntimeError("the controller presented no certificate")
    spki = (
        x509.load_der_x509_certificate(der)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


@contextlib.contextmanager
def chromium(base: str, verify: ssl.SSLContext) -> Iterator[Browser]:
    from playwright.sync_api import sync_playwright

    pin = spki_pin(base, verify)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[f"--ignore-certificate-errors-spki-list={pin}"]
        )
        try:
            yield browser
        finally:
            browser.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ui_perf", description=DESCRIPTION)
    parser.add_argument("--controller", required=True, help="https://host:port")
    parser.add_argument("--ca", type=Path, required=True, help="the fabric ca.crt")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--history", type=int, default=100, help="jobs per node")
    parser.add_argument("--bulk", type=int, default=10_000)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--out", type=Path, help="write the results as JSON here")
    args = parser.parse_args(argv)
    password, token = os.environ.get("MF_PASSWORD"), os.environ.get("MF_TOKEN")
    if not password or not token:
        print("set MF_PASSWORD (browser sign-in) and MF_TOKEN (admin API token)")
        return 2
    base = args.controller.rstrip("/")
    verify = ssl_context(args.ca)
    api = Api(base, token, verify)
    phases: dict[str, dict[str, Any]] = {}
    seeds: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tree_id = api.upload_project(tiny_project(Path(tmp)))
        with chromium(base, verify) as browser:
            if not args.skip_seed:
                seeds["history"] = dataclasses.asdict(
                    seed_history(api, tree_id, args.history)
                )
                seeds["mixed"] = dataclasses.asdict(
                    seed_bulk(api, tree_id, MIXED_CANCELLED)
                )
            phases["history"] = run(
                browser, api, base=base, user=args.user, password=password,
                tree_id=tree_id, with_events=True,
            )  # fmt: skip
            if args.bulk and not args.skip_seed:
                seeds["bulk"] = dataclasses.asdict(seed_bulk(api, tree_id, args.bulk))
                phases["bulk"] = run(
                    browser, api, base=base, user=args.user, password=password,
                    tree_id=tree_id, with_events=False,
                )  # fmt: skip
    finally:
        api.close()
    outcome = verdict(phases)
    result = {
        "machine": machine(),
        "seeds": seeds,
        "phases": phases,
        "verdict": outcome,
    }
    problems = [p for phase in phases.values() for p in check(phase)]
    print(table(phases))
    print(json.dumps({"machine": result["machine"], "seeds": seeds}, indent=2))
    for name, phase in phases.items():
        if "events" in phase:
            print(f"clock ({name}):", json.dumps(phase["events"]["clock"]))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for problem in problems:
        print("THRESHOLD BROKEN:", problem)
    if outcome == "skew_uncertain":
        print(
            "SKEW UNCERTAIN: the browser<->controller clock offset is not known "
            f"within {MAX_CLOCK_BOUND_MS} ms; the live-latency numbers neither "
            "pass nor fail - re-run with the browser on a wired link or on the "
            "controller's host"
        )
    print("VERDICT:", outcome)
    return EXIT_CODES[outcome]
