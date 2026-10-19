/* eslint-disable @typescript-eslint/require-await, @typescript-eslint/no-unnecessary-type-assertion, @typescript-eslint/ban-ts-comment -- tester fixtures: deliberately loose shapes and async mock handlers */
/**
 * V0.9 G6 independent tester round 1 (written from brief 5h + roadmap 0.9.9,
 * not from the implementation): adversarial cases the implementer's own tests
 * do not cover.
 */
import { QueryClient } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/errors";
import { ADMIN_SESSION, USER_SESSION, event, fakeController, json, node, renderApp, type Handler } from "../../test/fakeFabric";
import { applyCampaignEvent } from "./applyCampaignEvent";
import { benchKeys } from "./benchKeys";
import { isCompleteParquet } from "./download";
import { pollInterval } from "./poll";
import { getQuality, parseQuality } from "./qualityApi";
import { familyCharts, plottable, strategyChart } from "./chartData";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const A = "camp_01J00000000000000000000AAA";
const COUNTS = { planned: 5, submitted: 1, done: 3, failed: 1, skipped: 0 };

function campaign(id: string, state: string, counts = COUNTS) {
  return {
    campaign_id: id, name: "night-1", state, seed: 1, spec_digest: "sha256:a", run_count: 10, counts,
    summary: { groups: 1, configs: 1, dropped: 0, duplicates: 0, estimated_hours: 1, nodes: ["node_a"], unsupported: 0, session_label: null },
    created_by: "usr_admin", created_at: "2026-10-19T09:00:00Z",
  };
}
function status(id: string, over: Record<string, unknown> = {}) {
  return {
    campaign_id: id, state: "active", runner_owned: true, started_at: "2026-10-19T09:30:00Z", finished_reason: null,
    max_concurrent_per_node: 2, cooldown_s: 5, max_infra_retries: 3, counts: COUNTS, cancelled_jobs: 0, done_percent: 40,
    median_run_s: 12.5, eta_s: 750, infra_failure_rate: 0.01, records: 5, infra_failures: 1, preempted: 2, retries: 1,
    runner_degraded: false, ...over,
  };
}
function admin(extra: Record<string, Handler> = {}, state = "active") {
  return fakeController({
    "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
    "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("node_a")] }),
    "GET /v1/bench/campaigns": () => json(200, { campaigns: [campaign(A, state)], count: 1 }),
    [`GET /v1/bench/campaigns/${A}/status`]: () => json(200, status(A, { state })),
    ...extra,
  });
}

/* --------------------------------------------------------- admin gate */
describe("non-admin", () => {
  it("typing a campaign detail URL shows the admins-only state and sends no bench request", async () => {
    const fake = fakeController({
      "GET /v1/session": () => json(200, { session: USER_SESSION }),
      "GET /v1/nodes": () => json(200, { count: 0, nodes: [] }),
    });
    renderApp(`/benchmarks/${A}?tab=download`);
    expect(await screen.findByText("Benchmarks are run by admins")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Download/ })).toBeNull();
    expect(fake.calls.some((c) => c.url.includes("/v1/bench") || c.url.includes("/v1/telemetry"))).toBe(false);
  });
});

