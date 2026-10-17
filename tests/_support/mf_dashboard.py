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
    # By UTF-8 bytes = code points; build-stamp.mjs sorts the same way (its
    # default JS sort would compare UTF-16 units and differ for astral names).
    return sorted(found, key=lambda rel: rel.encode("utf-8", "surrogateescape"))


def sources_digest(frontend: Path = FRONTEND) -> str:
    """SHA-256 of the dashboard's build inputs (build-stamp.mjs algorithm)."""
    outer = hashlib.sha256()
    for rel in _stamp_sources(frontend):
        inner = hashlib.sha256((frontend / rel).read_bytes()).hexdigest()
        outer.update(f"{rel}\0{inner}\n".encode("utf-8", "surrogateescape"))
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


#: Console lines no ``allow_matching`` pattern may match (G7 tester r1 T-2):
#: an application error and HTTP 4xx/5xx answers. A pattern is for transport
#: noise (an outage's refused connections); an HTTP status is expected with
#: :meth:`PageWatch.allow`, which names the one resource.
NOISE_CANARIES: Final = (
    "",
    "an unrelated application error @ https://h/assets/index-abc.js",
    "Uncaught TypeError: x is not a function @ https://h/assets/index-abc.js",
    "Failed to load resource: the server responded with a status of 401 (Unauthorized) @ https://h/v1/nodes",
    "Failed to load resource: the server responded with a status of 404 (Not Found) @ https://h/v1/jobs/x",
    "Failed to load resource: the server responded with a status of 500 (Internal Server Error) @ https://h/v1/jobs",
    "Failed to load resource: net::ERR_HTTP_RESPONSE_CODE_FAILURE @ https://h/v1/jobs",
    "WebSocket connection to 'wss://h/v1/events/ws' failed: Error during WebSocket handshake: "
    "Unexpected response code: 500 @ https://h/assets/index-abc.js",
)
#: Shortest ``allow`` text: a one- or two-letter fragment is in almost any line.
MIN_ALLOW_TEXT: Final = 3
#: Shortest ``allow`` URL suffix without a ``/`` (T-2c): "s" ends every resource.
MIN_ALLOW_SUFFIX: Final = 3
#: The console line of a failed WebSocket names its socket only in the TEXT;
#: Chromium leaves the line's location URL empty or the script's (G7 tester r2
#: T-2d), so the socket URL in the text is (also) the line's resource.
_WS_LINE: Final = re.compile(r"^WebSocket connection to '([^']*)'")


def line_resources(text: str, location_url: str) -> tuple[str, ...]:
    """The resource URLs a console line is about: its location URL and, for a
    WebSocket connection line, the socket URL named in the text (without query
    or fragment, like a path the test can name)."""
    found = [location_url]
    socket_line = _WS_LINE.match(text)
    if socket_line is not None:
        found.append(re.split(r"[?#]", socket_line.group(1), maxsplit=1)[0])
    return tuple(found)


@dataclasses.dataclass(eq=False)
class Allowed:
    """One expected console error: its text contains ``text`` and the
    resource that caused it ends with ``url_suffix``. ``used`` counts the
    lines it excused; one never used is reported (T-2) unless the test said
    the line may not happen (``may_not_occur``, a timing-dependent line)."""

    text: str
    url_suffix: str
    reason: str = ""
    may_not_occur: bool = False
    used: int = 0


