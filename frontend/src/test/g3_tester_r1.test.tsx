/**
 * V0.8 G3 tester r1 (*_v08_g3_tester_r1*): adversarial checks derived from
 * brief 5c / D191 / 0.8.7 - figures equal the API under churn, resync, clock
 * skew, hostile events, user vs admin. `it.fails` = a real defect (T-n in the
 * progress file); it flips to red when the defect is fixed.
 */
import { QueryClient, type InfiniteData } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setUnauthorizedHandler } from "../api/client";
import type { NodeList, NodeSummary } from "../api/endpoints";
import type { AlertList, NodeMetrics } from "../api/fabric";
import { queryKeys } from "../api/queryKeys";
import { applyEvent, applyResync } from "../events/applyEvent";
import {
  noteServerTime,
  resetServerClock,
  serverClockOffsetMs,
  serverNow,
} from "../lib/serverClock";
import {
  ADMIN_SESSION,
  event,
  fakeController,
  json,
  node,
  renderApp,
  type Handler,
} from "./fakeFabric";

const GiB = 1024 ** 3;

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setUnauthorizedHandler(null);
  resetServerClock();
});

beforeEach(() => {
  document.documentElement.dataset.theme = "dark";
  document.cookie = "mf_csrf=csrf-value; path=/";
});

