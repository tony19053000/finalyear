"""Browser-side helpers for the dashboard e2e tier (V0.8 G2, D179).

* :func:`spki_pin` - the base64 SHA-256 of the controller's TLS public key, for
  Chromium's ``--ignore-certificate-errors-spki-list``. Chromium does not read
  ``SSL_CERT_FILE``; instead of ``ignore_https_errors`` (any certificate) the
  browser accepts exactly the key this test's controller presents, after the
  harness itself verified that certificate against the fabric CA.
* :class:`PageWatch` - records console errors, uncaught page errors and CSP
  violations; :meth:`PageWatch.assert_clean` fails the test on any of them
  except the few a test names exactly (e.g. the browser's own "Failed to load
  resource: 401" line for an expected login refusal).
* :func:`axe_violations` - runs axe-core (vendored by ``frontend/node_modules``)
  in the page through the DevTools protocol (not a page script: the CSP still
  forbids inline script for the app itself).
* :func:`dist_problem` / :func:`require_fresh_dist` (V0.8 G7, D206) - the
  build is git-ignored, so a checkout can hold one made from OLDER frontend
  sources. ``npm run build`` stamps ``dist/build-info.json`` with a digest of
  the sources (``frontend/scripts/build-stamp.mjs``); every test that reads the
  build compares it with the sources on disk and skips with "rebuild" as the
  reason, never failing on a symptom of a stale bundle.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import socket
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Page

REPO: Final = Path(__file__).resolve().parents[2]
FRONTEND: Final = REPO / "frontend"
DIST: Final = REPO / "src" / "meshforge" / "controller" / "web" / "dist"
AXE_JS: Final = REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js"
BUILD_STAMP: Final = "build-info.json"
REBUILD: Final = "run `cd frontend && npm ci && npm run build`"

#: Build inputs, exactly as ``frontend/scripts/build-stamp.mjs`` lists them.
_STAMP_TOP_FILES: Final = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
)
_STAMP_TREES: Final = ("public", "src")
_TEST_SOURCE: Final = re.compile(r"\.test\.tsx?$")


def _stamp_sources(frontend: Path) -> list[str]:
    found = [name for name in _STAMP_TOP_FILES if (frontend / name).is_file()]
    for tree in _STAMP_TREES:
        for path in (frontend / tree).rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(frontend).as_posix()
            if rel.startswith("src/test/") or _TEST_SOURCE.search(rel):
                continue
            found.append(rel)
    # JavaScript's default sort compares UTF-16 code units; for the ASCII paths
    # of this tree that is the same order as Python's.
    return sorted(found)


def sources_digest(frontend: Path = FRONTEND) -> str:
    """SHA-256 of the dashboard's build inputs (build-stamp.mjs algorithm)."""
    outer = hashlib.sha256()
    for rel in _stamp_sources(frontend):
        inner = hashlib.sha256((frontend / rel).read_bytes()).hexdigest()
        outer.update(f"{rel}\0{inner}\n".encode())
    return outer.hexdigest()


