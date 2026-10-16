/**
 * V0.8 G3 pages against a fake controller: every number shown is the API's
 * (checklist 26), empty/zero states are honest, live events change the page
 * without a refetch, admin-only parts stay admin-only.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CSRF_HEADER, setUnauthorizedHandler } from "../api/client";
import { resetServerClock } from "../lib/serverClock";
import {
  ADMIN_SESSION,
  EMPTY_NETWORK,
  USER_SESSION,
  event,
  fakeController,
  json,
  node,
  nodeDetail,
  renderApp,
  type Handler,
} from "./fakeFabric";

const GiB = 1024 ** 3;

function job(id: string, state: string) {
  return {
    job_id: id,
    name: id,
    state,
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    placement: { mode: "auto", node_id: null, policy: null },
  };
}

const NODES = [
  node("node_A", {
    hostname: "lab-a",
    cpu_percent: 30,
    ram_used_bytes: 4 * GiB,
    ram_total_bytes: 16 * GiB,
    gpus: [{ index: 0, util_percent: 50, vram_used_bytes: 1 * GiB, vram_total_bytes: 4 * GiB }],
  }),
  node("node_B", { hostname: "lab-b", cpu_percent: 70, ram_used_bytes: 12 * GiB, ram_total_bytes: 16 * GiB }),
  node("node_C", { hostname: "lab-c", state: "offline", connected: false, cpu_percent: null, ram_used_bytes: null, ram_total_bytes: null }),
];

const ALERT = {
  id: "41",
  at: new Date(Date.now() - 60_000).toISOString(),
  kind: "wrong_node",
  node_id: "node_B",
  actor: "node:node_B",
  details: { attempt_id: "att_01J0000000000000000000ATT1", job_id: "job_1", placed_node_id: "node_A", reported: "done" },
};

function routes(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
    "GET /v1/nodes": () => json(200, { count: NODES.length, nodes: NODES }),
    "GET /healthz": () => json(200, { status: "ok", sessions: 2 }),
    "GET /v1/jobs": () =>
      json(200, { count: 4, next_cursor: null, jobs: [job("j1", "running"), job("j2", "running"), job("j3", "running"), job("j4", "queued")] }),
    "GET /v1/alerts": () => json(200, { count: 1, next_cursor: null, alerts: [ALERT] }),
    ...overrides,
  };
}

beforeEach(() => {
  document.documentElement.dataset.theme = "dark";
  document.cookie = "mf_csrf=csrf-value; path=/";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setUnauthorizedHandler(null);
  resetServerClock();
});

describe("Overview", () => {
  it("shows exactly the API's numbers", async () => {
    const ctl = fakeController(routes());
    renderApp("/");
    expect(await screen.findByTestId("nodes-total")).toHaveProperty("textContent", "3");
    expect(screen.getByTestId("nodes-online").textContent).toBe("2");
    expect(screen.getByTestId("nodes-offline").textContent).toBe("1");
    expect(screen.getByTestId("health-fabric").textContent).toContain("2 of 3 nodes online");
    expect(await screen.findByText("2 agent sessions connected")).toBeTruthy();
    expect(screen.getByTestId("health-controller").textContent).toBe("OK");
    expect((await screen.findByTestId("jobs-active")).textContent).toBe("4");
    expect(screen.getByTestId("jobs-running").textContent).toBe("3");
    expect(screen.getByTestId("jobs-queued").textContent).toBe("1");
    // Utilisation over the 2 reporting nodes: (30+70)/2, 16/32 GiB, one GPU.
    expect(screen.getByTestId("util-cpu").textContent).toBe("50%");
    expect(screen.getByTestId("util-ram").textContent).toBe("16.0 GiB / 32.0 GiB (50%)");
    expect(screen.getByTestId("util-gpu").textContent).toBe("50%");
    expect(screen.getByTestId("util-vram").textContent).toBe("1.0 GiB / 4.0 GiB (25%)");
    expect(screen.getByTestId("util-reporting").textContent).toBe("From 2 of 3 nodes reporting now.");
    // The active-jobs request asks for every non-terminal state.
    const jobsCall = ctl.calls.find((c) => c.key === "GET /v1/jobs");
    const params = new URL(jobsCall?.url ?? "", "https://x").searchParams;
    expect(params.getAll("state").sort()).toEqual(["collecting", "planning", "preparing", "queued", "retrying", "running", "submitted"]);
    expect(params.get("limit")).toBe("500");
    // The alert, in words, from its own fields.
    const alerts = await screen.findByTestId("alerts-list");
    expect(alerts.textContent).toContain("lab-b reported done for attempt att_…00ATT1, which runs on lab-a");
  });

  it("an empty fabric says so and how to add a node; nothing invented", async () => {
    fakeController(
      routes({
        "GET /v1/nodes": () => json(200, { count: 0, nodes: [] }),
        "GET /v1/jobs": () => json(200, { count: 0, next_cursor: null, jobs: [] }),
        "GET /v1/alerts": () => json(200, { count: 0, next_cursor: null, alerts: [] }),
        "GET /healthz": () => json(200, { status: "ok", sessions: 0 }),
      }),
    );
    renderApp("/");
    expect(await screen.findByText("No nodes yet - create an invite")).toBeTruthy();
    expect(screen.getByLabelText("Add a node").textContent).toContain("meshforge-controller fabric invite");
    expect(screen.getByTestId("nodes-total").textContent).toBe("0");
    expect(await screen.findByText("No node is reporting resources right now.")).toBeTruthy();
    expect(await screen.findByText("No job is waiting or running.")).toBeTruthy();
    expect(await screen.findByTestId("alerts-empty")).toBeTruthy();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("more than one page of active jobs is shown as a lower bound", async () => {
    const many = Array.from({ length: 500 }, (_, i) => job(`j${String(i)}`, "queued"));
    fakeController(routes({ "GET /v1/jobs": () => json(200, { count: 500, next_cursor: "c2", jobs: many }) }));
    renderApp("/");
    expect((await screen.findByTestId("jobs-active")).textContent).toBe("500+");
    expect(screen.getByText(/there are more/)).toBeTruthy();
  });

  it("a user sees why alerts are empty; the admin-only API is never called", async () => {
    const ctl = fakeController(routes({ "GET /v1/session": () => json(200, { session: USER_SESSION }) }));
    renderApp("/");
    expect(await screen.findByText("Alerts are shown to admins only.")).toBeTruthy();
    await screen.findByTestId("nodes-total");
    expect(ctl.count("GET /v1/alerts")).toBe(0);
  });

  it("live: a new alert appears once; a metrics event moves the utilisation; a job event recounts", async () => {
    const ctl = fakeController(routes());
    const app = renderApp("/");
    await screen.findByTestId("alerts-list");
    act(() => {
      app.socket().greet();
    });
    const live = { id: "42", kind: "wrong_node", node_id: "node_A", attempt_id: "att_2", reported: "failed", job_id: "j", placed_node_id: "node_B" };
    act(() => {
      app.socket().send(event("alert", live));
      app.socket().send(event("alert", live));
    });
    await waitFor(() => {
      expect(screen.getByTestId("alerts-list").querySelectorAll("li")).toHaveLength(2);
    });
    expect(screen.getByTestId("alerts-list").querySelector("li")?.getAttribute("data-alert-id")).toBe("42");
    expect(ctl.count("GET /v1/alerts")).toBe(1); // merged, not refetched

    act(() => {
      app.socket().send(
        event("node.metrics", { node_id: "node_A", observed_at: new Date().toISOString(), cpu_percent: 90, ram_used_bytes: 4 * GiB, ram_total_bytes: 16 * GiB, gpus: [] }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("util-cpu").textContent).toBe("80%"); // (90 + 70) / 2
    });
    const before = ctl.count("GET /v1/jobs");
    act(() => {
      app.socket().send(event("job.state", { job_id: "j9", state: "queued" }));
    });
    await waitFor(() => {
      expect(ctl.count("GET /v1/jobs")).toBe(before + 1);
    });
  });
});

describe("Nodes list", () => {
  it("one row per API node, sortable and filterable, names are links", async () => {
    fakeController(routes());
    const user = userEvent.setup();
    renderApp("/nodes");
    const table = await screen.findByRole("table");
    const names = () => within(table).getAllByRole("link").map((a) => a.textContent);
    expect(names()).toEqual(["lab-a", "lab-b", "lab-c"]);
    expect(within(table).getAllByRole("link")[0]?.getAttribute("href")).toBe("/nodes/node_A");
    const cpu = within(table).getByRole("columnheader", { name: /CPU/ });
    expect(cpu.getAttribute("aria-sort")).toBe("none");
    await user.click(within(cpu).getByRole("button"));
    expect(cpu.getAttribute("aria-sort")).toBe("ascending");
    expect(names()).toEqual(["lab-c", "lab-a", "lab-b"]);
    await user.click(within(cpu).getByRole("button"));
    expect(cpu.getAttribute("aria-sort")).toBe("descending");
    expect(names()).toEqual(["lab-b", "lab-a", "lab-c"]);
    // Filter by status (counts are the API's).
    await user.click(screen.getByRole("button", { name: "offline (1)" }));
    expect(names()).toEqual(["lab-c"]);
    expect(screen.getByRole("button", { name: "offline (1)" }).getAttribute("aria-pressed")).toBe("true");
    await user.click(screen.getByRole("button", { name: "All (3)" }));
    expect(names()).toHaveLength(3);
    // The row's numbers.
    const rowB = within(table).getByText("lab-b").closest("tr");
    expect(rowB?.textContent).toContain("70%");
    expect(rowB?.textContent).toContain("12.0 GiB / 16.0 GiB");
    const rowC = within(table).getByText("lab-c").closest("tr");
    expect(rowC?.textContent).toContain("offline");
  });

  it("live: node.status and node.metrics change the row without a refetch round trip", async () => {
    let hold: (() => void) | null = null;
    let calls = 0;
    fakeController(
      routes({
        "GET /v1/nodes": () => {
          calls += 1;
          if (calls === 1) return json(200, { count: NODES.length, nodes: NODES });
          // The refetch after the event never answers: the patch alone must show it.
          return new Promise<Response>((resolve) => {
            hold = () => {
              resolve(json(200, { count: NODES.length, nodes: NODES }));
            };
          });
        },
      }),
    );
    const app = renderApp("/nodes");
    const table = await screen.findByRole("table");
    act(() => {
      app.socket().greet();
    });
    const rowA = () => within(table).getByText("lab-a").closest("tr");
    act(() => {
      app.socket().send(event("node.status", { node_id: "node_A", state: "suspect", connected: false }));
    });
    await waitFor(() => {
      expect(rowA()?.querySelector("[data-state]")?.getAttribute("data-state")).toBe("suspect");
    });
    expect(rowA()?.textContent).toContain("no live stream");
    act(() => {
      app.socket().send(
        event("node.metrics", { node_id: "node_B", observed_at: new Date().toISOString(), cpu_percent: 12, ram_used_bytes: 1, ram_total_bytes: 2, gpus: [] }),
      );
    });
    await waitFor(() => {
      expect(within(table).getByText("lab-b").closest("tr")?.textContent).toContain("12%");
    });
    expect(hold).not.toBeNull();
  });

  it("an empty fabric is an EmptyState with the join commands", async () => {
    fakeController(routes({ "GET /v1/nodes": () => json(200, { count: 0, nodes: [] }) }));
    renderApp("/nodes");
    expect(await screen.findByText("No nodes yet - create an invite")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

function detailRoutes(overrides: Record<string, Handler> = {}): Record<string, Handler> {
  const observed = new Date(Date.now() - 2000).toISOString();
  return routes({
    "GET /v1/nodes/node_A": () =>
      json(200, nodeDetail("node_A", { hostname: "lab-a", cache: { env: { count: 2, bytes: 3 * GiB } }, owner_policy: { accepting: true, max_ram_bytes: 8 * GiB, allowed_users_count: 2 } })),
    "GET /v1/nodes/node_A/metrics": () =>
      json(200, {
        node_id: "node_A",
        window_s: 300,
        count: 2,
        samples: [
          { node_id: "node_A", observed_at: new Date(Date.now() - 4000).toISOString(), cpu_percent: 20, ram_used_bytes: 4 * GiB, ram_total_bytes: 16 * GiB, gpus: [], age_s: 4 },
          { node_id: "node_A", observed_at: observed, cpu_percent: 25, ram_used_bytes: 4 * GiB, ram_total_bytes: 16 * GiB, gpus: [], age_s: 2 },
        ],
      }),
    "GET /v1/nodes/node_A/resources": () =>
      json(200, {
        node_id: "node_A",
        history: [],
        history_count: 0,
        live_count: 1,
        live: [
          { sequence: 1, session_epoch: 1, observed_at: observed, age_s: 2, cpu_percent: 25, ram_used_bytes: 1, ram_total_bytes: 2, gpus: [], disks: [{ mount: "/", free_bytes: 50 * GiB, total_bytes: 100 * GiB }] },
        ],
      }),
    "GET /v1/network": () =>
      json(200, {
        ...EMPTY_NETWORK,
        links: [
          { src_node_id: "node_A", dst_node_id: "node_B", rtt_ms: 2, bw_bytes_s: 50 * 1024 ** 2, samples: 4, measured_at: observed, age_s: 700, stale: true, source: "measured", last_failure: null },
        ],
      }),
    ...overrides,
  });
}

describe("Node detail", () => {
  it("summary, live samples, hardware, network (stale/unknown labelled), caches and policy", async () => {
    fakeController(detailRoutes());
    const user = userEvent.setup();
    renderApp("/nodes/node_A");
    expect(await screen.findByRole("heading", { level: 1, name: "lab-a" })).toBeTruthy();
    expect(screen.getByTestId("node-status").textContent).toContain("online");
    expect(await screen.findByTestId("live-cpu")).toHaveProperty("textContent", "25%");
    expect(screen.getByTestId("live-ram").textContent).toBe("4.0 GiB / 16.0 GiB");
    expect(screen.getByRole("img", { name: /CPU: now 25%, min 20%, max 25% over 2 samples/ })).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: "Hardware" }));
    const hw = await screen.findByTestId("hardware-card");
    expect(hw.textContent).toContain("Ryzen 7 5800H");
    expect(hw.textContent).toContain("8 / 16");
    expect(hw.textContent).toContain("RTX 3050");
    expect(hw.textContent).toContain("4.0 GiB");
    expect(await screen.findByText("50.0 GiB")).toBeTruthy(); // the disk's free space

    await user.click(screen.getByRole("tab", { name: "Network" }));
    const net = await screen.findByTestId("network-card");
    expect(net.textContent).toContain("50.0 MiB/s");
    expect(within(net).getByText("stale")).toBeTruthy();
    expect(within(net).getAllByText("unknown").length).toBeGreaterThan(0); // never measured
    expect(screen.getByText(/Not measured yet - no transfer from the controller/)).toBeTruthy();

    await user.click(screen.getByRole("tab", { name: "Caches & policy" }));
    expect((await screen.findByTestId("caches-card")).textContent).toContain("Python environments");
    expect(screen.getByTestId("caches-card").textContent).toContain("3.0 GiB");
    const policy = screen.getByTestId("policy-card");
    expect(policy.textContent).toContain("max_ram_bytes");
    expect(policy.textContent).toContain("read-only");
    expect(within(policy).queryByRole("button")).toBeNull(); // nothing to edit here
  });

  it("live: a node.metrics event appends to the sparkline and moves 'latest sample'", async () => {
    fakeController(detailRoutes());
    const app = renderApp("/nodes/node_A");
    await screen.findByTestId("live-cpu");
    act(() => {
      app.socket().greet();
    });
    const observed = new Date().toISOString();
    act(() => {
      app.socket().send(
        event("node.metrics", { node_id: "node_A", observed_at: observed, cpu_percent: 66, ram_used_bytes: 8 * GiB, ram_total_bytes: 16 * GiB, gpus: [] }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("live-cpu").textContent).toBe("66%");
    });
    expect(screen.getByTestId("latest-sample").getAttribute("datetime")).toBe(observed);
    expect(screen.getByRole("img", { name: /^CPU: now 66%.* over 3 samples$/ })).toBeTruthy();
  });

  it("an unknown node is 'No such node', not an error page", async () => {
    fakeController(detailRoutes({ "GET /v1/nodes/node_X": () => json(404, { error: { code: "not_found", message: "node not found" } }) }));
    renderApp("/nodes/node_X");
    expect(await screen.findByText("No such node")).toBeTruthy();
  });

  it("revoke: armed only by the typed name; CSRF header sent; result toast", async () => {
    const ctl = fakeController(
      detailRoutes({
        "POST /v1/nodes/node_A/revoke": () =>
          json(200, { node_id: "node_A", state: "revoked", serial: "ab", already_revoked: false, previous_state: "online", session_killed: true, enforcement: "applied" }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/nodes/node_A");
    await user.click(await screen.findByRole("button", { name: "Revoke node" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Revoke permanently" });
    expect(confirm).toHaveProperty("disabled", true);
    const field = within(dialog).getByRole("textbox");
    expect(document.activeElement).toBe(field);
    await user.type(field, "lab-");
    expect(confirm).toHaveProperty("disabled", true);
    await user.type(field, "a{Enter}");
    await waitFor(() => {
      expect(ctl.count("POST /v1/nodes/node_A/revoke")).toBe(1);
    });
    const post = ctl.calls.find((c) => c.key === "POST /v1/nodes/node_A/revoke");
    expect(new Headers(post?.init.headers).get(CSRF_HEADER)).toBe("csrf-value");
    // Scoped to the toast: Radix also renders a delayed screen-reader copy of its text.
    const toast = (await screen.findByText("lab-a revoked")).closest("li");
    expect(toast).not.toBeNull();
    expect(within(toast as HTMLElement).getByText(/Its live session was cut/)).toBeTruthy();
  });

  it("revoke refused by the server (403) is said in the dialog; the node stays", async () => {
    fakeController(
      detailRoutes({
        "POST /v1/nodes/node_A/revoke": () => json(403, { error: { code: "unauthorized", message: "this call needs an admin account" } }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/nodes/node_A");
    await user.click(await screen.findByRole("button", { name: "Revoke node" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox"), "lab-a{Enter}");
    expect((await within(dialog).findByRole("alert")).textContent).toBe("Not revoked: this call needs an admin account");
    expect(screen.queryByText("lab-a revoked")).toBeNull();
    // Cancel closes it (Escape is the browser's <dialog> - covered by e2e);
    // the error is forgotten for the next attempt.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Revoke node" }));
    expect(within(await screen.findByRole("dialog")).queryByRole("alert")).toBeNull();
  });

  it("a user gets no Revoke button", async () => {
    fakeController(detailRoutes({ "GET /v1/session": () => json(200, { session: USER_SESSION }) }));
    renderApp("/nodes/node_A");
    await screen.findByRole("heading", { level: 1, name: "lab-a" });
    expect(screen.queryByRole("button", { name: "Revoke node" })).toBeNull();
  });

  it("a revoke with no CSRF cookie is not sent and says to sign in again", async () => {
    document.cookie = "mf_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    const ctl = fakeController(detailRoutes());
    const user = userEvent.setup();
    renderApp("/nodes/node_A");
    await user.click(await screen.findByRole("button", { name: "Revoke node" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox"), "lab-a{Enter}");
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/Sign in again/);
    expect(ctl.count("POST /v1/nodes/node_A/revoke")).toBe(0);
  });
});
