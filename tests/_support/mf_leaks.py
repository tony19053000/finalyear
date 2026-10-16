"""Where a credential could leak out of the browser (V0.8 G7, checklist 25).

The e2e ``new_page`` fixture attaches a :class:`LeakWatch` (one per test
session) to every page it opens. While the whole e2e run goes on it records:

* **places** a value must never reach - every console message (all types),
  every URL a page requested or navigated to (WebSockets included), and, at
  the end of each test, each open page's ``localStorage``, ``sessionStorage``,
  every IndexedDB record, ``document.cookie`` minus the CSRF companion (it is
  readable by design, D170) and ``window.name``;
* **values** that are credentials - read from the traffic itself, so no test
  has to register anything: the password of a successful ``POST /v1/session``
  or ``POST /v1/auth/login``, the token that call returns, the join code a
  ``POST /v1/fabric/invites`` returns, the raw body of a successful
  ``PUT /v1/secrets/{name}``, the HttpOnly ``mf_session`` cookie, plus the
  harness users' passwords and API tokens.

:meth:`LeakWatch.findings` then reports each place holding a known value or a
credential *shape* (``mfat_`` / ``mfbs_`` tokens, ``mf1-`` join codes, the
same shapes ``common.redaction`` scrubs). The gate test
``tests/e2e/test_v08_g7_zz_leak_gate.py`` runs last and asserts none, and
greps the built bundle as well.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Page, Response

#: Credential shapes (as ``common.redaction`` scrubs them): API tokens,
#: browser session tokens, join codes.
SHAPES: Final = (
    re.compile(r"mfat_[A-Za-z0-9_-]{16,}"),
    re.compile(r"mfbs_[A-Za-z0-9_-]{16,}"),
    re.compile(r"mf1-[A-Za-z0-9]{5}(?:-[A-Za-z0-9]{1,5}){3,}"),
)
#: A known value shorter than this is not searched for (a 4-byte test secret
#: such as "abcd" would match ordinary text); the shapes still apply.
MIN_KNOWN_CHARS: Final = 8
CSRF_COOKIE: Final = "mf_csrf"
SESSION_COOKIE: Final = "mf_session"

#: Everything a page script can keep, as one JSON string.
DUMP_JS: Final = """async () => {
  const idb = [];
  try {
    const dbs = indexedDB.databases ? await indexedDB.databases() : [];
    for (const info of dbs) {
      const db = await new Promise((ok, no) => {
        const r = indexedDB.open(info.name);
        r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
      });
      for (const store of db.objectStoreNames) {
        const rows = await new Promise((ok, no) => {
          const r = db.transaction(store).objectStore(store).getAll();
          r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
        });
        idb.push([info.name, store, rows]);
      }
      db.close();
    }
  } catch (e) { idb.push(["error", String(e)]); }
  const cookies = document.cookie.split("; ").filter((c) => !c.startsWith("mf_csrf="));
  return JSON.stringify({
    localStorage: Object.entries(localStorage),
    sessionStorage: Object.entries(sessionStorage),
    indexedDB: idb,
    cookies,
    windowName: window.name,
    href: location.href,
  });
}"""


@dataclasses.dataclass
class LeakWatch:
    places: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    """(where, text) pairs a credential must never be in."""
    known: set[str] = dataclasses.field(default_factory=set)
    pages: int = 0
    snapshots: int = 0

    # ------------------------------------------------------------ recording

    def add_known(self, value: str | None) -> None:
        if value and len(value) >= MIN_KNOWN_CHARS:
            self.known.add(value)

    def attach(self, page: Page, test: str) -> None:
        self.pages += 1
        page.on(
            "console",
            lambda m: self.places.append((f"{test}: console.{m.type}", m.text)),
        )
        page.on(
            "request", lambda r: self.places.append((f"{test}: request URL", r.url))
        )
        page.on(
            "websocket",
            lambda ws: self.places.append((f"{test}: WebSocket URL", ws.url)),
        )
        page.on(
            "framenavigated",
            lambda f: self.places.append((f"{test}: navigated URL", f.url)),
        )
        page.on("response", self._learn)

    def _learn(self, response: Response) -> None:
        """Remember the credential a successful call carried or returned."""
        if not 200 <= response.status < 300:
            return
        request = response.request
        path = urlsplit(response.url).path
        method = request.method
        try:
            if method == "POST" and path in ("/v1/session", "/v1/auth/login"):
                body = json.loads(request.post_data or "{}")
                self.add_known(body.get("password"))
                if path == "/v1/auth/login":
                    self.add_known(response.json().get("token"))
            elif method == "POST" and path == "/v1/fabric/invites":
                self.add_known(response.json().get("code"))
            elif method == "PUT" and path.startswith("/v1/secrets/"):
                raw = request.post_data_buffer or b""
                self.add_known(raw.decode("utf-8", "ignore").strip())
        except Exception as exc:  # noqa: BLE001 - recorded: an unreadable body is a gap
            self.places.append(("leak watch", f"could not read {path}: {exc}"))

    def snapshot(self, page: Page, test: str) -> None:
        """Record what the page keeps (at the end of a test), and learn the
        HttpOnly session cookie of its context."""
        if page.is_closed():
            return
        for cookie in page.context.cookies():
            if cookie["name"] == SESSION_COOKIE:
                self.add_known(cookie["value"])
        try:
            dump = str(page.evaluate(DUMP_JS))
        except Exception as exc:  # noqa: BLE001 - a page mid-navigation
            dump = f"(not readable: {exc})"
        self.snapshots += 1
        self.places.append((f"{test}: storage of {page.url}", dump))

    # ------------------------------------------------------------ judging

    def findings(self, extra: list[tuple[str, str]] | None = None) -> list[str]:
        """Each place that holds a known credential or a credential shape.
        URLs are also checked decoded (``%2D`` etc. hide nothing)."""
        found: list[str] = []
        for where, text in [*self.places, *(extra or [])]:
            for candidate in {text, unquote(text)}:
                if any(value in candidate for value in self.known):
                    # never print the value, not even a part of it
                    found.append(f"{where}: holds a known credential")
                for shape in SHAPES:
                    match = shape.search(candidate)
                    if match:
                        found.append(f"{where}: holds a {match.group(0)[:4]}... shape")
        return sorted(set(found))
