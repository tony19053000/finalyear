import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CSRF_HEADER, setUnauthorizedHandler } from "../api/client";
import type { SocketLike } from "../events/eventStream";
import { loginErrorMessage } from "../pages/loginMessages";
import { ApiError } from "../api/errors";
import { App } from "./App";
import { THEME_STORAGE_KEY, applyTheme, saveTheme, storedTheme } from "./theme";

/* ------------------------------------------------------------ fakes */

const SESSION = {
  user: { id: "u1", name: "alice", role: "admin" },
  session_id: "tok_1",
  expires_at: "2026-10-23T00:00:00+00:00",
  idle_expires_at: "2026-10-16T12:00:00+00:00",
  idle_timeout_s: 43200,
};

function node(id: string, state: string) {
  return { node_id: id, state, connected: state === "online", hostname: id, os: "linux", platform: "x", gpus: [], last_seen_at: "2026-10-16T00:00:00Z" };
}

type Handler = (init: RequestInit, url: string) => Response | Promise<Response>;

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

const unauthorized = () => json(401, { error: { code: "unauthorized", message: "no session" } });

/** A fake controller: `routes["GET /v1/session"] = handler`. */
function fakeController(routes: Record<string, Handler>) {
  const calls: { key: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn((input: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const path = input.split("?")[0] ?? input;
    const key = `${method} ${path}`;
    calls.push({ key, init });
    const handler = routes[key];
    if (handler === undefined) return Promise.resolve(json(404, { error: { code: "not_found", message: key } }));
    return Promise.resolve(handler(init, input));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, routes };
}

class FakeSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    // client side close: nothing to do
  }
  greet(seq = 1): void {
    this.onopen?.(new Event("open"));
    this.send({ type: "hello", epoch: "e1", seq, resumed: false });
  }
  send(frame: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(frame) }));
  }
  serverClose(code: number): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }
}

function renderApp(path: string) {
  const sockets: FakeSocket[] = [];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <App
      initialPath={path}
      queryClient={queryClient}
      socketFactory={(url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      }}
    />,
  );
  const socket = () => {
    const s = sockets.at(-1);
    if (s === undefined) throw new Error("no event socket opened");
    return s;
  };
  return { ...utils, sockets, socket, queryClient };
}

