/**
 * Test doubles for page tests (V0.8 G3): a fake controller behind `fetch`, a
 * fake event socket, and `renderApp` at a path. Responses are plain objects
 * shaped like the API's (the pages type them through schema.d.ts).
 */
import { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";

import { App } from "../app/App";
import type { SocketLike } from "../events/eventStream";

export type Handler = (init: RequestInit, url: string) => Response | Promise<Response>;

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

export const ADMIN_SESSION = {
  user: { id: "usr_admin", name: "root", role: "admin" },
  session_id: "tok_1",
  expires_at: "2026-10-23T00:00:00+00:00",
  idle_expires_at: "2026-10-16T12:00:00+00:00",
  idle_timeout_s: 43200,
};

export const USER_SESSION = { ...ADMIN_SESSION, user: { id: "usr_alice", name: "alice", role: "user" } };

export function node(id: string, overrides: Record<string, unknown> = {}) {
  return {
    node_id: id,
    hostname: `host-${id}`,
    os: "linux",
    platform: "x86_64",
    state: "online",
    connected: true,
    last_seen_at: new Date(Date.now() - 3000).toISOString(),
    sample_age_s: 1,
    rtt_ms: 1.5,
    rtt_ewma_ms: 1.2,
    cpu_percent: 40,
    ram_used_bytes: 4 * 1024 ** 3,
    ram_total_bytes: 16 * 1024 ** 3,
    gpus: [],
    scores: { status: "none", metrics: [] },
    certificate: null,
    not_enforced: null,
    ...overrides,
  };
}

export function nodeDetail(id: string, overrides: Record<string, unknown> = {}) {
  return {
    ...node(id),
    first_seen_at: "2026-10-01T00:00:00Z",
    session_epoch: 3,
    last_session_id: "sess_1",
    agent_version: "0.8.0",
    protocol_version: 1,
    peer_address: "ipv4:10.0.0.5:5555",
    peer_endpoint: null,
    cache: {},
    hardware: {
      cpu_model: "Ryzen 7 5800H",
      cpu_cores: 8,
      cpu_threads: 16,
      ram_bytes: String(16 * 1024 ** 3),
      gpus: [{ index: 0, model: "RTX 3050", vram_bytes: String(4 * 1024 ** 3), driver_version: "550", compute_capability: "8.6" }],
      capabilities: ["exec", "docker"],
    },
    owner_policy: null,
    ...overrides,
  };
}

export const EMPTY_NETWORK = { now: "2026-10-16T00:00:00Z", links: [], controller_links: [], failures: [], prober: {}, stale_after_s: 600 };

/** A fake controller: `routes["GET /v1/nodes"] = handler`; unknown routes are 404. */
export function fakeController(routes: Record<string, Handler>) {
  const calls: { key: string; url: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn((input: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const path = input.split("?")[0] ?? input;
    const key = `${method} ${path}`;
    calls.push({ key, url: input, init });
    const handler = routes[key];
    if (handler === undefined) return Promise.resolve(json(404, { error: { code: "not_found", message: key } }));
    return Promise.resolve(handler(init, input));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, routes, count: (key: string) => calls.filter((c) => c.key === key).length };
}

export class FakeSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    // client side close: nothing to do
  }
  greet(seq = 0): void {
    this.onopen?.(new Event("open"));
    this.send({ type: "hello", epoch: "e1", seq, resumed: false });
  }
  send(frame: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(frame) }));
  }
}

let seq = 100;

/** A bus event frame (`at` = now, controller seconds). */
export function event(type: string, data: Record<string, unknown>, at: number = Date.now() / 1000) {
  seq += 1;
  return { seq, type, at, data };
}

export function renderApp(path: string) {
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