/* ----------------------------------------------------- double click */
describe("controls under repeated clicks", () => {
  it("double-clicking Pause sends exactly one POST", async () => {
    let release: (r: Response) => void = () => undefined;
    const fake = admin({
      [`POST /v1/bench/campaigns/${A}/pause`]: () => new Promise<Response>((r) => { release = r; }),
    });
    renderApp("/benchmarks");
    const pause = await screen.findByRole("button", { name: "Pause campaign night-1" });
    await userEvent.dblClick(pause);
    expect(fake.count(`POST /v1/bench/campaigns/${A}/pause`)).toBe(1);
    // the sibling control is disabled while the mutation is pending
    expect((screen.getByRole("button", { name: "Cancel campaign night-1" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { release(json(200, { ...status(A, { state: "paused" }), cancelled_jobs: 0 })); });
    expect(await screen.findByText("Campaign paused: running jobs finish, nothing new is submitted")).toBeTruthy();
  });

  it("double-clicking the confirm of Cancel sends one cancel; Escape on the dialog sends none", async () => {
    let release: (r: Response) => void = () => undefined;
    const fake = admin({
      [`POST /v1/bench/campaigns/${A}/cancel`]: () => new Promise<Response>((r) => { release = r; }),
    });
    renderApp("/benchmarks");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel campaign night-1" }));
    await userEvent.keyboard("{Escape}");
    expect(fake.count(`POST /v1/bench/campaigns/${A}/cancel`)).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Cancel campaign night-1" }));
    await userEvent.dblClick(await screen.findByRole("button", { name: "Cancel campaign" }));
    expect(fake.count(`POST /v1/bench/campaigns/${A}/cancel`)).toBe(1);
    await act(async () => { release(json(200, { ...status(A, { state: "cancelled" }), cancelled_jobs: 1 })); });
    expect(await screen.findByText("Campaign cancelled")).toBeTruthy();
  });

  it("409 campaign_running refusal toasts message and leaves the controls usable again", async () => {
    const fake = admin({
      [`POST /v1/bench/campaigns/${A}/pause`]: () =>
        json(409, { error: { code: "conflict", message: "campaign already running", reason: "campaign_running" } }),
    });
    renderApp("/benchmarks");
    const pause = await screen.findByRole("button", { name: "Pause campaign night-1" });
    await userEvent.click(pause);
    expect(await screen.findByText(/campaign already running/)).toBeTruthy();
    await waitFor(() => { expect((screen.getByRole("button", { name: "Pause campaign night-1" }) as HTMLButtonElement).disabled).toBe(false); });
    await userEvent.click(screen.getByRole("button", { name: "Pause campaign night-1" }));
    await waitFor(() => { expect(fake.count(`POST /v1/bench/campaigns/${A}/pause`)).toBe(2); });
  });

  it("controller down (network error) on a control toasts, no crash", async () => {
    admin({ [`POST /v1/bench/campaigns/${A}/pause`]: () => { throw new TypeError("Failed to fetch"); } });
    renderApp("/benchmarks");
    await userEvent.click(await screen.findByRole("button", { name: "Pause campaign night-1" }));
    expect(await screen.findByText("Pause refused")).toBeTruthy();
    expect(screen.getByRole("link", { name: "night-1" })).toBeTruthy();
  });
});

/* --------------------------------------------------------- keyboard */
describe("keyboard", () => {
  it("the list's controls are reachable by Tab and activatable with Enter", async () => {
    const fake = admin({
      [`POST /v1/bench/campaigns/${A}/pause`]: () => json(200, { ...status(A, { state: "paused" }), cancelled_jobs: 0 }),
    });
    renderApp("/benchmarks");
    const pause = await screen.findByRole("button", { name: "Pause campaign night-1" });
    pause.focus();
    expect(document.activeElement).toBe(pause);
    await userEvent.keyboard("{Enter}");
    await waitFor(() => { expect(fake.count(`POST /v1/bench/campaigns/${A}/pause`)).toBe(1); });
    const link = screen.getByRole("link", { name: "night-1" });
    expect(link.getAttribute("tabindex")).not.toBe("-1");
  });

  it("the detail tabs are arrow-key navigable and no element carries an inline style attribute outside charts", async () => {
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
    });
    const { container } = renderApp(`/benchmarks/${A}`);
    const tab = await screen.findByRole("tab", { name: "Overview" });
    tab.focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement?.getAttribute("role")).toBe("tab");
    expect(container.querySelectorAll("style").length).toBe(0);
  });
});

