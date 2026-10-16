/** V0.8 G2 tester r2: controller restarts, expired sessions, sign-out race. */
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "../api/queryKeys";
import { setUnauthorizedHandler } from "../api/client";
import { App } from "../app/App";
import { EventStreamClient, type SocketLike } from "../events/eventStream";

const SESSION = {
  user: { id: "u", name: "alice", role: "admin" },
  session_id: "tok_1",
  expires_at: "2026-10-23T00:00:00+00:00",
  idle_expires_at: "2026-10-16T12:00:00+00:00",
  idle_timeout_s: 1,
};
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const E401 = () => json(401, { error: { code: "unauthorized", message: "no" } });

class Sock implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    /* noop */
  }
  send(f: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(f) }));
  }
  hello(epoch = "e1", seq = 0, resumed = false) {
    this.onopen?.(new Event("open"));
    this.send({ type: "hello", epoch, seq, resumed });
  }
  drop(code = 1006, opened = true) {
    if (opened) this.onopen?.(new Event("open"));
    this.onclose?.(new CloseEvent("close", { code }));
  }
}

type Mode = "ok" | "down" | "expired";

function rig() {
  const state = { mode: "ok" as Mode, loginMode: "ok" as "ok" | "down", deleteGate: null as null | Promise<void> };
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const key = `${init.method ?? "GET"} ${url.split("?")[0] ?? url}`;
      calls.push(key);
      if (key === "POST /v1/session") {
        if (state.loginMode === "down") throw new TypeError("Failed to fetch");
        state.mode = "ok";
        return json(200, SESSION);
      }
      if (state.mode === "down") throw new TypeError("Failed to fetch");
      if (key === "DELETE /v1/session") {
        if (state.deleteGate) await state.deleteGate;
        state.mode = "expired";
        return json(200, { logged_out: true });
      }
      if (state.mode === "expired") return E401();
      if (key === "GET /v1/session") return json(200, { session: SESSION });
      if (key === "GET /v1/nodes") return json(200, { nodes: [] });
      return json(404, { error: { code: "nf", message: key } });
    }),
  );
  const sockets: Sock[] = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <App
      initialPath="/nodes"
      queryClient={client}
      socketFactory={(u) => {
        const s = new Sock(u);
        sockets.push(s);
        return s;
      }}
    />,
  );
  return { state, calls, sockets, client, ...utils, last: () => sockets[sockets.length - 1] as Sock };
}

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("T2: controller restarts under a signed-in shell", () => {
  it("restart with an EXPIRED session: reconnect -> resync -> login page, never a stuck shell", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.last().hello();
    t.state.mode = "down";
    t.last().drop();
    await waitFor(() => { expect(t.sockets.length).toBe(2); }, { timeout: 3000 });
    expect(screen.getByTestId("current-user")).toBeTruthy(); // shell kept while down
    t.state.mode = "expired"; // controller back, session gone (new DB / expired)
    t.last().send({ type: "resync", epoch: "e2", seq: 0, reason: "epoch" });
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByTestId("current-user")).toBeNull();
    expect(screen.queryByText("Cannot reach the controller")).toBeNull();
  });

  it("restart where the handshake is refused (dead session, 403): session re-check sends the user to login", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.last().hello();
    t.state.mode = "down";
    t.last().drop();
    await waitFor(() => { expect(t.sockets.length).toBe(2); }, { timeout: 3000 });
    t.state.mode = "expired";
    t.last().drop(1006, false); // refused before open
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
  });

  it("restart where the stream is closed 4401 by the new controller: login, once", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.last().hello();
    t.state.mode = "expired";
    t.last().drop(4401);
    expect(await screen.findByText("Your session ended. Sign in again.")).toBeTruthy();
    const n = t.sockets.length;
    await new Promise((r) => setTimeout(r, 1200));
    expect(t.sockets.length).toBe(n); // no zombie reconnect after logout-by-expiry
  });

  it("a long outage: many failed session re-checks keep the shell; recovery refreshes the data", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.last().hello();
    t.state.mode = "down";
    for (let i = 0; i < 8; i++) {
      await t.client.invalidateQueries({ queryKey: queryKeys.session });
      await t.client.invalidateQueries({ queryKey: queryKeys.nodes });
      expect(screen.queryByText("Cannot reach the controller")).toBeNull();
      expect(screen.getByTestId("current-user")).toBeTruthy();
    }
    t.state.mode = "ok";
    await t.client.invalidateQueries();
    await waitFor(() => { expect(screen.getByTestId("fabric-status").textContent).toContain("No nodes"); });
    expect(screen.getByTestId("current-user")).toBeTruthy();
  });

  it("controller down at first load (no session known): full-page error with Retry, and Retry recovers", async () => {
    const t = (() => {
      const s = { down: true };
      vi.stubGlobal("fetch", vi.fn((url: string) => {
        if (s.down) return Promise.reject(new TypeError("Failed to fetch"));
        if (url === "/v1/session") return Promise.resolve(json(200, { session: SESSION }));
        return Promise.resolve(json(200, { nodes: [] }));
      }));
      render(<App initialPath="/" queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })} socketFactory={() => new Sock("x")} />);
      return s;
    })();
    expect(await screen.findByText("Cannot reach the controller")).toBeTruthy();
    t.down = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("current-user")).toBeTruthy();
  });

  it("restart during login: network error message, form stays usable, second try signs in", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    // sign out locally to reach the login page
    t.state.mode = "expired";
    await t.client.invalidateQueries({ queryKey: queryKeys.session });
    await screen.findByRole("heading", { name: "Sign in" });
    t.state.loginMode = "down";
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("User name"), "alice");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain("did not answer");
    expect(screen.getByLabelText<HTMLInputElement>("User name").value).toBe("alice"); // name kept
    expect(screen.getByLabelText<HTMLInputElement>("Password").value).toBe("");
    t.state.loginMode = "ok";
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("current-user")).toBeTruthy();
  });
});

