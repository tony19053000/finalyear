/**
 * V0.8 G3 live wiring (0.8.7): what each event does to the query cache - the
 * client keeps no second store (D172).
 */
import { QueryClient, type InfiniteData } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NodeList } from "../api/endpoints";
import type { AlertList, NodeDetail, NodeMetrics } from "../api/fabric";
import { queryKeys } from "../api/queryKeys";
import { MAX_SERIES_POINTS, applyEvent } from "../events/applyEvent";
import { resetServerClock, serverClockOffsetMs } from "../lib/serverClock";
import { event, node, nodeDetail } from "./fakeFabric";

afterEach(() => {
  resetServerClock();
});

function client(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function seeded() {
  const qc = client();
  qc.setQueryData(queryKeys.nodes, { count: 2, nodes: [node("n1"), node("n2")] });
  qc.setQueryData(queryKeys.node("n1"), nodeDetail("n1"));
  return qc;
}

const list = (qc: QueryClient) => qc.getQueryData<NodeList>(queryKeys.nodes)?.nodes ?? [];

function metric(nodeId: string, observedAt: string, cpu = 77) {
  return {
    node_id: nodeId,
    observed_at: observedAt,
    cpu_percent: cpu,
    ram_used_bytes: 5,
    ram_total_bytes: 10,
    gpus: [{ index: 0, util_percent: 12, vram_used_bytes: 1, vram_total_bytes: 2 }],
  };
}

describe("node.status", () => {
  it("patches the list and the detail at once, then invalidates both (exact)", () => {
    const qc = seeded();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("node.status", { node_id: "n1", state: "suspect", connected: false }));
    const [n1, n2] = list(qc);
    expect([n1?.state, n1?.connected, n1?.cpu_percent, n1?.gpus]).toEqual(["suspect", false, null, []]);
    expect(n2?.state).toBe("online"); // others untouched
    expect(qc.getQueryData<NodeDetail>(queryKeys.node("n1"))?.state).toBe("suspect");
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.nodes, exact: true });
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.node("n1"), exact: true });
    // The metrics series is not refetched by a status change.
    expect(spy).not.toHaveBeenCalledWith(expect.objectContaining({ queryKey: queryKeys.nodeMetrics("n1") }));
  });

  it("state null (session gone) keeps the last state, marks it disconnected", () => {
    const qc = seeded();
    applyEvent(qc, event("node.status", { node_id: "n2", state: null, connected: false }));
    expect(list(qc)[1]).toMatchObject({ state: "online", connected: false, cpu_percent: null });
  });

  it("a malformed event changes nothing", () => {
    const qc = seeded();
    const before = qc.getQueryData(queryKeys.nodes);
    applyEvent(qc, event("node.status", { state: "offline" }));
    expect(qc.getQueryData(queryKeys.nodes)).toBe(before);
  });
});

