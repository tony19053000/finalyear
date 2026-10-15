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
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import socket
import ssl
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Page

REPO: Final = Path(__file__).resolve().parents[2]
DIST: Final = REPO / "src" / "meshforge" / "controller" / "web" / "dist"
AXE_JS: Final = REPO / "frontend" / "node_modules" / "axe-core" / "axe.min.js"

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
    page: Page
    console_errors: list[str] = dataclasses.field(default_factory=list)
    page_errors: list[str] = dataclasses.field(default_factory=list)
    allowed: list[Allowed] = dataclasses.field(default_factory=list)

    @classmethod
    def attach(cls, page: Page) -> PageWatch:
        watch = cls(page)
        page.add_init_script(CSP_LISTENER)
        page.on("console", watch._on_console)
        page.on("pageerror", lambda exc: watch.page_errors.append(str(exc)))
        return watch

    def allow(self, text: str, url_suffix: str) -> None:
        self.allowed.append(Allowed(text, url_suffix))

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
        assert not self.console_errors, f"console errors: {self.console_errors}"
        assert not self.page_errors, f"uncaught page errors: {self.page_errors}"
        assert not violations, f"CSP violations: {violations}"


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
          }));
        }"""
    )
    return list(result)
