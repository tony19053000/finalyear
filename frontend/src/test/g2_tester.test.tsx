/**
 * V0.8 G2 independent tester suite (r1). Derived from the brief (D170, D171,
 * D172, D175, D181, checklist 23-27), not from the implementation.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../app/App";
import { saveTheme, storedTheme, THEME_STORAGE_KEY } from "../app/theme";
import { CSRF_HEADER, apiRequest, readCookie, setUnauthorizedHandler } from "../api/client";
import {
  EventStreamClient,
  type FabricEvent,
  type SocketLike,
} from "../events/eventStream";

/* ------------------------------------------------------------ helpers */

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function stubFetch(handler: (url: string, init: RequestInit) => Response) {
  const fn = vi.fn((url: string, init: RequestInit = {}) => Promise.resolve(handler(url, init)));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function headerOf(init: RequestInit | undefined, name: string): string | null {
  return new Headers(init?.headers).get(name);
}

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

const SRC = join(__dirname, "..");
const SOURCES = walk(SRC).filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\.|\.d\.ts$|\/test\//.test(f));

beforeEach(() => {
  setUnauthorizedHandler(null);
  localStorage.clear();
  window.sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

/* ------------------------------------------------------------ CSRF (D170) */

describe("T: CSRF header on every mutating call, never on reads", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"] as const)("%s carries X-MeshForge-CSRF from the mf_csrf cookie", async (method) => {
    document.cookie = "mf_csrf=abc.def%3D; path=/";
    const fetchFn = stubFetch(() => json(200, {}));
    await apiRequest("/v1/x", { method });
    expect(headerOf(fetchFn.mock.calls[0]?.[1], CSRF_HEADER)).toBe("abc.def=");
  });

  it.each(["GET", "HEAD"] as const)("%s never carries it even when the cookie exists", async (method) => {
    document.cookie = "mf_csrf=secret; path=/";
    const fetchFn = stubFetch(() => json(200, {}));
    await apiRequest("/v1/x", { method });
    expect(headerOf(fetchFn.mock.calls[0]?.[1], CSRF_HEADER)).toBeNull();
  });

  it("default method (none given) is a GET without the header", async () => {
    document.cookie = "mf_csrf=secret; path=/";
    const fetchFn = stubFetch(() => json(200, {}));
    await apiRequest("/v1/x");
    expect(headerOf(fetchFn.mock.calls[0]?.[1], CSRF_HEADER)).toBeNull();
  });

  it("picks the cookie by exact name, not a look-alike or a suffix", () => {
    expect(readCookie("mf_csrf", "xmf_csrf=bad; mf_csrf_x=bad2; mf_csrf=good")).toBe("good");
    expect(readCookie("mf_csrf", "a=b=c; mf_csrf=v=w==")).toBe("v=w==");
    expect(readCookie("mf_csrf", "mf_csrf")).toBeUndefined();
    expect(readCookie("mf_csrf", "")).toBeUndefined();
    expect(readCookie("mf_csrf", "mf_csrf=%E0%A4%A")).toBe("%E0%A4%A"); // malformed % escape: no throw
  });

  it("re-reads the cookie on every call (rotation after login)", async () => {
    const fetchFn = stubFetch(() => json(200, {}));
    document.cookie = "mf_csrf=one; path=/";
    await apiRequest("/v1/x", { method: "POST" });
    document.cookie = "mf_csrf=two; path=/";
    await apiRequest("/v1/x", { method: "POST" });
    expect(headerOf(fetchFn.mock.calls[0]?.[1], CSRF_HEADER)).toBe("one");
    expect(headerOf(fetchFn.mock.calls[1]?.[1], CSRF_HEADER)).toBe("two");
  });

  it("every request is same-origin with credentials, to a relative /v1 path", async () => {
    const fetchFn = stubFetch(() => json(200, {}));
    await apiRequest("/v1/nodes");
    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe("/v1/nodes");
    expect(init?.credentials).toBe("same-origin");
  });

  it.each(["https://evil.example/v1/x", "//evil.example/v1/x", "v1/x", "/other", ""])(
    "refuses to send to %j",
    async (path) => {
      const fetchFn = stubFetch(() => json(200, {}));
      await expect(apiRequest(path)).rejects.toThrow();
      expect(fetchFn).not.toHaveBeenCalled();
    },
  );

  it("the ONLY fetch/XHR/sendBeacon/EventSource call site in src is api/client.ts", () => {
    const offenders = SOURCES.filter((f) => !f.endsWith(join("api", "client.ts"))).filter((f) =>
      /\bfetch\s*\(|XMLHttpRequest|sendBeacon|new EventSource/.test(readFileSync(f, "utf8")),
    );
    expect(offenders).toEqual([]);
  });

  it("WebSocket is created only by the event stream client", () => {
    const offenders = SOURCES.filter((f) => !f.endsWith(join("events", "eventStream.ts"))).filter((f) =>
      /new WebSocket/.test(readFileSync(f, "utf8")),
    );
    expect(offenders).toEqual([]);
  });
});

/* ------------------------------------------------------------ no secrets (checklist 25) */

describe("T: nothing secret is kept by script", () => {
  it("only theme.ts touches web storage", () => {
    const users = SOURCES.filter((f) => /localStorage|sessionStorage|indexedDB|document\.cookie/.test(readFileSync(f, "utf8"))).map(
      (f) => f.slice(SRC.length + 1),
    );
    expect(users.sort()).toEqual(["api/client.ts", "app/theme.ts"].sort()); // client.ts: read-only csrf cookie
  });

  it("client.ts never writes a cookie or stores anything", () => {
    const text = readFileSync(join(SRC, "api", "client.ts"), "utf8");
    expect(text).not.toMatch(/document\.cookie\s*=[^=]/);
    expect(text).not.toMatch(/Storage|setItem/);
  });

  it("no source puts a credential in a URL or a log", () => {
    for (const f of SOURCES) {
      const text = readFileSync(f, "utf8");
      expect(text, f).not.toMatch(/[?&](token|access_token|password|session)=/i);
      expect(text, f).not.toMatch(/console\.(log|debug|info)\(/);
    }
  });

  it("the event stream URL carries only since+epoch, never credentials", () => {
    const sockets: string[] = [];
    const mk = () => {
      const s: SocketLike & { url?: string } = { onopen: null, onmessage: null, onclose: null, onerror: null, close() {} };
      return s;
    };
    const holder: SocketLike[] = [];
    const client = new EventStreamClient({
      baseUrl: "wss://h/v1/events/ws",
      socketFactory: (u) => {
        sockets.push(u);
        const s = mk();
        holder.push(s);
        return s;
      },
      onEvent: () => undefined,
      onResync: () => undefined,
    });
    client.start();
    holder[0]?.onmessage?.(new MessageEvent("message", { data: JSON.stringify({ type: "hello", epoch: "E", seq: 3, resumed: false }) }));
    expect(client.url()).toBe("wss://h/v1/events/ws?since=3&epoch=E");
    client.stop();
  });
});

/* ------------------------------------------------------------ 401 -> login, no leak */

describe("T: 401 sends the user to login without leaking the previous path into the URL", () => {
  it("signed-out deep link with query ends at exactly /login (no ?from=, no hash)", async () => {
    stubFetch((url) => {
      if (url.startsWith("/v1/session")) return json(401, { error: { code: "unauthorized", message: "no" } });
      return json(404, { error: { code: "not_found", message: url } });
    });
    window.history.replaceState(null, "", "/jobs?secret=abc&x=1#frag");
    render(<App />);
    await screen.findByRole("heading", { name: "Sign in" });
    expect(window.location.pathname).toBe("/login");
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toMatch(/secret|from|jobs/);
    expect(JSON.stringify(window.history.state)).toContain("/jobs?secret=abc&x=1"); // router state only
    expect(Object.keys(localStorage)).toEqual([]);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("login response without CSRF cookie still never stores the password in the DOM after a refusal", async () => {
    stubFetch((url, init) => {
      if (url === "/v1/session" && init.method === "POST") return json(401, { error: { code: "unauthorized", message: "bad" } });
      return json(401, { error: { code: "unauthorized", message: "no" } });
    });
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText("User name"), "alice");
    await user.type(screen.getByLabelText("Password"), "hunter2-unique");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText<HTMLInputElement>("Password").value).toBe("");
    expect(document.body.innerHTML).not.toContain("hunter2-unique");
    expect(window.location.href).not.toContain("hunter2");
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain("hunter2");
  });

  it("a wrong password (401 on POST /v1/session) does not run the session-expired handler", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    stubFetch(() => json(401, { error: { code: "unauthorized", message: "bad" } }));
    const { openSession } = await import("../api/endpoints");
    await expect(openSession("a", "b")).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
  });
});

/* ------------------------------------------------------------ theme with hostile storage (D175) */

describe("T: theme survives hostile storage", () => {
  it("window.localStorage getter throwing (SecurityError)", () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(storedTheme()).toBe("dark");
    expect(() => {
      saveTheme("light");
    }).not.toThrow();
  });

  it("getItem/setItem throwing (quota, privacy mode)", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("boom");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(storedTheme()).toBe("dark");
    expect(() => {
      saveTheme("dark");
    }).not.toThrow();
  });

  it.each([null, "", "LIGHT", "blue", "{\"x\":1}", "light "])("garbage stored value %j -> dark", (v) => {
    if (v !== null) localStorage.setItem(THEME_STORAGE_KEY, v);
    expect(storedTheme()).toBe("dark");
  });

  it("clicking the toggle in the real shell with a throwing setItem still switches the theme and the page keeps working", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    const session = {
      user: { id: "u", name: "alice", role: "admin" },
      session_id: "tok_1",
      expires_at: "2026-10-23T00:00:00+00:00",
      idle_expires_at: "2026-10-16T12:00:00+00:00",
      idle_timeout_s: 1,
    };
    stubFetch((url) => (url === "/v1/session" ? json(200, session) : url.startsWith("/v1/nodes") ? json(200, { nodes: [] }) : json(404, { error: { code: "x", message: "x" } })));
    document.documentElement.dataset.theme = "dark";
    const user = userEvent.setup();
    render(<App initialPath="/" socketFactory={() => ({ onopen: null, onmessage: null, onclose: null, onerror: null, close() {} })} queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    await user.click(await screen.findByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeTruthy();
  });
});

