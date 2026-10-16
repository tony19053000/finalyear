"""Where a credential could leak out of the browser (V0.8 G7, checklist 25).

The e2e ``new_page`` fixture attaches a :class:`LeakWatch` (one per test
session) to every page it opens. While the whole e2e run goes on it records:

* **places** a value must never reach - every console message (all types),
  every URL a page requested or navigated to (WebSockets included), every
  WRITE a page script makes to web storage, IndexedDB, ``document.cookie``,
  Cache Storage and ``history`` state (recorded as it happens, so a value
  stored and removed again is still seen - fix round 1, T-7), and, at the end
  of each test, each page's ``localStorage``, ``sessionStorage``, every
  IndexedDB record, Cache Storage, ``history.state``, ``document.cookie``
  minus the CSRF companion (it is readable by design, D170) and
  ``window.name`` (a page closed before then: its context's storage, T-7);
  popups and other tabs of a watched context are watched too (T-7b);
* **values** that are credentials - read from the traffic itself, so no test
  has to register anything: the password of a successful ``POST /v1/session``
  or ``POST /v1/auth/login``, the token that call returns, the join code a
  ``POST /v1/fabric/invites`` returns, the raw body of a successful
  ``PUT /v1/secrets/{name}``, the HttpOnly ``mf_session`` cookie, plus the
  harness users' passwords and API tokens.

:meth:`LeakWatch.findings` then reports each place holding a known value or a
credential *shape* (``mfat_`` / ``mfbs_`` tokens, ``mf1-`` join codes, the
same shapes ``common.redaction`` scrubs). A known value is searched in each
form a page could hold it in (T-6): as is, JSON-escaped (once and twice - a
storage dump is JSON, and a stored value may be JSON itself), base64
(standard and URL-safe), and each line of a multi-line value on its own; in
URLs also percent-decoded. A finding names the place, never the value: its
label is redacted too (T-10).

Judging is independent of test order (T-1): ``tests/e2e/conftest.py`` fails
the session (an error at the session fixture's teardown, and a non-zero exit
from ``pytest_sessionfinish``) when the evidence of ANY subset of e2e tests
holds a finding. ``tests/e2e/test_v08_g7_zz_leak_gate.py`` adds its own
credential-making flow and greps the built bundle.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Response

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

#: The page-side name of the write recorder (a Playwright binding).
WRITE_BINDING: Final = "__mfLeakWrite"
#: Records every write a page script makes to a place a credential could stay
#: in, as it happens (T-7): a value stored and removed before the test ends,
#: or kept only by a page closed meanwhile, is still seen. Wraps the real
#: methods and calls through, so the page behaves exactly as before.
WRITE_HOOK_JS: Final = """(() => {
  const send = (kind, text) => {
    try { window.__mfLeakWrite(kind, String(text)).catch(() => {}); } catch (e) {}
  };
  const json = (v) => { try { return JSON.stringify(v); } catch (e) { return String(v); } };
  const wrap = (proto, name, report) => {
    const real = proto && proto[name];
    if (typeof real !== "function") return;
    proto[name] = function (...args) {
      try { report(this, args); } catch (e) {}
      return real.apply(this, args);
    };
  };
  wrap(Storage.prototype, "setItem", (self, [k, v]) => send(
    self === window.sessionStorage ? "sessionStorage write" : "localStorage write",
    `${k}=${v}`,
  ));
  for (const name of ["put", "add"]) {
    wrap(window.IDBObjectStore && IDBObjectStore.prototype, name, (self, [v, k]) =>
      send("IndexedDB write", `${self.name}: ${json(k)} ${json(v)}`));
  }
  wrap(window.Cache && Cache.prototype, "put", (self, [req, resp]) => {
    const url = typeof req === "string" ? req : (req && req.url);
    send("Cache Storage write", url);
    if (resp && resp.clone) {
      resp.clone().text().then((t) => send("Cache Storage write", t), () => {});
    }
  });
  for (const name of ["pushState", "replaceState"]) {
    wrap(History.prototype, name, (self, [state, title, url]) =>
      send("history write", `${json(state)} ${url === undefined ? "" : url}`));
  }
  const cookie = Object.getOwnPropertyDescriptor(Document.prototype, "cookie");
  if (cookie && cookie.set) {
    Object.defineProperty(Document.prototype, "cookie", {
      configurable: true, enumerable: cookie.enumerable,
      get() { return cookie.get.call(this); },
      set(v) {
        if (!String(v).startsWith("mf_csrf=")) send("cookie write", v);
        cookie.set.call(this, v);
      },
    });
  }
})();"""

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
  const cached = [];
  try {
    if (window.caches) {
      for (const name of await caches.keys()) {
        const cache = await caches.open(name);
        for (const req of await cache.keys()) {
          const resp = await cache.match(req);
          cached.push([name, req.url, resp ? (await resp.text()).slice(0, 262144) : null]);
        }
      }
    }
  } catch (e) { cached.push(["error", String(e)]); }
  let state;
  try { state = JSON.stringify(history.state); } catch (e) { state = String(history.state); }
  const cookies = document.cookie.split("; ").filter((c) => !c.startsWith("mf_csrf="));
  return JSON.stringify({
    localStorage: Object.entries(localStorage),
    sessionStorage: Object.entries(sessionStorage),
    indexedDB: idb,
    cacheStorage: cached,
    historyState: state,
    cookies,
    windowName: window.name,
    href: location.href,
  });
}"""