/* ------------------------------------------------------ event stream */
describe("live updates", () => {
  it("stream down: a paused-at-first campaign becomes done by polling alone and polling then stops", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let state = "active";
    const fake = admin({
      "GET /v1/bench/campaigns": () => json(200, { campaigns: [campaign(A, state, { planned: 0, submitted: 0, done: 10, failed: 0, skipped: 0 })], count: 1 }),
      [`GET /v1/bench/campaigns/${A}/status`]: () => json(200, status(A, { state })),
    });
    renderApp("/benchmarks"); // socket never greets
    await screen.findByRole("link", { name: "night-1" });
    state = "done";
    await act(async () => { await vi.advanceTimersByTimeAsync(5_500); });
    const row = (await screen.findByRole("link", { name: "night-1" })).closest("tr") as HTMLElement;
    await waitFor(() => { expect(within(row).getByText("done")).toBeTruthy(); });
    const settled = fake.count("GET /v1/bench/campaigns");
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(fake.count("GET /v1/bench/campaigns")).toBe(settled); // terminal: no more polling
  });

  it("stream live and runner-owned: no polling at all", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fake = admin();
    const { socket } = renderApp("/benchmarks");
    await screen.findByRole("link", { name: "night-1" });
    act(() => { socket().greet(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    const n = fake.count("GET /v1/bench/campaigns");
    await act(async () => { await vi.advanceTimersByTimeAsync(40_000); });
    expect(fake.count("GET /v1/bench/campaigns")).toBe(n);
  });

  it("garbage / unknown campaign events never crash the page", async () => {
    admin();
    const { socket } = renderApp("/benchmarks");
    const row = (await screen.findByRole("link", { name: "night-1" })).closest("tr") as HTMLElement;
    act(() => { socket().greet(); });
    act(() => {
      socket().send(event("campaign.progress", {}));
      socket().send(event("campaign.progress", { campaign_id: 7 }));
      socket().send(event("campaign.progress", { campaign_id: A, state: "exploded", counts: { done: -1 }, done_percent: "NaN", eta_s: "x" }));
      socket().send(event("campaign.nonsense", { campaign_id: A, state: "done" }));
      socket().send(event("campaign.progress", { campaign_id: "camp_unknown", state: "active" }));
    });
    expect(within(row).getByText("active")).toBeTruthy();
    expect(within(row).getByText("4 / 10")).toBeTruthy();
  });
});

describe("applyCampaignEvent (pure cache)", () => {
  function seeded() {
    const client = new QueryClient();
    client.setQueryData(benchKeys.list, { campaigns: [campaign(A, "active")], count: 1 });
    client.setQueryData(benchKeys.status(A), status(A));
    return client;
  }
  const ev = (data: Record<string, unknown>) => event("campaign.progress", { campaign_id: A, ...data });

  // DEFECT T-1 (minor): no ordering guard; it.fails flips red once fixed.
  it.fails("a late/out-of-order 'active' event after 'done' must not resurrect a finished campaign (T-1)", () => {
    const client = seeded();
    applyCampaignEvent(client, ev({ state: "done", finished_reason: "complete", counts: { planned: 0, submitted: 0, done: 9, failed: 1, skipped: 0 } }));
    applyCampaignEvent(client, ev({ state: "active", counts: COUNTS }));
    const list = client.getQueryData<{ campaigns: { state: string }[] }>(benchKeys.list);
    expect(list?.campaigns[0]?.state).toBe("done");
  });

  // DEFECT T-1b (minor), same cause.
  it.fails("counts never move backwards on an older event (T-1b)", () => {
    const client = seeded();
    applyCampaignEvent(client, ev({ state: "active", counts: { planned: 1, submitted: 1, done: 8, failed: 0, skipped: 0 } }));
    applyCampaignEvent(client, ev({ state: "active", counts: { planned: 4, submitted: 1, done: 4, failed: 1, skipped: 0 } }));
    const list = client.getQueryData<{ campaigns: { counts: { done: number } }[] }>(benchKeys.list);
    expect(list?.campaigns[0]?.counts.done).toBe(8);
  });

  it("an event for another type returns false and does not touch the cache", () => {
    const client = seeded();
    expect(applyCampaignEvent(client, event("job.updated", { campaign_id: A, state: "done" }))).toBe(false);
  });

  it("an empty / absent cache is tolerated", () => {
    expect(applyCampaignEvent(new QueryClient(), ev({ state: "paused" }))).toBe(true);
  });
});

describe("poll policy", () => {
  it("is off when nothing is open, 5 s when the stream is down", () => {
    expect(pollInterval(false, { open: false, unowned: false })).toBe(false);
    expect(pollInterval(true, { open: false, unowned: true })).toBe(false);
    expect(pollInterval(false, { open: true, unowned: false })).toBe(5000);
  });
});

/* ------------------------------------------------------------ quality */
describe("quality robustness", () => {
  it.each([null, "<html>", 42, [], {}, { quality_version: 2 }, { quality_version: "1" }])("parseQuality(%j) never throws and is not a report", (body) => {
    expect(() => parseQuality(body)).not.toThrow();
    expect(parseQuality(body).kind).toBe("unsupported");
  });

  it("a v1 body with junk fields parses without throwing and invents no numbers", () => {
    const r = parseQuality({ quality_version: 1, summary: { infra_failure_rate: "x", runs_by_state: { done: "NaN" } }, configs: [null, 3, { group_id: 1 }, { group_id: "g", config_id: "c", total_ms: "bad", n: Infinity }] });
    expect(r.kind).toBe("report");
    if (r.kind === "report") {
      expect(r.report.summary.infra_failure_rate).toBeNull();
      expect(r.report.configs).toHaveLength(1);
      expect(r.report.configs[0]?.n).toBeNull();
    }
  });

  it("404 'Not Found' (route missing) is unavailable; 404 'no such campaign' is an error; 500 is an error", async () => {
    const mk = (status: number, message: string) => json(status, { error: { code: status === 404 ? "not_found" : "internal", message } });
    fakeController({ [`GET /v1/bench/campaigns/${A}/quality`]: () => mk(404, "Not Found") });
    expect((await getQuality(A)).kind).toBe("unavailable");
    fakeController({ [`GET /v1/bench/campaigns/${A}/quality`]: () => mk(404, "no such campaign") });
    await expect(getQuality(A)).rejects.toBeInstanceOf(ApiError);
    fakeController({ [`GET /v1/bench/campaigns/${A}/quality`]: () => mk(500, "boom") });
    await expect(getQuality(A)).rejects.toBeInstanceOf(ApiError);
  });

  it("a 404 with a non-JSON (plain text/HTML) body is still 'unavailable', not a crash", async () => {
    fakeController({ [`GET /v1/bench/campaigns/${A}/quality`]: () => new Response("<h1>404</h1>", { status: 404 }) });
    expect((await getQuality(A)).kind).toBe("unavailable");
  });

  it("quality tab with a 200 HTML body shows an explicit state, not a blank/crash", async () => {
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      [`GET /v1/bench/campaigns/${A}/quality`]: () => new Response("<html>proxy</html>", { status: 200, headers: { "content-type": "text/html" } }),
    });
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect(await screen.findByRole("tab", { name: "Quality" })).toBeTruthy(); // page still alive
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByTestId("quality-crit1")).toBeNull();
    expect(screen.queryByText(/NaN|undefined/)).toBeNull();
  });

  it("quality 'no such campaign' 404 is shown as an error, not 'not available on this controller yet'", async () => {
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      [`GET /v1/bench/campaigns/${A}/quality`]: () => json(404, { error: { code: "not_found", message: "no such campaign" } }),
    });
    renderApp(`/benchmarks/${A}?tab=quality`);
    await waitFor(() => { expect(screen.getAllByText(/no such campaign/i).length).toBeGreaterThan(0); });
    expect(screen.queryByText("Quality report not available on this controller yet")).toBeNull();
  });
});