/* ------------------------------------------------------------ no invented data on placeholders (checklist 26) */

describe("T: placeholder pages invent nothing", () => {
  const session = {
    user: { id: "u", name: "alice", role: "admin" },
    session_id: "tok_1",
    expires_at: "2026-10-23T00:00:00+00:00",
    idle_expires_at: "2026-10-16T12:00:00+00:00",
    idle_timeout_s: 1,
  };
  it.each(["/", "/nodes", "/jobs"])("%s shows no numbers, percentages, host names or job ids", async (path) => {
    stubFetch((url) => {
      if (url === "/v1/session") return json(200, session);
      if (url.startsWith("/v1/nodes"))
        return json(200, { nodes: [{ node_id: "n1", state: "online", connected: true, hostname: "UNIQUE-HOST-XYZ", os: "linux", platform: "p", gpus: [], last_seen_at: "2026-10-16T00:00:00Z" }] });
      return json(404, { error: { code: "x", message: "x" } });
    });
    render(<App initialPath={path} socketFactory={() => ({ onopen: null, onmessage: null, onclose: null, onerror: null, close() {} })} queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    const main = await screen.findByRole("main");
    await waitFor(() => { expect(screen.getByTestId("fabric-status")).toBeTruthy(); });
    const text = main.textContent;
    expect(text).not.toMatch(/\d/);
    expect(text).not.toMatch(/%|UNIQUE-HOST/);
    expect(main.querySelectorAll("table, canvas, svg path, progress, meter").length).toBe(0);
  });
});

/* ------------------------------------------------------------ CSP at the source level */

describe("T: no inline style anywhere in source", () => {
  it("no JSX style=, dangerouslySetInnerHTML, .style assignment, cssText, insertAdjacentHTML or <style> creation", () => {
    for (const f of SOURCES) {
      const text = readFileSync(f, "utf8");
      const rel = f.slice(SRC.length + 1);
      expect(text, rel).not.toMatch(/\bstyle\s*=\s*[{"]/);
      expect(text, rel).not.toMatch(/dangerouslySetInnerHTML|insertAdjacentHTML|\.innerHTML\s*=|outerHTML\s*=/);
      expect(text, rel).not.toMatch(/\.style\.cssText|createElement\(\s*["']style|setAttribute\(\s*["']style|\.style\.[a-zA-Z]+\s*=[^=]/);
      expect(text, rel).not.toMatch(/\beval\(|new Function\(|setTimeout\(\s*["'`]/);
    }
  });
});

/* ------------------------------------------------------------ event stream edges */

class Sock implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closes: number[] = [];
  constructor(readonly url: string) {}
  close(code?: number) { this.closes.push(code ?? -1); }
  open() { this.onopen?.(new Event("open")); }
  send(f: unknown) { this.onmessage?.(new MessageEvent("message", { data: typeof f === "string" ? f : JSON.stringify(f) })); }
  hello(epoch = "E", seq = 0, resumed = false) { this.open(); this.send({ type: "hello", epoch, seq, resumed }); }
  drop(code = 1006) { this.onclose?.(new CloseEvent("close", { code })); }
}

function stream(over: Partial<ConstructorParameters<typeof EventStreamClient>[0]> = {}) {
  const socks: Sock[] = [];
  const events: FabricEvent[] = [];
  const resyncs: string[] = [];
  const statuses: string[] = [];
  const unauthorized = vi.fn();
  const handshake = vi.fn();
  const client = new EventStreamClient({
    baseUrl: "wss://h/v1/events/ws",
    socketFactory: (u) => { const s = new Sock(u); socks.push(s); return s; },
    onEvent: (e) => events.push(e),
    onResync: (r) => resyncs.push(r),
    onStatus: (s) => statuses.push(s),
    onUnauthorized: unauthorized,
    onHandshakeFailed: handshake,
    random: () => 1,
    ...over,
  });
  const last = () => { const s = socks.at(-1); if (!s) throw new Error("none"); return s; };
  return { client, socks, events, resyncs, statuses, unauthorized, handshake, last };
}
const ev = (seq: number, type = "node.status") => ({ seq, type, at: 1, data: {} });

describe("T: event stream edge cases", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("resumes with since=0 (a valid position) rather than treating 0 as 'no position'", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 0);
    t.last().drop();
    vi.runOnlyPendingTimers();
    expect(t.last().url).toBe("wss://h/v1/events/ws?since=0&epoch=E");
  });

  it("start() is idempotent: twice -> one socket; while a reconnect timer is pending -> still none extra", () => {
    const t = stream();
    t.client.start();
    t.client.start();
    expect(t.socks).toHaveLength(1);
    t.last().drop();
    t.client.start(); // timer pending
    expect(t.socks).toHaveLength(1);
    vi.runOnlyPendingTimers();
    expect(t.socks).toHaveLength(2);
  });

  it("a flapping server that accepts then closes before hello keeps backing off (failures not reset by 'open')", () => {
    const t = stream({ backoffBaseMs: 100, backoffCapMs: 1600 });
    t.client.start();
    const delays: number[] = [];
    for (let i = 0; i < 6; i++) {
      t.last().open();
      const before = vi.getTimerCount();
      t.last().drop(1006);
      expect(vi.getTimerCount()).toBe(before + 1);
      // random()=1 -> delay is the ceiling; find it by stepping time
      let waited = 0;
      const n = t.socks.length;
      while (t.socks.length === n) { vi.advanceTimersByTime(50); waited += 50; if (waited > 5000) throw new Error("never reconnected"); }
      delays.push(waited);
    }
    expect(delays[0]).toBeLessThanOrEqual(150);
    expect(delays[3]).toBeGreaterThan(delays[0] ?? 0);
    expect(Math.max(...delays)).toBeLessThanOrEqual(1650);
    expect(delays.at(-1)).toBeGreaterThanOrEqual(1500);
  });

  it("a long outage never overflows the delay (2**failures -> Infinity) and stays capped", () => {
    const t = stream();
    t.client.start();
    for (let i = 0; i < 1200; i++) {
      t.last().drop(1006);
      vi.runOnlyPendingTimers();
    }
    expect(t.client.backoffMs(5000)).toBeLessThanOrEqual(15_000);
    expect(Number.isFinite(t.client.backoffMs(5000))).toBe(true);
  });

  it("4401 while reconnecting-after-live: stops for good, one onUnauthorized, no timer left; start() can revive", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 5);
    t.last().drop(1006);
    vi.runOnlyPendingTimers();
    t.last().open();
    t.last().drop(4401);
    expect(t.unauthorized).toHaveBeenCalledTimes(1);
    expect(t.client.status).toBe("stopped");
    expect(vi.getTimerCount()).toBe(0);
    const n = t.socks.length;
    vi.advanceTimersByTime(60_000);
    expect(t.socks).toHaveLength(n);
    t.client.start();
    expect(t.socks).toHaveLength(n + 1);
  });

  it("4401 delivered even if the socket never reported open", () => {
    const t = stream();
    t.client.start();
    t.last().drop(4401);
    expect(t.unauthorized).toHaveBeenCalledTimes(1);
    expect(t.handshake).not.toHaveBeenCalled();
  });

  it("4503 / 4029 / 4008 / 1011 / 1006 / 1000 all reconnect with backoff (never give up silently)", () => {
    for (const code of [4503, 4029, 4008, 1011, 1006, 1000, 1001]) {
      const t = stream();
      t.client.start();
      t.last().hello("E", 1);
      t.last().drop(code);
      expect(t.client.status, String(code)).toBe("reconnecting");
      vi.runOnlyPendingTimers();
      expect(t.socks, String(code)).toHaveLength(2);
    }
  });

  it("stop() then a late close/message from the old socket does not reconnect or deliver", () => {
    const t = stream();
    t.client.start();
    const s = t.last();
    s.hello("E", 1);
    const onclose = s.onclose;
    const onmessage = s.onmessage;
    t.client.stop();
    expect(s.closes).toHaveLength(1);
    onclose?.(new CloseEvent("close", { code: 1006 }));
    onmessage?.(new MessageEvent("message", { data: JSON.stringify(ev(2)) }));
    vi.advanceTimersByTime(60_000);
    expect(t.socks).toHaveLength(1);
    expect(t.events).toHaveLength(0);
    expect(t.client.status).toBe("stopped");
  });

  it("stop() then start() on the same client works and does not duplicate events", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 1);
    t.last().send(ev(2));
    t.client.stop();
    t.client.start();
    expect(t.last().url).toContain("since=2");
    t.last().hello("E", 2, true);
    t.last().send(ev(2)); // replayed duplicate
    t.last().send(ev(3));
    expect(t.events.map((e) => e.seq)).toEqual([2, 3]);
  });

  it("a stale socket (replaced by a newer one) cannot trigger a second reconnect or events", () => {
    const t = stream();
    t.client.start();
    const old = t.last();
    old.hello("E", 1);
    old.drop(1006);
    vi.runOnlyPendingTimers();
    const fresh = t.last();
    fresh.hello("E", 1, true);
    old.drop(1006); // late duplicate close from the dead socket
    old.send(ev(9));
    vi.advanceTimersByTime(60_000);
    expect(t.socks).toHaveLength(2);
    expect(t.events).toHaveLength(0);
    expect(t.client.status).toBe("live");
  });

  it("epoch change announced by resync: new epoch + restarted seq used on the next reconnect; low seqs are delivered", () => {
    const t = stream();
    t.client.start();
    t.last().hello("OLD", 500);
    t.last().send(ev(501));
    t.last().drop();
    vi.runOnlyPendingTimers();
    expect(t.last().url).toContain("since=501&epoch=OLD");
    t.last().open();
    t.last().send({ type: "resync", epoch: "NEW", seq: 2, reason: "epoch" });
    expect(t.resyncs).toEqual(["epoch"]);
    t.last().send(ev(3));
    expect(t.events.map((e) => e.seq)).toEqual([501, 3]);
    t.last().drop();
    vi.runOnlyPendingTimers();
    expect(t.last().url).toContain("since=3&epoch=NEW");
  });

  it("resync with an unknown reason still resyncs; resync with a missing epoch is not a greeting", () => {
    const t = stream();
    t.client.start();
    t.last().open();
    t.last().send({ type: "resync", seq: 1, reason: "ring" }); // no epoch
    expect(t.client.status).toBe("connecting");
    expect(t.resyncs).toEqual([]);
    t.last().send({ type: "resync", epoch: "E", seq: 1, reason: "weird" });
    expect(t.resyncs).toEqual(["ring"]);
    expect(t.client.status).toBe("live");
  });

  it("garbage frames never throw: binary, arrays, null, numbers, partial json, string seq, missing type", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 0);
    const bad = [new ArrayBuffer(4) as unknown as string, "[]", "null", "42", "{", "", '{"seq":"1","type":"x"}', '{"seq":1}', '{"type":"x"}', '{"seq":NaN,"type":"x"}'];
    for (const b of bad) {
      expect(() => { t.last().onmessage?.(new MessageEvent("message", { data: b })); }).not.toThrow();
    }
    expect(t.events).toEqual([]);
    t.last().send(ev(1));
    expect(t.events).toHaveLength(1);
  });

  it("event with data = array/null/string is delivered with {} data, never a crash downstream", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 0);
    for (const [i, data] of [[], null, "s", 5].entries()) {
      t.last().send({ seq: i + 1, type: "alert", at: 1, data });
    }
    expect(t.events.map((e) => e.data)).toEqual([{}, {}, {}, {}]);
  });

  it("out-of-order older seq after newer is dropped; a gap is accepted", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 10);
    t.last().send(ev(15));
    t.last().send(ev(12));
    t.last().send(ev(16));
    expect(t.events.map((e) => e.seq)).toEqual([15, 16]);
  });

  // T-1 (minor): control frames (hello/resync) after the greeting are treated as ordinary
  // events (advance seq, reach onEvent; a mid-stream resync would never call onResync).
  // The server today only sends them first, so this is defence in depth.
  it.fails("T-1: a second 'hello'/'resync' frame mid-stream is not delivered as an app event", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 5);
    t.last().send({ type: "hello", epoch: "E", seq: 99, resumed: false });
    t.last().send({ type: "resync", epoch: "E", seq: 120, reason: "ring" });
    expect(t.events.map((e) => e.type)).not.toContain("hello");
    expect(t.events.map((e) => e.type)).not.toContain("resync");
  });

  it("socketFactory throwing (e.g. SecurityError, bad URL) backs off and retries instead of crashing", () => {
    let calls = 0;
    const t = stream({ socketFactory: () => { calls += 1; throw new DOMException("bad", "SyntaxError"); } });
    expect(() => { t.client.start(); }).not.toThrow();
    for (let i = 0; i < 5; i++) vi.runOnlyPendingTimers();
    expect(calls).toBeGreaterThanOrEqual(5);
    expect(t.client.status).toBe("reconnecting");
    t.client.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("a refused handshake (close before open, 1006) calls onHandshakeFailed every attempt and still retries", () => {
    const t = stream();
    t.client.start();
    for (let i = 0; i < 3; i++) { t.last().drop(1006); vi.runOnlyPendingTimers(); }
    expect(t.handshake).toHaveBeenCalledTimes(3);
    expect(t.socks).toHaveLength(4);
  });

  it("status sequence connecting -> live -> reconnecting -> live, no duplicates", () => {
    const t = stream();
    t.client.start();
    t.last().hello("E", 1);
    t.last().drop();
    vi.runOnlyPendingTimers();
    t.last().hello("E", 1, true);
    expect(t.statuses).toEqual(["connecting", "live", "reconnecting", "live"]);
  });

  it("an onEvent consumer that throws does not corrupt the position (the event is not replayed forever)", () => {
    let boom = true;
    const t = stream({ onEvent: () => { if (boom) { boom = false; throw new Error("consumer bug"); } } });
    t.client.start();
    t.last().hello("E", 0);
    expect(() => { t.last().send(ev(1)); }).toThrow(); // surfaces to the console, as an unhandled handler error would
    expect(t.client.seq).toBe(1);
    expect(() => { t.last().send(ev(2)); }).not.toThrow();
    expect(t.client.seq).toBe(2);
  });
});