function qcWith(...nodes: ReturnType<typeof node>[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(queryKeys.nodes, { count: nodes.length, nodes });
  return qc;
}

function metric(nodeId: string, observedAt: string, cpu = 50) {
  return {
    node_id: nodeId,
    observed_at: observedAt,
    cpu_percent: cpu,
    ram_used_bytes: 1,
    ram_total_bytes: 2,
    gpus: [],
  };
}

function series(qc: QueryClient, nodeId: string, times: string[]) {
  qc.setQueryData(queryKeys.nodeMetrics(nodeId), {
    node_id: nodeId,
    window_s: 300,
    count: times.length,
    samples: times.map((t, i) => ({ ...metric(nodeId, t, i), age_s: 0 })),
  });
}

const samplesOf = (qc: QueryClient, id: string) =>
  qc.getQueryData<NodeMetrics>(queryKeys.nodeMetrics(id))?.samples ?? [];

describe("metrics window (300 s, cap 200) under hostile samples", () => {
  // T-1 (minor): the sample list is appended in arrival order, so a late sample is drawn out of place.
  it.fails("T-1 (defect) sample list stays sorted: see above", () => {
    const qc = qcWith(node("n1"));
    series(qc, "n1", ["2026-10-16T10:00:00Z", "2026-10-16T10:00:20Z"]);
    applyEvent(qc, event("node.metrics", metric("n1", "2026-10-16T10:00:05Z")));
    const times = samplesOf(qc, "n1").map((s) => Date.parse(s.observed_at));
    expect(times).toEqual([...times].sort((a, b) => a - b));
  });

  // T-2 (minor): the window is cut relative to the NEW point, so one sample stamped far in the future
  // (an agent clock jump) erases the whole history.
  it.fails(
    "T-2 (defect) one future-stamped sample must not erase the series",
    () => {
      const qc = qcWith(node("n1"));
      series(qc, "n1", [
        "2026-10-16T10:00:00Z",
        "2026-10-16T10:00:10Z",
        "2026-10-16T10:00:20Z",
      ]);
      applyEvent(
        qc,
        event("node.metrics", metric("n1", "2026-10-16T14:00:00Z")),
      );
      expect(samplesOf(qc, "n1").length).toBeGreaterThan(1);
    },
  );

  it("never exceeds 200 points however fast samples arrive (1 Hz for 10 minutes)", () => {
    const qc = qcWith(node("n1"));
    series(qc, "n1", ["2026-10-16T10:00:00Z"]);
    const base = Date.parse("2026-10-16T10:00:00Z");
    for (let i = 1; i <= 600; i++) {
      applyEvent(
        qc,
        event(
          "node.metrics",
          metric("n1", new Date(base + i * 1000).toISOString(), i % 100),
        ),
      );
      expect(samplesOf(qc, "n1").length).toBeLessThanOrEqual(200);
    }
    const s = samplesOf(qc, "n1");
    expect(
      Date.parse(s.at(-1)?.observed_at ?? "") -
        Date.parse(s[0]?.observed_at ?? ""),
    ).toBeLessThanOrEqual(300_000);
    expect(
      qc.getQueryData<NodeMetrics>(queryKeys.nodeMetrics("n1"))?.count,
    ).toBe(s.length);
  });

  // T-3 (minor): a metrics event that arrives after the node went offline brings the live numbers back.
  it.fails(
    "T-3 (defect) a late metrics event must not revive numbers on a disconnected node (D16)",
    () => {
      const qc = qcWith(node("n1"));
      applyEvent(
        qc,
        event("node.status", {
          node_id: "n1",
          state: "offline",
          connected: false,
        }),
      );
      applyEvent(
        qc,
        event("node.metrics", metric("n1", "2026-10-16T10:00:00Z", 88)),
      );
      const row = qc.getQueryData<NodeList>(queryKeys.nodes)?.nodes[0];
      expect(row?.connected).toBe(false);
      expect(row?.cpu_percent).toBeNull();
    },
  );
});

describe("hostile and odd events", () => {
  it("applyEvent never throws for unknown types, empty data, wrong types", () => {
    const qc = qcWith(node("n1"));
    for (const e of [
      event("nope", {}),
      event("node.status", {}),
      event("node.status", { node_id: 7, state: {}, connected: "yes" }),
      event("node.metrics", {}),
      event("node.metrics", {
        node_id: "n1",
        observed_at: 5,
        cpu_percent: Number.NaN,
      }),
      event("node.metrics", { ...metric("n1", "garbage-time") }),
      event("alert", {}),
      event("alert", { id: 5, kind: "wrong_node" }),
      event("job.state", {}),
      event("attempt.state", { job_id: 9 }),
    ]) {
      expect(() => {
        applyEvent(qc, e);
      }).not.toThrow();
    }
  });

  it("a numeric (non-string) alert id is refetched, not merged under a wrong key", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("alert", { id: 12, kind: "wrong_node" }));
    expect(spy).toHaveBeenCalledWith({ queryKey: queryKeys.alerts });
  });

  it("an alert already on page 2 is not added to page 1", () => {
    const qc = new QueryClient();
    const mk = (ids: string[]): AlertList => ({
      count: ids.length,
      next_cursor: null,
      alerts: ids.map((id) => ({
        id,
        at: "2026-10-16T00:00:00Z",
        kind: "wrong_node",
        node_id: "n",
        actor: "node:n",
        details: {},
      })),
    });
    qc.setQueryData<InfiniteData<AlertList, string | undefined>>(
      queryKeys.alerts,
      { pages: [mk(["9", "8"]), mk(["7", "6"])], pageParams: [undefined, "8"] },
    );
    applyEvent(
      qc,
      event("alert", { id: "6", kind: "wrong_node", node_id: "n" }),
    );
    const data = qc.getQueryData<InfiniteData<AlertList>>(queryKeys.alerts);
    expect(data?.pages.flatMap((p) => p.alerts.map((a) => a.id))).toEqual([
      "9",
      "8",
      "7",
      "6",
    ]);
  });

  it("an alert with no node keeps actor empty and does not invent one", () => {
    const qc = new QueryClient();
    qc.setQueryData<InfiniteData<AlertList, string | undefined>>(
      queryKeys.alerts,
      {
        pages: [{ alerts: [], count: 0, next_cursor: null }],
        pageParams: [undefined],
      },
    );
    applyEvent(qc, event("alert", { id: "3", kind: "wrong_node" }));
    const first = qc.getQueryData<InfiniteData<AlertList>>(queryKeys.alerts)
      ?.pages[0]?.alerts[0];
    expect(first).toMatchObject({ id: "3", node_id: null, actor: "" });
  });

  it("a resync invalidates every query (nothing shown is trusted)", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyResync(qc);
    expect(spy).toHaveBeenCalledWith();
  });
});