@dataclasses.dataclass(eq=False)
class Noise:
    """Expected console errors matching ``pattern`` (judged when checked)."""

    pattern: re.Pattern[str]
    reason: str
    may_not_occur: bool = False


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
    noise: list[Noise] = dataclasses.field(default_factory=list)
    """Console errors matching one of these are expected (an outage's network
    errors); filtered when checked, so it may be declared at any time."""
    final_axe: bool = True
    disarmed: str | None = None

    @classmethod
    def attach(cls, page: Page) -> PageWatch:
        watch = cls(page)
        page.add_init_script(CSP_LISTENER)
        watch._follow(page)
        return watch

    def _follow(self, page: Page) -> None:
        """Judge ``page``'s console and uncaught errors - and those of every
        popup it opens (G7 tester r1 T-7b), recursively - in this watch.
        (A popup's CSP violations and final axe state are not judged.)"""
        page.on("console", self._on_console)
        page.on("pageerror", lambda exc: self.page_errors.append(str(exc)))
        page.on("popup", self._follow)

    def allow(
        self,
        text: str,
        url_suffix: str,
        reason: str = "",
        *,
        may_not_occur: bool = False,
    ) -> None:
        """Expect console errors whose text contains ``text`` and whose
        resource URL ends with ``url_suffix`` (for a WebSocket line: the
        socket URL its text names, :func:`line_resources`; a suffix without
        ``/`` needs >= 3 characters, T-2c). Judged as each line arrives, so
        declare it BEFORE the action. ``reason`` is required; an empty or
        trivial text or suffix (which would excuse everything) is refused
        (G7 tester r1 T-2), and an allowance that excused nothing is reported
        at the end unless ``may_not_occur``."""
        if not reason.strip():
            raise ValueError("watch.allow needs a reason")
        if len(text.strip()) < MIN_ALLOW_TEXT:
            raise ValueError(f"watch.allow text {text!r} would match almost any line")
        suffix = url_suffix.strip()
        if suffix in ("", "/"):
            raise ValueError("watch.allow needs the resource (URL suffix) it excuses")
        if "/" not in suffix and len(suffix) < MIN_ALLOW_SUFFIX:
            # G7 tester r2 T-2c: "s" or "e" ends almost every resource URL.
            raise ValueError(
                f"watch.allow suffix {url_suffix!r} is no resource: give a path "
                f"('/...') or at least {MIN_ALLOW_SUFFIX} characters"
            )
        self.allowed.append(Allowed(text, url_suffix, reason, may_not_occur))

    def allow_matching(
        self, pattern: str, reason: str = "", *, may_not_occur: bool = False
    ) -> None:
        """Expect console errors whose text matches ``pattern`` (regex), for
        transport noise such as an outage's refused connections. ``reason`` is
        required; a pattern that matches an empty line, an application error
        or an HTTP 4xx/5xx line (:data:`NOISE_CANARIES`) is refused (T-2), and
        a pattern that matched nothing is reported unless ``may_not_occur``."""
        if not reason.strip():
            raise ValueError("watch.allow_matching needs a reason")
        compiled = re.compile(pattern)
        hidden = [c for c in NOISE_CANARIES if compiled.search(c)]
        if hidden:
            raise ValueError(
                f"watch.allow_matching({pattern!r}) would also hide {hidden[0]!r}"
            )
        self.noise.append(Noise(compiled, reason, may_not_occur))

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
            if not any(n.pattern.search(line) for n in self.noise)
        ]

    def unused_allowances(self) -> list[str]:
        """Allowances that excused no line (and were not marked optional): a
        stale or mistyped allowance - the G7 item-1 bug - is reported (T-2)."""
        unused = [
            f"allow({a.text!r}, {a.url_suffix!r}) - {a.reason}"
            for a in self.allowed
            if a.used == 0 and not a.may_not_occur
        ]
        unused += [
            f"allow_matching({n.pattern.pattern!r}) - {n.reason}"
            for n in self.noise
            if not n.may_not_occur
            and not any(n.pattern.search(line) for line in self.console_errors)
        ]
        return unused

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type not in ("error", "assert"):
            return
        url = str(message.location.get("url", ""))
        resources = line_resources(message.text, url)
        for allowed in self.allowed:
            if allowed.text in message.text and any(
                r.endswith(allowed.url_suffix) for r in resources
            ):
                allowed.used += 1
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
        problems += [f"allowance never used: {a}" for a in self.unused_allowances()]
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