describe("T2: sign-out race", () => {
  it("4401 landing BEFORE the DELETE answer is still 'signed out', not 'session ended'", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.last().hello();
    let release: () => void = () => undefined;
    t.state.deleteGate = new Promise<void>((r) => { release = r; });
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    t.last().drop(4401); // controller closes the stream while revoking
    await new Promise((r) => setTimeout(r, 30));
    release();
    expect(await screen.findByText("You are signed out.")).toBeTruthy();
    expect(screen.queryByText("Your session ended. Sign in again.")).toBeNull();
  });

  it("sign-out while the controller is down: still lands on login, with a toast, cache cleared", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.state.mode = "down";
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(t.client.getQueryData(queryKeys.session)).toBeNull();
    expect(t.client.getQueryData(queryKeys.nodes)).toBeUndefined();
  });

  it("double-click on Sign out sends one DELETE", async () => {
    const t = rig();
    await screen.findByTestId("current-user");
    t.state.deleteGate = new Promise<void>((r) => setTimeout(r, 50));
    const b = screen.getByRole("button", { name: "Sign out" });
    await userEvent.dblClick(b);
    await screen.findByText("You are signed out.");
    expect(t.calls.filter((c) => c === "DELETE /v1/session")).toHaveLength(1);
  });
});

describe("T2: stream client under restarts", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  function mk() {
    const socks: Sock[] = [];
    const events: number[] = [];
    const resyncs: string[] = [];
    const c = new EventStreamClient({
      baseUrl: "wss://h/v1/events/ws",
      socketFactory: (u) => { const s = new Sock(u); socks.push(s); return s; },
      onEvent: (e) => events.push(e.seq),
      onResync: (r) => resyncs.push(r),
      random: () => 1,
    });
    return { c, socks, events, resyncs, last: () => socks[socks.length - 1] as Sock };
  }

  it("hello with a NEW epoch mid-stream = resync(epoch): position restarts, low seqs delivered", () => {
    const t = mk();
    t.c.start();
    t.last().hello("A", 100);
    t.last().send({ seq: 101, type: "alert", at: 1, data: {} });
    t.last().send({ type: "hello", epoch: "B", seq: 3, resumed: false });
    expect(t.resyncs).toEqual(["epoch"]);
    t.last().send({ seq: 4, type: "alert", at: 1, data: {} });
    expect(t.events).toEqual([101, 4]);
    expect(t.c.epoch).toBe("B");
  });

  it("hello with the SAME epoch mid-stream changes nothing (no resync, position kept)", () => {
    const t = mk();
    t.c.start();
    t.last().hello("A", 10);
    t.last().send({ seq: 11, type: "alert", at: 1, data: {} });
    t.last().send({ type: "hello", epoch: "A", seq: 2, resumed: false });
    expect(t.resyncs).toEqual([]);
    expect(t.c.seq).toBe(11);
    t.last().send({ seq: 12, type: "alert", at: 1, data: {} });
    expect(t.events).toEqual([11, 12]);
  });

  it("malformed mid-stream control frames are ignored", () => {
    const t = mk();
    t.c.start();
    t.last().hello("A", 10);
    t.last().send({ type: "resync", seq: 1 });
    t.last().send({ type: "hello", epoch: 5, seq: 1 });
    t.last().send({ type: "resync", epoch: "A", seq: "x" });
    expect(t.resyncs).toEqual([]);
    expect(t.c.seq).toBe(10);
    expect(t.c.epoch).toBe("A");
  });

  it("rapid repeated restarts: one socket at a time, one resync per restart, at most one timer", () => {
    const t = mk();
    t.c.start();
    t.last().hello("E0", 5);
    for (let i = 1; i <= 25; i++) {
      t.last().drop(1006);
      expect(vi.getTimerCount()).toBeLessThanOrEqual(1);
      vi.runOnlyPendingTimers();
      t.last().onopen?.(new Event("open"));
      t.last().send({ type: "resync", epoch: `E${i}`, seq: 0, reason: "epoch" });
      t.last().send({ seq: 1, type: "alert", at: 1, data: {} });
    }
    expect(t.socks).toHaveLength(26);
    expect(t.resyncs).toHaveLength(25);
    expect(t.events).toHaveLength(25);
    expect(t.c.status).toBe("live");
    // the last reconnect URL carries the latest epoch
    t.last().drop(1006);
    vi.runOnlyPendingTimers();
    expect(t.last().url).toContain("epoch=E25");
    t.c.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("minutes-long outage then recovery: delay never exceeds the cap, reconnect succeeds, failures reset", () => {
    const t = mk();
    t.c.start();
    t.last().hello("A", 1);
    let waited = 0;
    t.last().drop(1006);
    for (let i = 0; i < 40; i++) {
      const n = t.socks.length;
      let step = 0;
      while (t.socks.length === n) { vi.advanceTimersByTime(100); step += 100; if (step > 15_100) throw new Error("delay exceeds cap"); }
      waited += step;
      t.last().drop(1006, false);
    }
    expect(waited).toBeGreaterThan(5 * 60_000 * 0.5); // minutes of outage were simulated
    vi.runOnlyPendingTimers();
    t.last().onopen?.(new Event("open"));
    t.last().send({ type: "resync", epoch: "B", seq: 0, reason: "epoch" });
    expect(t.c.status).toBe("live");
    t.last().drop(1006);
    expect(t.c.backoffMs(0)).toBeLessThanOrEqual(500);
  });
});