/* ---------------------------------------------------------- downloads */
describe("isCompleteParquet", () => {
  const b = (s: string) => new Blob([s]);
  it.each([
    ["", false], ["PAR1", false], ["PAR1PAR1", false], ["PAR1xxxx", false], ["xxxxPAR1", false],
    ["PAR1 body", false], ["PAR1 cut PAR", false], ["PAR1 rows PAR1", true], ["PAR1\u0000\u0000\u0000\u0000PAR1", true],
  ])("%j -> %s", async (s, ok) => {
    expect(await isCompleteParquet(b(s))).toBe(ok);
  });
  it("a 20 MB blob is checked without reading it whole or overflowing the stack", async () => {
    const big = new Blob(["PAR1", new Uint8Array(20 * 1024 * 1024), "PAR1"]);
    expect(await isCompleteParquet(big)).toBe(true);
  });
});

describe("download failures", () => {
  it("empty 200 body and a mid-stream network error both save nothing and toast", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    let mode: "empty" | "throw" = "empty";
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      "GET /v1/telemetry/export": () => {
        if (mode === "throw") throw new TypeError("network error");
        return new Response("", { status: 200 });
      },
    });
    renderApp(`/benchmarks/${A}?tab=download`);
    const btn = await screen.findByRole("button", { name: "Download records (Parquet)" });
    await userEvent.click(btn);
    expect(await screen.findByText("Could not download the records")).toBeTruthy();
    mode = "throw";
    await userEvent.click(screen.getByRole("button", { name: "Download records (Parquet)" }));
    await waitFor(() => { expect(screen.getAllByText("Could not download the records").length).toBeGreaterThan(0); });
    expect(click).not.toHaveBeenCalled();
    click.mockRestore();
  });

  it("double-clicking Download records sends one request", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => undefined }));
    let release: (r: Response) => void = () => undefined;
    const fake = admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      "GET /v1/telemetry/export": () => new Promise<Response>((r) => { release = r; }),
    });
    renderApp(`/benchmarks/${A}?tab=download`);
    await userEvent.dblClick(await screen.findByRole("button", { name: "Download records (Parquet)" }));
    expect(fake.count("GET /v1/telemetry/export")).toBe(1);
    await act(async () => { release(new Response("PAR1 rows PAR1", { status: 200 })); });
    click.mockRestore();
  });
});