beforeEach(() => {
  document.documentElement.dataset.theme = "dark";
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

/* ------------------------------------------------------------ tests */

describe("login", () => {
  it("no session -> login page; wrong password -> message; right one -> back where the user was", async () => {
    let loggedIn = false;
    const ctl = fakeController({
      "GET /v1/session": () => json(200, { session: loggedIn ? SESSION : null }),
      "POST /v1/session": (init) => {
        const body = JSON.parse(init.body as string) as { name: string; password: string };
        if (body.password !== "right") return unauthorized();
        loggedIn = true;
        return json(200, SESSION);
      },
      "GET /v1/nodes": () => json(200, { count: 2, nodes: [node("n1", "online"), node("n2", "offline")] }),
    });
    renderApp("/nodes");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    const name = screen.getByLabelText("User name");
    expect(document.activeElement).toBe(name);
    await userEvent.type(name, "alice");
    await userEvent.type(screen.getByLabelText("Password"), "wrong{Enter}");
    expect((await screen.findByRole("alert")).textContent).toBe("Wrong user name or password.");
    expect(screen.getByLabelText<HTMLInputElement>("Password").value).toBe("");

    await userEvent.type(screen.getByLabelText("Password"), "right{Enter}");
    expect(await screen.findByRole("heading", { level: 1, name: "Nodes" })).toBeTruthy();
    expect(screen.getByTestId("current-user").textContent).toBe("alice(admin)");
    expect((await screen.findByTestId("fabric-status")).textContent).toBe("1 of 2 nodes online");

    const post = ctl.calls.filter((c) => c.key === "POST /v1/session").at(-1);
    expect(post?.init.credentials).toBe("same-origin");
    expect(JSON.parse(post?.init.body as string)).toEqual({ name: "alice", password: "right" });
  });

  it("explains a rate-limited login with the wait", () => {
    expect(
      loginErrorMessage(new ApiError(429, "resource_busy", "x", { reason: "rate_limited_user", retryAfterS: 29.2 })),
    ).toBe("Too many login attempts. Try again in 30 s.");
    expect(loginErrorMessage(new ApiError(403, "unauthorized", "x", { reason: "cross_origin" }))).toMatch(/Reload the page/);
    expect(loginErrorMessage(new ApiError(503, "network_error", "x"))).toMatch(/unavailable/);
  });

  it("an unreachable controller is an error with a retry, not a login loop", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    renderApp("/");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Cannot reach the controller")).toBeTruthy();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});

describe("shell", () => {
  function signedIn(extra: Record<string, Handler> = {}) {
    return fakeController({
      "GET /v1/session": () => json(200, { session: SESSION }),
      "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("n1", "online")] }),
      "GET /v1/auth/whoami": () => json(200, { user: SESSION.user, legacy: false, token_id: "tok_1", token_expires_at: null }),
      ...extra,
    });
  }

  it("has landmarks, a skip link, and navigates with the active item marked", async () => {
    signedIn();
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Skip to content" }).getAttribute("href")).toBe("#main");
    const nav = screen.getByRole("navigation", { name: "Main" });
    await userEvent.click(within(nav).getByRole("link", { name: "Jobs" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Jobs" }).getAttribute("aria-current")).toBe("page");
    expect(document.title).toBe("Jobs · MeshForge");
  });

  it("unknown paths show a not-found page inside the shell", async () => {
    signedIn();
    renderApp("/no/such/page");
    expect(await screen.findByRole("heading", { name: "No such page" })).toBeTruthy();
  });

  it("settings shows whoami and the session times", async () => {
    signedIn();
    renderApp("/settings");
    const card = await screen.findByTestId("account-card");
    await within(card).findByText("tok_1");
    expect(within(card).getByText("alice")).toBeTruthy();
    expect(within(card).getByText("admin")).toBeTruthy();
  });

  it("the event stream connects once, shows Live, and does not reconnect on navigation", async () => {
    signedIn();
    const app = renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    expect(screen.getByTestId("stream-status").dataset.status).toBe("connecting");
    app.socket().greet();
    await waitFor(() => {
      expect(screen.getByTestId("stream-status").dataset.status).toBe("live");
    });
    await userEvent.click(screen.getByRole("link", { name: "Nodes" }));
    await screen.findByRole("heading", { level: 1, name: "Nodes" });
    expect(app.sockets).toHaveLength(1);
  });

  it("a node.status event refetches the node count", async () => {
    let online = 1;
    signedIn({
      "GET /v1/nodes": () => json(200, { count: 2, nodes: [node("n1", online >= 1 ? "online" : "offline"), node("n2", online >= 2 ? "online" : "offline")] }),
    });
    const app = renderApp("/");
    expect((await screen.findByTestId("fabric-status")).textContent).toBe("1 of 2 nodes online");
    app.socket().greet();
    online = 2;
    app.socket().send({ seq: 2, type: "node.status", at: 0, data: { node_id: "n2", state: "online", connected: true } });
    await waitFor(() => {
      expect(screen.getByTestId("fabric-status").textContent).toBe("2 of 2 nodes online");
    });
  });

  it("sign out sends the CSRF header and lands on the login page", async () => {
    document.cookie = "mf_csrf=csrf-value; path=/";
    let live = true;
    const ctl = signedIn({
      "GET /v1/session": () => json(200, { session: live ? SESSION : null }),
      "DELETE /v1/session": () => {
        live = false;
        return json(200, { logged_out: true });
      },
    });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You are signed out.")).toBeTruthy();
    const del = ctl.calls.find((c) => c.key === "DELETE /v1/session");
    expect(new Headers(del?.init.headers).get(CSRF_HEADER)).toBe("csrf-value");
  });

  it("our own sign-out is not reported as an expired session, whenever the stream's 4401 lands", async () => {
    let live = true;
    const holder: { app?: ReturnType<typeof renderApp> } = {};
    signedIn({
      "GET /v1/session": () => json(200, { session: live ? SESSION : null }),
      "DELETE /v1/session": async () => {
        live = false;
        const response = json(200, { logged_out: true });
        // The controller closes the stream as it revokes; the 4401 lands
        // around the time the answer does. (The exact window the e2e hit -
        // mutation settled, navigation not yet run - is browser timing; jsdom
        // does not reproduce it. This guards the flow; the e2e keyboard and
        // logout tests guard the timing.)
        await Promise.resolve();
        setTimeout(() => {
          holder.app?.socket().serverClose(4401);
        }, 0);
        return response;
      },
    });
    const app = renderApp("/");
    holder.app = app;
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    app.socket().greet();
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You are signed out.")).toBeTruthy();
    expect(screen.queryByText("Your session ended. Sign in again.")).toBeNull();
  });

  it("a 401 on any call while signed in ends the session -> login, with the reason", async () => {
    signedIn({ "GET /v1/auth/whoami": unauthorized });
    renderApp("/settings");
    expect(await screen.findByText("Your session ended. Sign in again.")).toBeTruthy();
  });

  it("the event stream closing 4401 ends the session -> login", async () => {
    signedIn();
    const app = renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    app.socket().greet();
    app.socket().serverClose(4401);
    expect(await screen.findByText("Your session ended. Sign in again.")).toBeTruthy();
  });
});

/**
 * Fake-time stand-in for `waitFor`: flush promises and due timers, advancing
 * the fake clock by `stepMs` per round (0 = never move the clock: wait for
 * promise work only), until `check` passes. Real time is out of the picture
 * (no `shouldAdvanceTime`) and the backoff jitter is fixed, so the stream's
 * timers fire at the same fake instant on every run, however loaded the
 * machine is (V0.8 G5 item 0h, D197).
 */
async function untilFake(check: () => void, stepMs: number, maxRounds = 3_000): Promise<void> {
  for (let round = 0; ; round += 1) {
    try {
      check();
      return;
    } catch (error) {
      if (round >= maxRounds) throw error;
    }
    await vi.advanceTimersByTimeAsync(stepMs);
  }
}

describe("controller restart (T-2)", () => {
  it("keeps the shell through a failed session re-check and goes live again by itself", async () => {
    let down = false;
    const fetchMock = vi.fn((input: string) => {
      if (down) return Promise.reject(new TypeError("Failed to fetch"));
      const path = input.split("?")[0];
      if (path === "/v1/session") return Promise.resolve(json(200, { session: SESSION }));
      if (path === "/v1/nodes") return Promise.resolve(json(200, { count: 1, nodes: [node("n1", "online")] }));
      return Promise.resolve(json(404, { error: { code: "not_found", message: input } }));
    });
    vi.stubGlobal("fetch", fetchMock);
    // Fix r1 T-2: the stream client captures `Math.random` when it is CREATED
    // (`options.random ?? Math.random`), so the jitter must be fixed before the
    // app renders. Spying later left the real random in the client: a backoff
    // drawn under 50 ms opened a 3rd socket inside the 50 ms advance below.
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const app = renderApp("/");
    await screen.findByTestId("current-user");
    app.socket().greet();
    await waitFor(() => {
      expect(screen.getByTestId("stream-status").dataset.status).toBe("live");
    });

    // From here on time is fully fake (V0.8 G5 item 0h): the stream's backoff
    // timers only move when the test advances them, and the jitter is fixed.
    // Before, `shouldAdvanceTime` let real time run the backoff during a
    // waitFor under load, so the socket count raced (D197).
    vi.useFakeTimers();
    try {
      // The controller goes away: the stream drops, the handshake is refused,
      // and the session re-check that triggers fails with a network error.
      down = true;
      app.socket().serverClose(1006);
      await vi.advanceTimersByTimeAsync(1_000);
      app.socket().serverClose(1006); // refused handshake -> session re-check
      await vi.advanceTimersByTimeAsync(50);
      await untilFake(() => {
        expect(app.queryClient.getQueryState(["session"])?.status).toBe("error");
      }, 0);
      expect(screen.queryByText("Cannot reach the controller")).toBeNull();
      expect(screen.getByTestId("current-user").textContent).toBe("alice(admin)");
      expect(screen.getByTestId("stream-status").dataset.status).toBe("reconnecting");

      // It comes back (new epoch): the stream reconnects on its own backoff.
      down = false;
      // Sockets so far: the first one and the refused handshake.
      const beforeRestart = 2;
      expect(app.sockets.length).toBe(beforeRestart); // the fake clock has not reached the backoff
      await vi.advanceTimersByTimeAsync(20_000);
      expect(app.sockets.length).toBeGreaterThan(beforeRestart);
      app.socket().onopen?.(new Event("open"));
      app.socket().send({ type: "resync", epoch: "e2", seq: 1, reason: "epoch" });
      await untilFake(() => {
        expect(screen.getByTestId("stream-status").dataset.status).toBe("live");
      }, 10);
      await untilFake(() => {
        expect(app.queryClient.getQueryState(["session"])?.status).toBe("success");
      }, 10);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("theme", () => {
  it("toggles data-theme and persists only the theme", async () => {
    fakeController({
      "GET /v1/session": () => json(200, { session: SESSION }),
      "GET /v1/nodes": () => json(200, { count: 0, nodes: [] }),
    });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(Object.keys(localStorage)).toEqual([THEME_STORAGE_KEY]);
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeTruthy();
  });

  it("survives a storage that throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    } as unknown as Storage;
    expect(storedTheme(broken)).toBe("dark");
    expect(() => {
      saveTheme("light", broken);
    }).not.toThrow();
    expect(storedTheme(null)).toBe("dark");
    const el = document.createElement("html");
    applyTheme("light", el);
    expect(el.dataset.theme).toBe("light");
  });
});