def dist_problem(dist: Path = DIST, frontend: Path = FRONTEND) -> str | None:
    """``None`` when ``dist`` holds a build of the frontend sources now on
    disk; otherwise a one-line reason that says what to run."""
    if not (dist / "index.html").is_file():
        return f"the dashboard is not built - {REBUILD}"
    stamp_path = dist / BUILD_STAMP
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        built_from = str(stamp["sources_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return (
            f"the dashboard build has no valid {BUILD_STAMP} (made before V0.8 G7 "
            f"or by hand), so it may be stale - {REBUILD}"
        )
    if built_from != sources_digest(frontend):
        return (
            "the dashboard build is STALE: it was built from other frontend "
            f"sources than the ones on disk - {REBUILD}"
        )
    return None


def require_fresh_dist(dist: Path = DIST) -> Path:
    """Skip the calling test, with the reason, unless ``dist`` is a fresh build."""
    import pytest

    problem = dist_problem(dist)
    if problem is not None:
        pytest.skip(problem)
    return dist


#: Collects CSP violations from the first moment of every document.
CSP_LISTENER: Final = """
window.__mfCsp = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.__mfCsp.push({directive: e.violatedDirective, blocked: e.blockedURI, sample: e.sample});
});
"""


def spki_pin(host: str, port: int, context: ssl.SSLContext) -> str:
    """Connect with ``context`` (which must verify the fabric CA), and return
    the base64 SHA-256 of the leaf certificate's SubjectPublicKeyInfo."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    assert der is not None, "the controller presented no certificate"
    spki = (
        x509.load_der_x509_certificate(der)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


@dataclasses.dataclass(frozen=True)
class Allowed:
    """One expected console error: its text contains ``text`` and the
    resource that caused it ends with ``url_suffix``."""

    text: str
    url_suffix: str


@dataclasses.dataclass
class PageWatch:
    """What one page logged. V0.8 G7: the e2e ``new_page`` fixture checks
    every watch it made when the test ends (:meth:`final_problems`) - console
    errors, uncaught errors, CSP violations and axe-core on the page's final
    state - so no test can forget to; a test states its expectations with
    :meth:`allow` / :meth:`allow_matching` / :meth:`skip_final_axe` /
    :meth:`disarm`, each with a reason in the test."""

    page: Page
    console_errors: list[str] = dataclasses.field(default_factory=list)
    page_errors: list[str] = dataclasses.field(default_factory=list)
    allowed: list[Allowed] = dataclasses.field(default_factory=list)
    noise: list[re.Pattern[str]] = dataclasses.field(default_factory=list)
    """Console errors matching one of these are expected (an outage's network
    errors); filtered when checked, so it may be declared at any time."""
    final_axe: bool = True
    disarmed: str | None = None

    @classmethod
    def attach(cls, page: Page) -> PageWatch:
        watch = cls(page)
        page.add_init_script(CSP_LISTENER)
        page.on("console", watch._on_console)
        page.on("pageerror", lambda exc: watch.page_errors.append(str(exc)))
        return watch

    def allow(self, text: str, url_suffix: str) -> None:
        self.allowed.append(Allowed(text, url_suffix))

    def allow_matching(self, pattern: str) -> None:
        """Expect console errors whose text matches ``pattern`` (regex)."""
        self.noise.append(re.compile(pattern))

    def skip_final_axe(self, reason: str) -> None:
        """The page ends in a state axe must not judge (``reason`` says why)."""
        assert reason
        self.final_axe = False

    def disarm(self, reason: str) -> None:
        """A negative control: this page is dirty on purpose; the end-of-test
        check skips it (``reason`` says why)."""
        assert reason
        self.disarmed = reason

    def unexpected_console_errors(self) -> list[str]:
        return [
            line
            for line in self.console_errors
            if not any(p.search(line) for p in self.noise)
        ]

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type not in ("error", "assert"):
            return
        url = str(message.location.get("url", ""))
        for allowed in self.allowed:
            if allowed.text in message.text and url.endswith(allowed.url_suffix):
                return
        self.console_errors.append(f"{message.text} @ {url}")

    def csp_violations(self) -> list[dict[str, Any]]:
        found = self.page.evaluate("window.__mfCsp || []")
        return list(found)

    def assert_clean(self) -> None:
        violations = self.csp_violations()
        errors = self.unexpected_console_errors()
        assert not errors, f"console errors: {errors}"
        assert not self.page_errors, f"uncaught page errors: {self.page_errors}"
        assert not violations, f"CSP violations: {violations}"

    def final_problems(self) -> list[str]:
        """Everything wrong with this page at the end of a test (V0.8 G7):
        console/uncaught errors and, while the page is open, CSP violations
        and axe-core findings on its final state. Empty when disarmed."""
        if self.disarmed is not None:
            return []
        problems = [f"console error: {e}" for e in self.unexpected_console_errors()]
        problems += [f"uncaught page error: {e}" for e in self.page_errors]
        if self.page.is_closed():
            return problems
        where = self.page.url
        problems += [f"CSP violation on {where}: {v}" for v in self.csp_violations()]
        if self.final_axe and where.startswith("https://"):
            problems += [
                f"axe violation on {where} (final state): {v}"
                for v in axe_violations(self.page)
            ]
        return problems


def axe_violations(page: Page) -> list[dict[str, Any]]:
    """axe-core's WCAG 2.x A/AA violations on the current page."""
    page.evaluate(AXE_JS.read_text(encoding="utf-8"))
    result = page.evaluate(
        """async () => {
          const r = await axe.run(document, {
            runOnly: {type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
          });
          return r.violations.map((v) => ({
            id: v.id, impact: v.impact, help: v.help,
            nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ")),
            why: v.nodes.slice(0, 3).map((n) => n.failureSummary),
          }));
        }"""
    )
    return list(result)