describe("browser clock skew (serverClock)", () => {
  it("a browser 10 minutes behind or ahead still sees the controller's now", () => {
    for (const skew of [-600_000, 600_000, 86_400_000]) {
      resetServerClock();
      const local = Date.now();
      for (let i = 0; i < 5; i++) noteServerTime(local + skew, local);
      expect(Math.abs(serverNow(local) - (local + skew))).toBeLessThan(1);
    }
  });

  it("one wild sample among normal ones moves nothing (median)", () => {
    const local = Date.now();
    for (let i = 0; i < 8; i++) noteServerTime(local + 500, local);
    noteServerTime(local + 3_600_000, local);
    expect(serverClockOffsetMs()).toBe(500);
  });

  it("the offset follows the controller after the browser clock is corrected", () => {
    const local = Date.now();
    for (let i = 0; i < 15; i++) noteServerTime(local + 120_000, local);
    for (let i = 0; i < 15; i++) noteServerTime(local, local);
    expect(serverClockOffsetMs()).toBe(0);
  });

  // T-4 (minor): the stream coerces a frame with no numeric `at` to 0, and 0 is "finite": the clock offset
  // becomes -56 years and "last heartbeat" prints nonsense until 8 good samples arrive.
  it.fails(
    "T-4 (defect) an event with no usable `at` must not move the clock",
    () => {
      const qc = qcWith(node("n1"));
      applyEvent(
        qc,
        event(
          "node.status",
          { node_id: "n1", state: "online", connected: true },
          0,
        ),
      );
      expect(Math.abs(serverClockOffsetMs())).toBeLessThan(60_000);
    },
  );
});

/* ---- figures equal the API under churn ------------------------------- */

interface Truth {
  nodes: Record<string, unknown>[];
}

function lcg(seed: number) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

describe("Overview and Nodes equal the API under churn", () => {
  it("after 60 random status/metrics/revoke/re-enroll events the figures are the API's", async () => {
    const rnd = lcg(7);
    const truth: Truth = {
      nodes: ["a", "b", "c", "d"].map((id) =>
        node(`n_${id}`, {
          hostname: `h-${id}`,
          cpu_percent: 10,
          ram_used_bytes: GiB,
          ram_total_bytes: 8 * GiB,
          gpus: [],
        }),
      ),
    };
    const routes: Record<string, Handler> = {
      "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
      "GET /v1/nodes": () =>
        json(200, { count: truth.nodes.length, nodes: truth.nodes }),
      "GET /healthz": () => json(200, { status: "ok", sessions: 1 }),
      "GET /v1/jobs": () =>
        json(200, { count: 0, next_cursor: null, jobs: [] }),
      "GET /v1/alerts": () =>
        json(200, { count: 0, next_cursor: null, alerts: [] }),
    };
    fakeController(routes);
    const app = renderApp("/");
    await screen.findByTestId("nodes-total");
    act(() => {
      app.socket().greet();
    });
    const find = (id: string) => truth.nodes.find((n) => n.node_id === id);
    for (let i = 0; i < 60; i++) {
      const id = `n_${"abcd"[Math.floor(rnd() * 4)] ?? "a"}`;
      const n = find(id);
      if (n === undefined) continue;
      const roll = rnd();
      let frame;
      if (roll < 0.45) {
        const cpu = Math.round(rnd() * 100);
        if (n.state === "revoked") continue;
        Object.assign(n, {
          cpu_percent: cpu,
          connected: true,
          state: "online",
          sample_age_s: 0,
        });
        frame = event("node.metrics", {
          node_id: id,
          observed_at: new Date().toISOString(),
          cpu_percent: cpu,
          ram_used_bytes: GiB,
          ram_total_bytes: 8 * GiB,
          gpus: [],
        });
      } else if (roll < 0.75) {
        if (n.state === "revoked") continue;
        Object.assign(n, {
          state: "offline",
          connected: false,
          cpu_percent: null,
          ram_used_bytes: null,
          ram_total_bytes: null,
          gpus: [],
          sample_age_s: null,
        });
        frame = event("node.status", {
          node_id: id,
          state: "offline",
          connected: false,
        });
      } else if (roll < 0.85) {
        Object.assign(n, {
          state: "revoked",
          connected: false,
          cpu_percent: null,
          ram_used_bytes: null,
          ram_total_bytes: null,
          gpus: [],
        });
        frame = event("node.status", {
          node_id: id,
          state: "revoked",
          connected: false,
        });
      } else {
        // re-enrol: a brand-new node id appears
        const fresh = `n_new${String(i)}`;
        truth.nodes.push(
          node(fresh, {
            hostname: `h-${fresh}`,
            cpu_percent: 5,
            ram_used_bytes: GiB,
            ram_total_bytes: 8 * GiB,
            gpus: [],
          }),
        );
        frame = event("node.status", {
          node_id: fresh,
          state: "online",
          connected: true,
        });
      }
      act(() => {
        app.socket().send(frame);
      });
    }
    const members = truth.nodes.filter((n) => n.state !== "revoked");
    const online = truth.nodes.filter((n) => n.state === "online");
    const live = truth.nodes.filter(
      (n) => n.connected === true && n.cpu_percent != null,
    );
    await waitFor(() => {
      expect(screen.getByTestId("nodes-total").textContent).toBe(
        String(truth.nodes.length),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("health-fabric").textContent).toContain(
        online.length === members.length
          ? `All ${String(members.length)}`
          : `${String(online.length)} of ${String(members.length)}`,
      );
    });
    for (const state of ["online", "offline", "revoked"]) {
      const want = truth.nodes.filter((n) => n.state === state).length;
      if (want === 0) expect(screen.queryByTestId(`nodes-${state}`)).toBeNull();
      else
        expect(screen.getByTestId(`nodes-${state}`).textContent).toBe(
          String(want),
        );
    }
    if (live.length > 0) {
      const mean =
        live.reduce((a, n) => a + Number(n.cpu_percent), 0) / live.length;
      await waitFor(() => {
        expect(screen.getByTestId("util-cpu").textContent).toBe(
          `${String(Math.round(mean))}%`,
        );
      });
      expect(screen.getByTestId("util-reporting").textContent).toBe(
        `From ${String(live.length)} of ${String(truth.nodes.length)} nodes reporting now.`,
      );
    }
  });
});