describe("node.metrics", () => {
  it("patches numbers + heartbeat, appends one point; never refetches", () => {
    const qc = seeded();
    const at = Date.parse("2026-10-16T10:00:05Z") / 1000;
    qc.setQueryData(queryKeys.nodeMetrics("n1"), {
      node_id: "n1",
      window_s: 300,
      count: 1,
      samples: [{ ...metric("n1", "2026-10-16T10:00:00Z", 10), age_s: 3 }],
    });
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("node.metrics", metric("n1", "2026-10-16T10:00:04Z"), at));
    expect(list(qc)[0]).toMatchObject({
      cpu_percent: 77,
      ram_used_bytes: 5,
      sample_age_s: 0,
      last_seen_at: "2026-10-16T10:00:05.000Z",
      gpus: [{ index: 0, util_percent: 12, vram_used_bytes: 1, vram_total_bytes: 2 }],
    });
    expect(qc.getQueryData<NodeDetail>(queryKeys.node("n1"))?.cpu_percent).toBe(77);
    const series = qc.getQueryData<NodeMetrics>(queryKeys.nodeMetrics("n1"));
    expect(series?.samples.map((s) => s.cpu_percent)).toEqual([10, 77]);
    expect(series?.count).toBe(2);
    // The same sample again (a replay after reconnect) is not appended twice.
    applyEvent(qc, event("node.metrics", metric("n1", "2026-10-16T10:00:04Z"), at));
    expect(qc.getQueryData<NodeMetrics>(queryKeys.nodeMetrics("n1"))?.samples).toHaveLength(2);
    expect(spy).not.toHaveBeenCalled();
  });

  it("caps the series and drops points older than the window", () => {
    const qc = seeded();
    const base = Date.parse("2026-10-16T10:00:00Z");
    const samples = Array.from({ length: MAX_SERIES_POINTS }, (_, i) => ({
      ...metric("n1", new Date(base + i * 1000).toISOString(), i),
      age_s: 0,
    }));
    qc.setQueryData(queryKeys.nodeMetrics("n1"), { node_id: "n1", window_s: 300, count: samples.length, samples });
    applyEvent(qc, event("node.metrics", metric("n1", new Date(base + 400_000).toISOString(), 1)));
    const after = qc.getQueryData<NodeMetrics>(queryKeys.nodeMetrics("n1"))?.samples ?? [];
    expect(after.length).toBeLessThanOrEqual(MAX_SERIES_POINTS);
    // Everything more than 300 s before the new point is gone.
    expect(after.every((s) => Date.parse(s.observed_at) >= base + 100_000)).toBe(true);
    expect(after.at(-1)?.cpu_percent).toBe(1);
  });

  it("a series nobody loaded is not created; a bad payload is ignored", () => {
    const qc = seeded();
    applyEvent(qc, event("node.metrics", metric("n2", "2026-10-16T10:00:04Z")));
    expect(qc.getQueryData(queryKeys.nodeMetrics("n2"))).toBeUndefined();
    const before = qc.getQueryData(queryKeys.nodes);
    applyEvent(qc, event("node.metrics", { node_id: "n1", cpu_percent: "high" }));
    expect(qc.getQueryData(queryKeys.nodes)).toBe(before);
  });

  it("feeds the controller clock offset", () => {
    const qc = seeded();
    applyEvent(qc, event("node.metrics", metric("n1", "x"), Date.now() / 1000 + 120));
    expect(serverClockOffsetMs()).toBeGreaterThan(119_000);
  });
});

describe("alert", () => {
  const page = (ids: string[]): AlertList => ({
    count: ids.length,
    next_cursor: null,
    alerts: ids.map((id) => ({ id, at: "2026-10-16T00:00:00Z", kind: "wrong_node", node_id: "n2", actor: "node:n2", details: {} })),
  });

  it("is merged into the first page by id - once", () => {
    const qc = client();
    qc.setQueryData<InfiniteData<AlertList, string | undefined>>(queryKeys.alerts, { pages: [page(["5", "4"])], pageParams: [undefined] });
    const live = event("alert", { id: "6", kind: "wrong_node", node_id: "n2", attempt_id: "att_1", reported: "done", job_id: "job_1", placed_node_id: "n1" });
    applyEvent(qc, live);
    applyEvent(qc, live); // replayed after a reconnect
    applyEvent(qc, event("alert", { id: "5", kind: "wrong_node", node_id: "n2" })); // already listed
    const data = qc.getQueryData<InfiniteData<AlertList>>(queryKeys.alerts);
    expect(data?.pages[0]?.alerts.map((a) => a.id)).toEqual(["6", "5", "4"]);
    expect(data?.pages[0]?.alerts[0]).toMatchObject({
      kind: "wrong_node",
      node_id: "n2",
      actor: "node:n2",
      details: { attempt_id: "att_1", reported: "done", job_id: "job_1", placed_node_id: "n1" },
    });
  });

  it("an alert without an id refetches instead of guessing; none loaded = nothing kept", () => {
    const qc = client();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("alert", { kind: "wrong_node" }));
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.alerts });
    applyEvent(qc, event("alert", { id: "1", kind: "wrong_node" }));
    expect(qc.getQueryData(queryKeys.alerts)).toBeUndefined();
  });
});

describe("job events", () => {
  it("invalidate the job, the plain list and the Overview counts", () => {
    const qc = client();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("job.state", { job_id: "job_1", state: "running" }));
    expect(spy.mock.calls.map((c) => c[0])).toEqual([
      { queryKey: queryKeys.job("job_1") },
      { queryKey: queryKeys.jobs, exact: true },
      { queryKey: queryKeys.activeJobs },
    ]);
  });
});