/* ------------------------------------------------------------ T-2: transient session-check failure */

describe("T: a failed session re-check while signed in must not tear down the shell", () => {
  it.fails("T-2 (major): network error on the session refetch keeps the shell (and its event stream) mounted", async () => {
    const { queryKeys } = await import("../api/queryKeys");
    const session = {
      user: { id: "u", name: "alice", role: "admin" },
      session_id: "tok_1",
      expires_at: "2026-10-23T00:00:00+00:00",
      idle_expires_at: "2026-10-16T12:00:00+00:00",
      idle_timeout_s: 1,
    };
    let down = false;
    const fetchFn = vi.fn((url: string) => {
      if (down) return Promise.reject(new TypeError("Failed to fetch"));
      if (url === "/v1/session") return Promise.resolve(json(200, session));
      if (url.startsWith("/v1/nodes")) return Promise.resolve(json(200, { nodes: [] }));
      return Promise.resolve(json(404, { error: { code: "x", message: "x" } }));
    });
    vi.stubGlobal("fetch", fetchFn);
    const closes: number[] = [];
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <App
        initialPath="/"
        queryClient={client}
        socketFactory={() => ({ onopen: null, onmessage: null, onclose: null, onerror: null, close() { closes.push(1); } })}
      />,
    );
    await screen.findByTestId("current-user");
    down = true; // the controller restarts; the stream's handshake failure invalidates the session
    await client.invalidateQueries({ queryKey: queryKeys.session });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("heading", { name: "Cannot reach the controller" })).toBeNull();
    expect(screen.queryByTestId("stream-status")).not.toBeNull();
    expect(closes).toEqual([]);
  });
});