describe("event stream reconnect / resync on the Overview", () => {
  it("a resync frame makes the page refetch nodes, jobs, health and alerts", async () => {
    const ctl = fakeController({
      "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
      "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("n1")] }),
      "GET /healthz": () => json(200, { status: "ok", sessions: 1 }),
      "GET /v1/jobs": () =>
        json(200, { count: 0, next_cursor: null, jobs: [] }),
      "GET /v1/alerts": () =>
        json(200, { count: 0, next_cursor: null, alerts: [] }),
    });
    const app = renderApp("/");
    await screen.findByTestId("nodes-total");
    await screen.findByTestId("alerts-empty");
    act(() => {
      app.socket().greet();
    });
    const before = {
      nodes: ctl.count("GET /v1/nodes"),
      jobs: ctl.count("GET /v1/jobs"),
      alerts: ctl.count("GET /v1/alerts"),
    };
    act(() => {
      app
        .socket()
        .send({ type: "resync", epoch: "e2", seq: 500, reason: "ring" });
    });
    await waitFor(() => {
      expect(ctl.count("GET /v1/nodes")).toBeGreaterThan(before.nodes);
      expect(ctl.count("GET /v1/jobs")).toBeGreaterThan(before.jobs);
      expect(ctl.count("GET /v1/alerts")).toBeGreaterThan(before.alerts);
    });
  });
});

describe("user vs admin", () => {
  it("a user never gets the revoke action or the alerts API on the Overview or a node page", async () => {
    const user = {
      ...ADMIN_SESSION,
      user: { id: "u", name: "alice", role: "user" },
    };
    const ctl = fakeController({
      "GET /v1/session": () => json(200, { session: user }),
      "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("n1")] }),
      "GET /healthz": () => json(200, { status: "ok", sessions: 1 }),
      "GET /v1/jobs": () =>
        json(200, { count: 0, next_cursor: null, jobs: [] }),
    });
    const app = renderApp("/");
    await screen.findByTestId("nodes-total");
    act(() => {
      app.socket().greet();
    });
    // an alert event leaking to a user's stream must not create alert state
    act(() => {
      app
        .socket()
        .send(event("alert", { id: "1", kind: "wrong_node", node_id: "n1" }));
    });
    expect(ctl.count("GET /v1/alerts")).toBe(0);
    expect(app.queryClient.getQueryData(queryKeys.alerts)).toBeUndefined();
    expect(screen.queryByRole("button", { name: /revoke/i })).toBeNull();
  });
});

describe("node rows keep their identity while sorting under live updates", () => {
  it("a metrics event for a node not in the list does not add a row", () => {
    const qc = qcWith(node("n1"));
    applyEvent(
      qc,
      event("node.metrics", metric("ghost", "2026-10-16T10:00:00Z")),
    );
    const rows = qc.getQueryData<NodeList>(queryKeys.nodes)?.nodes ?? [];
    expect(rows.map((n: NodeSummary) => n.node_id)).toEqual(["n1"]);
  });
});