/* ------------------------------------------------------------- charts */
const grp = (id: string, label: string, params: Record<string, unknown>, configs: unknown[]) => ({
  group_id: id, family: "cpu.primes", condition: "none", params, label, best_config_id: null, best_strategy: null, best_node_id: null,
  margin_pct: null, tied_config_ids: [], planner_node_id: null, planner_agrees: null, reason: "x", configs,
});
const emptyCfg = (id: string) => ({ config_id: id, strategy: "local", node_id: "node_a", origin_node_id: "node_a", state: "insufficient", repetitions: 1, planner_node_id: null, total_ms: null, compute_per_iter_ns: null, bytes: null });
const labels = (groups: unknown[]) => ({ campaign_id: A, labels_version: 1, campaign_state: "active", rows: 0, counts: { best: 0, tie: 0, insufficient: groups.length, pending: 0 }, groups });

describe("charts with unusable labels", () => {
  it("chartData: groups with no measured configs produce no points and are not plottable", () => {
    const l = labels([grp("g1", "insufficient", { limit: 1 }, [emptyCfg("c1")]), grp("g2", "pending", { limit: 2 }, [emptyCfg("c2")])]);
    // @ts-expect-error fixture is structurally compatible but loosely typed
    const charts = familyCharts(l, "cpu.primes");
    for (const c of charts) expect(plottable(c)).toBe(false);
    // @ts-expect-error fixture
    expect(strategyChart(l.groups[0]).bars).toEqual([]);
  });

  it("chartData: a group with zero configs, non-numeric / negative / zero x values does not throw", () => {
    const cfg = { ...emptyCfg("c"), state: "measured", total_ms: { n: 3, median: 5, mean: 5, std: 1, cv: 0.1, ci_low: 4, ci_high: 6, ci_coverage: 0.75, outliers: 0 } };
    const l = labels([grp("g1", "best", { limit: "big" }, [cfg]), grp("g2", "best", { limit: 0 }, []), grp("g3", "best", { limit: -5 }, [{ ...cfg, config_id: "c3" }]), grp("g4", "best", { limit: null }, [{ ...cfg, config_id: "c4" }])]);
    // @ts-expect-error fixture
    expect(() => familyCharts(l, "cpu.primes").map(plottable)).not.toThrow();
  });

  it("Charts tab with only insufficient/pending labels shows an explicit empty state and no svg", async () => {
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      [`GET /v1/bench/campaigns/${A}/labels`]: () => json(200, labels([grp("g1", "insufficient", { limit: 1 }, [emptyCfg("c1")]), grp("g2", "pending", { limit: 2 }, [emptyCfg("c2")])])),
    });
    const { container } = renderApp(`/benchmarks/${A}?tab=charts`);
    await waitFor(() => { expect(screen.queryByText("Loading…")).toBeNull(); });
    await screen.findByRole("tab", { name: "Charts" });
    await waitFor(() => { expect(container.querySelector("[data-testid=family-chart] svg")).toBeNull(); });
    expect(screen.queryByText(/NaN|Infinity|undefined/)).toBeNull();
  });

  it("Charts tab: labels request failing (500) shows an error, not a blank tab", async () => {
    admin({
      [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "active"), spec: "name: x\n" }),
      [`GET /v1/bench/campaigns/${A}/labels`]: () => json(500, { error: { code: "internal", message: "labels exploded" } }),
    });
    renderApp(`/benchmarks/${A}?tab=charts`);
    expect(await screen.findByText(/labels exploded/)).toBeTruthy();
  });
});
