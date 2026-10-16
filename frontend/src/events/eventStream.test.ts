import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "../api/queryKeys";
import { applyEvent, applyResync } from "./applyEvent";
import {
  CLOSE_SLOW_CONSUMER,
  CLOSE_UNAUTHORIZED,
  EventStreamClient,
  defaultEventsUrl,
  type FabricEvent,
  type SocketLike,
} from "./eventStream";

/** A scriptable stand-in for the browser WebSocket. */
class FakeSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed: { code?: number; reason?: string } | null = null;

  constructor(readonly url: string) {}

  open(): void {
    this.onopen?.(new Event("open"));
  }

  send(frame: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: typeof frame === "string" ? frame : JSON.stringify(frame) }));
  }

  serverClose(code: number): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }

  close(code?: number, reason?: string): void {
    this.closed = { code, reason };
  }
}

const BASE = "wss://ctl.test/v1/events/ws";

function setup(overrides: Partial<ConstructorParameters<typeof EventStreamClient>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  const events: FabricEvent[] = [];
  const resyncs: string[] = [];
  const statuses: string[] = [];
  const onUnauthorized = vi.fn();
  const onHandshakeFailed = vi.fn();
  const client = new EventStreamClient({
    baseUrl: BASE,
    socketFactory: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    onEvent: (e) => events.push(e),
    onResync: (r) => resyncs.push(r),
    onStatus: (s) => statuses.push(s),
    onUnauthorized,
    onHandshakeFailed,
    random: () => 1,
    ...overrides,
  });
  const last = (): FakeSocket => {
    const socket = sockets.at(-1);
    if (socket === undefined) throw new Error("no socket");
    return socket;
  };
  return { client, sockets, events, resyncs, statuses, onUnauthorized, onHandshakeFailed, last };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EventStreamClient", () => {
  it("first connect sends no position; hello makes it live", () => {
    const t = setup();
    t.client.start();
    expect(t.last().url).toBe(BASE);
    expect(t.statuses).toEqual(["connecting"]);
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 7, resumed: false });
    expect(t.client.status).toBe("live");
    expect([t.client.epoch, t.client.seq]).toEqual(["e1", 7]);
  });

  it("delivers events in order, advances seq, drops replays and garbage", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 7, resumed: false });
    t.last().send({ seq: 8, type: "job.state", at: 1.5, data: { job_id: "j1", state: "running" } });
    t.last().send({ seq: 8, type: "job.state", at: 1.5, data: { job_id: "j1", state: "running" } });
    t.last().send("not json");
    t.last().send([1, 2]);
    t.last().send({ type: "missing seq" });
    t.last().send({ seq: 9, type: "node.status", at: 2, data: { node_id: "n1", state: "online", connected: true } });
    expect(t.events.map((e) => e.seq)).toEqual([8, 9]);
    expect(t.events[0]?.data).toEqual({ job_id: "j1", state: "running" });
    expect(t.client.seq).toBe(9);
  });

  it("reconnects with since+epoch after a drop and resumes", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 7, resumed: false });
    t.last().send({ seq: 8, type: "job.state", at: 0, data: {} });
    t.last().serverClose(1006);
    expect(t.client.status).toBe("reconnecting");
    vi.advanceTimersByTime(500); // failures=0 -> base * 2^0, random()=1
    expect(t.sockets).toHaveLength(2);
    expect(t.last().url).toBe(`${BASE}?since=8&epoch=e1`);
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 12, resumed: true });
    // A resumed hello does not skip ahead: the backlog 9..12 is still coming.
    expect(t.client.seq).toBe(8);
    t.last().send({ seq: 10, type: "job.state", at: 0, data: {} });
    expect(t.events.map((e) => e.seq)).toEqual([8, 10]);
    expect(t.resyncs).toEqual([]);
  });

  it("a resync frame tells the app to refetch and restarts the position", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 3, resumed: false });
    t.last().serverClose(1006);
    vi.advanceTimersByTime(500);
    t.last().open();
    t.last().send({ type: "resync", epoch: "e2", seq: 40, reason: "epoch" });
    expect(t.resyncs).toEqual(["epoch"]);
    expect([t.client.epoch, t.client.seq]).toEqual(["e2", 40]);
    expect(t.client.status).toBe("live");
  });

  it("backs off exponentially with a cap while the controller is down", () => {
    const t = setup({ backoffBaseMs: 100, backoffCapMs: 1000 });
    expect([0, 1, 2, 3, 4, 5].map((n) => t.client.backoffMs(n))).toEqual([100, 200, 400, 800, 1000, 1000]);
    t.client.start();
    t.last().serverClose(1006); // never opened
    vi.advanceTimersByTime(99);
    expect(t.sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(2);
    t.last().serverClose(1006);
    vi.advanceTimersByTime(199);
    expect(t.sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(t.sockets).toHaveLength(3);
  });

  it("jitter scales the delay (full jitter)", () => {
    const t = setup({ random: () => 0.25, backoffBaseMs: 1000 });
    expect(t.client.backoffMs(2)).toBe(1000);
  });

  it("a refused handshake asks the app to re-check its session and keeps retrying", () => {
    const t = setup();
    t.client.start();
    t.last().serverClose(1006);
    expect(t.onHandshakeFailed).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(500);
    expect(t.sockets).toHaveLength(2);
  });

  it("4401 (session ended) stops for good and reports unauthorized", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 1, resumed: false });
    t.last().serverClose(CLOSE_UNAUTHORIZED);
    expect(t.onUnauthorized).toHaveBeenCalledTimes(1);
    expect(t.client.status).toBe("stopped");
    vi.advanceTimersByTime(60_000);
    expect(t.sockets).toHaveLength(1);
  });

  it("4008 (slow consumer) reconnects from where it was", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 5, resumed: false });
    t.last().serverClose(CLOSE_SLOW_CONSUMER);
    vi.advanceTimersByTime(500);
    expect(t.last().url).toBe(`${BASE}?since=5&epoch=e1`);
  });

  it("stop() closes the socket and cancels a pending reconnect", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.client.stop();
    expect(t.sockets[0]?.closed?.code).toBe(1000);
    expect(t.client.status).toBe("stopped");

    const u = setup();
    u.client.start();
    u.last().serverClose(1006);
    u.client.stop();
    vi.advanceTimersByTime(60_000);
    expect(u.sockets).toHaveLength(1);
  });

  it("a resync mid-stream is a control frame: onResync, new position, no event (T-1)", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 5, resumed: false });
    t.last().send({ seq: 6, type: "job.state", at: 0, data: {} });
    t.last().send({ type: "resync", epoch: "e1", seq: 120, reason: "ring" });
    expect(t.resyncs).toEqual(["ring"]);
    expect([t.client.epoch, t.client.seq]).toEqual(["e1", 120]);
    t.last().send({ seq: 121, type: "job.state", at: 0, data: {} });
    expect(t.events.map((e) => e.seq)).toEqual([6, 121]);
    expect(t.events.map((e) => e.type)).not.toContain("resync");
  });

  it("a hello mid-stream: same epoch is ignored, a new epoch is a resync (T-1)", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ type: "hello", epoch: "e1", seq: 5, resumed: false });
    t.last().send({ type: "hello", epoch: "e1", seq: 99, resumed: false });
    expect(t.resyncs).toEqual([]);
    expect(t.client.seq).toBe(5);
    t.last().send({ type: "hello", epoch: "e2", seq: 3, resumed: false });
    expect(t.resyncs).toEqual(["epoch"]);
    expect([t.client.epoch, t.client.seq]).toEqual(["e2", 3]);
    expect(t.events).toEqual([]);
  });

  it("ignores a first frame that is not hello/resync", () => {
    const t = setup();
    t.client.start();
    t.last().open();
    t.last().send({ seq: 1, type: "job.state", at: 0, data: {} });
    expect(t.events).toEqual([]);
    expect(t.client.status).toBe("connecting");
  });

  it("builds wss for https pages and ws for http (loopback_plain)", () => {
    expect(defaultEventsUrl({ protocol: "https:", host: "10.0.0.5:47110" } as Location)).toBe(
      "wss://10.0.0.5:47110/v1/events/ws",
    );
    expect(defaultEventsUrl({ protocol: "http:", host: "127.0.0.1:47111" } as Location)).toBe(
      "ws://127.0.0.1:47111/v1/events/ws",
    );
  });
});

describe("applyEvent", () => {
  it("node.status invalidates node queries; job events invalidate the job and the list", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, { seq: 1, type: "node.status", at: 0, data: { node_id: "n1" } });
    expect(spy).toHaveBeenLastCalledWith({ queryKey: queryKeys.nodes });
    applyEvent(qc, { seq: 2, type: "attempt.state", at: 0, data: { job_id: "j9" } });
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.job("j9") });
    expect(spy).toHaveBeenLastCalledWith({ queryKey: queryKeys.jobs, exact: true });
    spy.mockClear();
    applyEvent(qc, { seq: 3, type: "node.metrics", at: 0, data: {} });
    applyEvent(qc, { seq: 4, type: "from.the.future", at: 0, data: {} });
    expect(spy).not.toHaveBeenCalled();
  });

  it("resync invalidates everything", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyResync(qc);
    expect(spy).toHaveBeenCalledWith();
  });
});