def needles(value: str) -> set[str]:
    """Every form of ``value`` a page could hold (T-6): as is, JSON-escaped
    once and twice (ASCII-escaped or not), base64 (standard / URL-safe, with
    or without padding), and each line of a multi-line value of at least
    :data:`MIN_KNOWN_CHARS` characters (with its JSON-escaped forms)."""

    def escaped(text: str) -> set[str]:
        forms = {text}
        for ascii_only in (True, False):
            once = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
            forms |= {once, json.dumps(once, ensure_ascii=ascii_only)[1:-1]}
        return forms

    found = escaped(value)
    raw = value.encode("utf-8", "surrogatepass")
    for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
        text = encoded.decode("ascii")
        found |= {text, text.rstrip("=")}
    lines = [line.strip() for line in re.split(r"\r\n|\r|\n", value)]
    if len(lines) > 1:
        for line in lines:
            if len(line) >= MIN_KNOWN_CHARS:
                found |= escaped(line)
    return {form for form in found if len(form) >= MIN_KNOWN_CHARS}


def _redact(text: str, secret_forms: set[str]) -> str:
    """``text`` with every known form and credential shape replaced (T-10)."""
    for form in sorted(secret_forms, key=len, reverse=True):
        text = text.replace(form, "<redacted>")
    for shape in SHAPES:
        text = shape.sub("<redacted>", text)
    return text


def _page_label(url: str) -> str:
    """A page's URL for a label: no query, no fragment (T-10)."""
    parts = urlsplit(url)
    return (
        f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else "(no URL)"
    )


@dataclasses.dataclass
class LeakWatch:
    places: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    """(where, text) pairs a credential must never be in."""
    known: set[str] = dataclasses.field(default_factory=set)
    pages: int = 0
    snapshots: int = 0
    unread: list[str] = dataclasses.field(default_factory=list)
    """Pages whose storage could not be read at the end (context closed too);
    their writes were still recorded as they happened."""
    followed: list[tuple[Page, str]] = dataclasses.field(default_factory=list)
    """Pages a watched context opened by itself (popups, other tabs)."""
    _contexts: list[BrowserContext] = dataclasses.field(default_factory=list)

    # ------------------------------------------------------------ recording

    def add_known(self, value: str | None) -> None:
        if value and len(value) >= MIN_KNOWN_CHARS:
            self.known.add(value)

    def attach(self, page: Page, test: str) -> None:
        """Watch ``page`` and every page its context opens later (T-7b), and
        record each storage write of the context's pages as it happens (T-7).
        Call before the page's first navigation."""
        context = page.context
        if not any(c is context for c in self._contexts):
            self._contexts.append(context)

            def record(_source: Any, kind: str, text: str) -> None:
                self.places.append((f"{test}: {kind}", text))

            context.expose_binding(WRITE_BINDING, record)
            context.add_init_script(WRITE_HOOK_JS)

            def follow(new: Page) -> None:
                if new is not page:
                    self.followed.append((new, test))
                    self._listen(new, test)

            context.on("page", follow)
        self._listen(page, test)

    def _listen(self, page: Page, test: str) -> None:
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
        HttpOnly session cookie of its context. A page closed before then is
        read through its context (web storage + IndexedDB by origin, T-7);
        if the context is gone too, the page is listed in :attr:`unread`."""
        where = f"{test}: storage of {_page_label(page.url)}"
        try:
            for cookie in page.context.cookies():
                if cookie["name"] == SESSION_COOKIE:
                    self.add_known(cookie["value"])
        except Exception:  # noqa: BLE001,S110 - a closed context: handled below
            pass
        if page.is_closed():
            try:
                state = page.context.storage_state(indexed_db=True)
            except Exception as exc:  # noqa: BLE001 - the context is closed too
                self.unread.append(f"{where} (closed): {type(exc).__name__}")
                return
            # Only what a script could read: HttpOnly cookies (the session
            # cookie) are out of a page's reach, the CSRF companion is exempt.
            state["cookies"] = [
                c
                for c in state.get("cookies", [])
                if not c.get("httpOnly") and c["name"] != CSRF_COOKIE
            ]
            self.snapshots += 1
            self.places.append((f"{where} (closed page)", json.dumps(state)))
            return
        try:
            dump = str(page.evaluate(DUMP_JS))
        except Exception as exc:  # noqa: BLE001 - a page mid-navigation
            dump = f"(not readable: {exc})"
        self.snapshots += 1
        self.places.append((where, dump))

    def snapshot_followed(self) -> None:
        """Snapshot the pages a watched context opened by itself, then forget
        them (the e2e ``new_page`` fixture calls this at each test's end)."""
        for page, test in self.followed:
            self.snapshot(page, test)
        self.followed.clear()

    # ------------------------------------------------------------ judging

    def findings(self, extra: list[tuple[str, str]] | None = None) -> list[str]:
        """Each place that holds a known credential (in any form of
        :func:`needles`) or a credential shape. URLs are also checked decoded
        (``%2D`` etc. hide nothing). Labels are redacted: a finding never
        prints a value, not even when its page URL held it."""
        forms: set[str] = set()
        for value in self.known:
            forms |= needles(value)
        found: list[str] = []
        for where, text in [*self.places, *(extra or [])]:
            label = _redact(unquote(where), forms)
            for candidate in {text, unquote(text)}:
                if any(form in candidate for form in forms):
                    found.append(f"{label}: holds a known credential")
                for shape in SHAPES:
                    match = shape.search(candidate)
                    if match:
                        found.append(f"{label}: holds a {match.group(0)[:4]}... shape")
        return sorted(set(found))
