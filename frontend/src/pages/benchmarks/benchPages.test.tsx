/**
 * V0.9 G6: the Benchmarks pages against a fake controller (stubbed fetch, fake
 * event socket). Every number on screen is what the API returned (checklist
 * 26); controls send the right call and toast the server's answer; the
 * Quality tab survives a controller without the route; downloads save only a
 * complete Parquet file; a user never sees the section.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ADMIN_SESSION, USER_SESSION, event, fakeController, json, node, renderApp, type Handler } from "../../test/fakeFabric";
import { qualityConfig, qualityMetric, qualityReport, qualitySummary } from "../../test/qualityFixture";

beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const A = "camp_01J00000000000000000000AAA";
const B = "camp_01J00000000000000000000BBB";

const COUNTS_A = { planned: 5, submitted: 1, done: 3, failed: 1, skipped: 0 };

function campaign(id: string, name: string, state: string, counts: Record<string, number>, runCount: number) {
  return {
    campaign_id: id,
    name,
    state,
    seed: 51,
    spec_digest: "sha256:abc",
    run_count: runCount,
    counts,
    summary: { groups: 2, configs: 4, dropped: 0, duplicates: 0, estimated_hours: 1.25, nodes: ["node_a", "node_b"], unsupported: 0, session_label: "wifi" },
    created_by: "usr_admin",
    created_at: "2026-10-19T09:00:00Z",
  };
}

const LIST = {
  campaigns: [
    campaign(A, "night-1", "active", COUNTS_A, 10),
    campaign(B, "old", "done", { planned: 0, submitted: 0, done: 6, failed: 0, skipped: 0 }, 6),
  ],
  count: 2,
};

function status(id: string, over: Record<string, unknown> = {}) {
  return {
    campaign_id: id,
    state: "active",
    runner_owned: true,
    started_at: "2026-10-19T09:30:00Z",
    finished_reason: null,
    max_concurrent_per_node: 2,
    cooldown_s: 5,
    max_infra_retries: 3,
    counts: COUNTS_A,
    cancelled_jobs: 0,
    done_percent: 40,
    median_run_s: 12.5,
    eta_s: 750,
    infra_failure_rate: 0.0123,
    records: 5,
    infra_failures: 1,
    preempted: 2,
    retries: 1,
    runner_degraded: false,
    ...over,
  };
}

const STATUS_B = status(B, { state: "done", finished_reason: "complete", done_percent: 100, eta_s: null, infra_failure_rate: 0, counts: LIST.campaigns[1]?.counts });

function admin(extra: Record<string, Handler> = {}) {
  return fakeController({
    "GET /v1/session": () => json(200, { session: ADMIN_SESSION }),
    "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("node_a")] }),
    "GET /v1/bench/campaigns": () => json(200, LIST),
    [`GET /v1/bench/campaigns/${A}/status`]: () => json(200, status(A)),
    [`GET /v1/bench/campaigns/${B}/status`]: () => json(200, STATUS_B),
    ...extra,
  });
}

/* ------------------------------------------------------------ list */

describe("campaigns list", () => {
  it("shows the API's numbers per campaign", async () => {
    admin();
    renderApp("/benchmarks");
    expect(await screen.findByRole("heading", { level: 1, name: "Benchmarks" })).toBeTruthy();
    const rowA = (await screen.findByRole("link", { name: "night-1" })).closest("tr");
    if (rowA === null) throw new Error("no row");
    const row = within(rowA);
    expect(await row.findByText("40.0 %")).toBeTruthy(); // done_percent
    expect(row.getByText("active")).toBeTruthy();
    expect(row.getByText("4 / 10")).toBeTruthy(); // done + failed + skipped of run_count
    expect(row.getByText("13 min")).toBeTruthy(); // eta_s 750
    expect(row.getByText("1.2 %")).toBeTruthy(); // infra_failure_rate 0.0123
    expect(row.getByRole("button", { name: "Pause campaign night-1" })).toBeTruthy(); // runner-owned
    expect(row.getByRole("button", { name: "Cancel campaign night-1" })).toBeTruthy();
    expect(screen.getByText("(owner pre-emptions excluded)")).toBeTruthy();
    const rowB = within(screen.getByRole("link", { name: "old" }).closest("tr") as HTMLElement);
    expect(await rowB.findByText("100.0 %")).toBeTruthy();
    expect(rowB.getByText("complete")).toBeTruthy();
    expect(rowB.queryByRole("button")).toBeNull(); // done: no controls
    expect(screen.getByRole("link", { name: "night-1" }).getAttribute("href")).toBe(`/benchmarks/${A}`);
  });

  it("an empty fabric says how to create a campaign", async () => {
    admin({ "GET /v1/bench/campaigns": () => json(200, { campaigns: [], count: 0 }) });
    renderApp("/benchmarks");
    expect(await screen.findByText("No benchmark campaigns yet")).toBeTruthy();
  });

  it("a load failure shows the server's error with a retry", async () => {
    admin({ "GET /v1/bench/campaigns": () => json(503, { error: { code: "network_error", message: "the database is unavailable; retry" } }) });
    renderApp("/benchmarks");
    expect(await screen.findByText("Could not load campaigns")).toBeTruthy();
    expect(screen.getByText("the database is unavailable; retry")).toBeTruthy();
  });

  it("admins see the nav entry; a user sees neither the entry nor the page, and no bench call is made", async () => {
    admin();
    const first = renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(await within(nav).findByRole("link", { name: "Benchmarks" })).toBeTruthy();
    first.unmount();

    const fake = fakeController({
      "GET /v1/session": () => json(200, { session: USER_SESSION }),
      "GET /v1/nodes": () => json(200, { count: 0, nodes: [] }),
    });
    renderApp("/benchmarks");
    expect(await screen.findByText("Benchmarks are run by admins")).toBeTruthy();
    expect(within(screen.getByRole("navigation", { name: "Main" })).queryByRole("link", { name: "Benchmarks" })).toBeNull();
    expect(fake.calls.some((c) => c.key.includes("/v1/bench"))).toBe(false);
  });
});

/* ------------------------------------------------------------ controls */

describe("campaign controls", () => {
  it("start sends POST .../start with the CSRF header and toasts the result", async () => {
    const fake = admin({
      [`GET /v1/bench/campaigns/${A}/status`]: () => json(200, status(A, { runner_owned: false, started_at: null })),
      [`POST /v1/bench/campaigns/${A}/start`]: () => json(200, { ...status(A), cancelled_jobs: 0 }),
    });
    renderApp("/benchmarks");
    const start = await screen.findByRole("button", { name: "Start campaign night-1" });
    await userEvent.click(start);
    expect(await screen.findByText("Campaign started: the controller's runner submits its runs")).toBeTruthy();
    const call = fake.calls.find((c) => c.key === `POST /v1/bench/campaigns/${A}/start`);
    expect(new Headers(call?.init.headers).get("X-MeshForge-CSRF")).toBe("test-csrf");
  });

  it("a refusal (409) toasts the server's message and code; nothing changes", async () => {
    admin({
      [`POST /v1/bench/campaigns/${A}/pause`]: () =>
        json(409, { error: { code: "conflict", message: "the campaign is done; it cannot pause", reason: "campaign_running" } }),
    });
    renderApp("/benchmarks");
    await userEvent.click(await screen.findByRole("button", { name: "Pause campaign night-1" }));
    expect(await screen.findByText("Pause refused")).toBeTruthy();
    expect(screen.getByText("the campaign is done; it cannot pause (conflict (campaign_running))")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pause campaign night-1" })).toBeTruthy();
  });

  it("cancel asks first; Keep running sends nothing; Cancel campaign sends the cancel", async () => {
    const fake = admin({
      [`POST /v1/bench/campaigns/${A}/cancel`]: () => json(200, { ...status(A, { state: "cancelled" }), cancelled_jobs: 2 }),
    });
    renderApp("/benchmarks");
    await userEvent.click(await screen.findByRole("button", { name: "Cancel campaign night-1" }));
    expect(screen.getByText("Cancel this campaign?")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    expect(fake.count(`POST /v1/bench/campaigns/${A}/cancel`)).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Cancel campaign night-1" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel campaign" }));
    expect(await screen.findByText("Campaign cancelled")).toBeTruthy();
    expect(screen.getByText(`${A} is cancelled. 2 benchmark job(s) told to stop.`)).toBeTruthy();
    expect(fake.count(`POST /v1/bench/campaigns/${A}/cancel`)).toBe(1);
  });

  it("paused campaigns offer Resume", async () => {
    admin({
      "GET /v1/bench/campaigns": () => json(200, { campaigns: [campaign(A, "night-1", "paused", COUNTS_A, 10)], count: 1 }),
      [`GET /v1/bench/campaigns/${A}/status`]: () => json(200, status(A, { state: "paused" })),
    });
    renderApp("/benchmarks");
    expect(await screen.findByRole("button", { name: "Resume campaign night-1" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause campaign night-1" })).toBeNull();
  });
});

/* ------------------------------------------------------------ live + poll */

describe("live updates", () => {
  it("campaign.progress moves the row without a refetch", async () => {
    const fake = admin();
    const { socket } = renderApp("/benchmarks");
    const rowA = (await screen.findByRole("link", { name: "night-1" })).closest("tr") as HTMLElement;
    expect(await within(rowA).findByText("40.0 %")).toBeTruthy();
    act(() => {
      socket().greet();
    });
    const lists = fake.count("GET /v1/bench/campaigns");
    act(() => {
      socket().send(
        event("campaign.progress", {
          campaign_id: A,
          state: "active",
          finished_reason: null,
          counts: { planned: 2, submitted: 1, done: 6, failed: 1, skipped: 0 },
          done_percent: 70,
          eta_s: 30,
          infra_failure_rate: 0.02,
        }),
      );
    });
    expect(await within(rowA).findByText("70.0 %")).toBeTruthy();
    expect(within(rowA).getByText("7 / 10")).toBeTruthy();
    expect(within(rowA).getByText("30 s")).toBeTruthy();
    expect(within(rowA).getByText("2.0 %")).toBeTruthy();
    expect(fake.count("GET /v1/bench/campaigns")).toBe(lists);
  });

  it("with the event stream down the list is polled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fake = admin();
    renderApp("/benchmarks"); // the socket never says hello: not live
    await screen.findByRole("link", { name: "night-1" });
    const before = fake.count("GET /v1/bench/campaigns");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_500);
    });
    await waitFor(() => {
      expect(fake.count("GET /v1/bench/campaigns")).toBeGreaterThan(before);
    });
    expect(fake.count(`GET /v1/bench/campaigns/${A}/status`)).toBeGreaterThan(1);
  });
});

/* ------------------------------------------------------------ detail */

const SPEC = "name: night-1\nseed: 51\nfamilies:\n  - family: cpu.primes\nrepetitions: 3\nbudget: {max_runs: 40, max_hours: 2}\n";

const RUNS = {
  campaign_id: A,
  runs: [
    {
      run_id: "run_1",
      seq: 1,
      group_id: "grp_01J0000000000000000000GGG1",
      config_id: "cfg_01J0000000000000000000CCC1",
      repetition: 0,
      family: "cpu.primes",
      params: { limit: 10_000_000 },
      seed: 1,
      conditions: ["none"],
      strategy: "local",
      node_id: "node_a",
      origin_node_id: "node_a",
      state: "done",
      job_id: "job_01J00000000000000000000JJ1",
      detail: null,
      submitted_at: "2026-10-19T09:31:00Z",
      ended_at: "2026-10-19T09:31:20Z",
      tries: 2,
      last_failure: "network_error: the stream to the node was lost",
    },
  ],
  count: 1,
  next_after: null,
};

function mstats(median: number, low: number, high: number) {
  return { n: 3, median, mean: median, std: 10, cv: 0.01, ci_low: low, ci_high: high, ci_coverage: 0.75, outliers: 0 };
}

function cfg(id: string, nodeId: string, total: ReturnType<typeof mstats>) {
  return { config_id: id, strategy: "local", node_id: nodeId, origin_node_id: nodeId, state: "measured", repetitions: 3, planner_node_id: "node_b", total_ms: total, compute_per_iter_ns: null, bytes: null };
}

const LABELS = {
  campaign_id: A,
  labels_version: 1,
  campaign_state: "active",
  rows: 12,
  counts: { best: 1, tie: 1, insufficient: 0, pending: 0 },
  groups: [
    {
      group_id: "grp_01J0000000000000000000GGG1",
      family: "cpu.primes",
      condition: "none",
      params: { limit: 10_000_000 },
      label: "best",
      best_config_id: "cfg_b1",
      best_strategy: "local",
      best_node_id: "node_b",
      margin_pct: 28.571,
      tied_config_ids: [],
      planner_node_id: "node_b",
      planner_agrees: true,
      reason: null,
      configs: [cfg("cfg_a1", "node_a", mstats(900, 880, 950)), cfg("cfg_b1", "node_b", mstats(700, 690, 720))],
    },
    {
      group_id: "grp_01J0000000000000000000GGG2",
      family: "cpu.primes",
      condition: "none",
      params: { limit: 100_000_000 },
      label: "tie",
      best_config_id: null,
      best_strategy: null,
      best_node_id: null,
      margin_pct: null,
      tied_config_ids: ["cfg_a2", "cfg_b2"],
      planner_node_id: "node_a",
      planner_agrees: null,
      reason: "CIs overlap",
      configs: [cfg("cfg_a2", "node_a", mstats(9000, 8000, 9900)), cfg("cfg_b2", "node_b", mstats(8800, 8500, 9100))],
    },
  ],
};

function detail(extra: Record<string, Handler> = {}) {
  return admin({
    [`GET /v1/bench/campaigns/${A}`]: () => json(200, { ...campaign(A, "night-1", "active", COUNTS_A, 10), spec: SPEC }),
    [`GET /v1/bench/campaigns/${A}/runs`]: () => json(200, RUNS),
    [`GET /v1/bench/campaigns/${A}/labels`]: () => json(200, LABELS),
    ...extra,
  });
}

async function openTab(name: string) {
  await userEvent.click(await screen.findByRole("tab", { name }));
}

describe("campaign detail", () => {
  it("overview: counts, progress, failure numbers, budget and throttle from the API", async () => {
    detail();
    renderApp(`/benchmarks/${A}`);
    expect(await screen.findByRole("heading", { level: 1, name: "night-1" })).toBeTruthy();
    expect((await screen.findByTestId("overview-done")).textContent).toContain("40.0 %");
    expect(screen.getByTestId("count-done").textContent).toBe("3");
    expect(screen.getByTestId("count-failed").textContent).toBe("1");
    expect(screen.getByTestId("overview-eta").textContent).toBe("13 min");
    expect(screen.getByTestId("overview-infra-rate").textContent).toBe("1.2 %");
    expect(screen.getByTestId("overview-budget").textContent).toBe("max 40 runs, max 2 h");
    expect(screen.getByTestId("overview-finished").textContent).toBe("not finished");
    expect(screen.getByText("5 s")).toBeTruthy(); // cooldown_s
    expect(screen.getByRole("button", { name: "Pause campaign night-1" })).toBeTruthy();
  });

  it("an unknown campaign says so", async () => {
    detail({ [`GET /v1/bench/campaigns/${A}`]: () => json(404, { error: { code: "not_found", message: "campaign not found" } }) });
    renderApp(`/benchmarks/${A}`);
    expect(await screen.findByText("No such campaign")).toBeTruthy();
  });

  it("runs: group, config, node, strategy, repetition, state, tries, last failure, job link", async () => {
    detail();
    renderApp(`/benchmarks/${A}?tab=runs`);
    const table = await screen.findByRole("table", { name: "Campaign runs in execution order" });
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    const cells = within(row).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toEqual([
      "1",
      "grp_…00GGG1 cpu.primes",
      "cfg_…00CCC1",
      "node_a",
      "local",
      "0",
      "done",
      "2",
      "network_error: the stream to the node was lost",
      "job_…000JJ1",
    ]);
    expect(within(row).getByRole("link").getAttribute("href")).toBe("/jobs/job_01J00000000000000000000JJ1");
  });

  it("runs: the state filter is sent to the server", async () => {
    const fake = detail();
    renderApp(`/benchmarks/${A}?tab=runs`);
    await screen.findByRole("table", { name: "Campaign runs in execution order" });
    fireEvent.change(screen.getByLabelText("State"), { target: { value: "failed" } });
    await waitFor(() => {
      expect(fake.calls.some((c) => c.url.includes("/runs?") && c.url.includes("state=failed"))).toBe(true);
    });
  });

  it("labels: family, condition, label, best config, margin, planner agrees", async () => {
    detail();
    renderApp(`/benchmarks/${A}`);
    await openTab("Labels");
    const table = await screen.findByRole("table", { name: "Per-group labels" });
    const rows = within(table).getAllByRole("row").slice(1);
    const first = within(rows[0] as HTMLElement).getAllByRole("cell").map((c) => c.textContent);
    expect(first).toEqual(["cpu.primesgrp_…00GGG1", "limit=10000000", "none", "best", "local on node_b", "28.6 %", "yes"]);
    const second = within(rows[1] as HTMLElement).getAllByRole("cell").map((c) => c.textContent);
    expect(second).toEqual(["cpu.primesgrp_…00GGG2", "limit=100000000", "none", "tieCIs overlap", "tie: 2 configs", "—", "—"]);
    expect(screen.getByTestId("labels-summary").textContent).toBe("2 groups from 12 run records: 1 best, 1 tie, 0 insufficient, 0 pending.");
  });

  it("charts: runtime vs parameter and strategy comparison show the labels' numbers", async () => {
    detail();
    renderApp(`/benchmarks/${A}?tab=charts`);
    const family = await screen.findByTestId("family-chart");
    expect(family.querySelector("figcaption")?.textContent).toBe("cpu.primes: median total time vs limit (condition none)");
    const numbers = within(family).getByTestId("chart-numbers");
    const rows = within(numbers).getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell").map((c) => c.textContent));
    expect(rows).toEqual([
      ["10.0M", "node_a", "900 ms", "880 ms", "950 ms", "3"],
      ["100M", "node_a", "9.0 s", "8.0 s", "9.9 s", "3"],
      ["10.0M", "node_b", "700 ms", "690 ms", "720 ms", "3"],
      ["100M", "node_b", "8.8 s", "8.5 s", "9.1 s", "3"],
    ]);
    expect(family.querySelector("svg")).not.toBeNull();
    const strategy = screen.getByTestId("strategy-chart");
    const bars = within(within(strategy).getByTestId("chart-numbers"))
      .getAllByRole("row")
      .slice(1)
      .map((r) => within(r).getAllByRole("cell").map((c) => c.textContent));
    expect(bars).toEqual([
      ["local on node_a", "900 ms", "880 ms", "950 ms", "3", ""],
      ["local on node_b", "700 ms", "690 ms", "720 ms", "3", "yes"],
    ]);
  });

  it("charts: no data -> an explicit empty state", async () => {
    detail({ [`GET /v1/bench/campaigns/${A}/labels`]: () => json(200, { ...LABELS, groups: [], rows: 0 }) });
    renderApp(`/benchmarks/${A}?tab=charts`);
    expect(await screen.findByText("No data to chart yet")).toBeTruthy();
  });

  it("quality: a controller without the route (404) says so - no crash, no numbers", async () => {
    detail(); // no quality route: the fake answers 404
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect(await screen.findByText("Quality report not available on this controller yet")).toBeTruthy();
    expect(screen.queryByTestId("quality-crit1")).toBeNull();
  });

  it("quality: the report's verdicts and flagged configs first", async () => {
    detail({
      [`GET /v1/bench/campaigns/${A}/quality`]: () =>
        json(
          200,
          qualityReport(A, {
            summary: qualitySummary({ pending: true, campaign_state: "active", crit1_infra_ok: true, crit3_reps_ok: false, infra_failure_rate: 0.0123 }),
            configs: [
              qualityConfig("cfg_ok"),
              qualityConfig("cfg_bad", {
                n: 2,
                total_ms: qualityMetric(700, { n: 2, cv: 0.3 }),
                cv_flag: true,
                reps_flag: true,
                flagged: true,
                missing: { ...qualityConfig("x").missing, planner_node_id: 2 },
              }),
              qualityConfig("cfg_unsup", { state: "unsupported", n: 0, done: 0, total_ms: null, compute_per_iter_ns: null, condition: "gpu_busy" }),
            ],
          }),
        ),
    });
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect((await screen.findByTestId("quality-crit1")).textContent).toBe("ok so far");
    expect(screen.getByTestId("quality-crit3").textContent).toBe("not met");
    expect(screen.getByTestId("quality-infra-rate").textContent).toBe("1.2 %");
    expect(screen.getByTestId("quality-campaign-state").textContent).toBe("active");
    expect(screen.getByText("The campaign is still running: these numbers and verdicts are provisional.")).toBeTruthy();
    const table = screen.getByRole("table", { name: "Data quality per configuration, flagged first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0] as HTMLElement).getByText("high CV")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText("missing fields")).toBeTruthy();
    expect(within(rows[0] as HTMLElement).getByText("planner_node_id: 2")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("2.0 %")).toBeTruthy(); // cv 0.02 of cfg_ok
    expect(within(rows[2] as HTMLElement).getByText("unsupported")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).getByText("condition gpu_busy")).toBeTruthy();
  });

  it("quality: every record bucket is shown and they add up to `records`", async () => {
    detail({ [`GET /v1/bench/campaigns/${A}/quality`]: () => json(200, qualityReport(A)) });
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect((await screen.findByTestId("quality-records")).textContent).toBe("14");
    const ids = ["measured", "infra", "preempted", "condition", "workload", "rejected", "other"];
    const counts = ids.map((id) => Number(screen.getByTestId(`quality-bucket-${id}`).textContent));
    expect(counts).toEqual([6, 1, 2, 1, 1, 2, 1]);
    expect(counts.reduce((a, b) => a + b, 0)).toBe(14);
    const buckets = screen.getByRole("table", { name: "Run records by bucket" });
    expect(within(buckets).getByText("check_failed: 1, condition_unverified: 1")).toBeTruthy();
    expect(within(buckets).getByText("unavailable: 1")).toBeTruthy();
    expect(screen.getByTestId("quality-fillers").textContent).toContain("filler: 2, warmup: 1");
    expect(screen.queryByRole("alert")).toBeNull(); // consistent: no warning
  });

  it("quality: buckets that do not add up are called out, never hidden", async () => {
    detail({ [`GET /v1/bench/campaigns/${A}/quality`]: () => json(200, qualityReport(A, { summary: qualitySummary({ records: 15 }) })) });
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect((await screen.findByRole("alert")).textContent).toContain("The buckets add up to 14, not 15");
  });

  it("quality: a v1 body that is not a QualityOut shows no numbers", async () => {
    detail({ [`GET /v1/bench/campaigns/${A}/quality`]: () => json(200, { quality_version: 1, summary: { records: 3 }, configs: [] }) });
    renderApp(`/benchmarks/${A}?tab=quality`);
    expect(await screen.findByText("Quality report could not be read")).toBeTruthy();
    expect(screen.queryByTestId("quality-records")).toBeNull();
  });

  it("download: a complete Parquet file is saved under <campaign>-records.parquet", async () => {
    const created: Blob[] = [];
    const clicked: string[] = [];
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (b: Blob) => { created.push(b); return "blob:x"; }, revokeObjectURL: () => undefined }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    });
    const fake = detail({
      "GET /v1/telemetry/export": () => new Response("PAR1 rows PAR1", { status: 200, headers: { "content-type": "application/vnd.apache.parquet" } }),
    });
    renderApp(`/benchmarks/${A}?tab=download`);
    await userEvent.click(await screen.findByRole("button", { name: "Download records (Parquet)" }));
    expect(await screen.findByText(`Saved ${A}-records.parquet`)).toBeTruthy();
    expect(clicked).toEqual([`${A}-records.parquet`]);
    expect(await created[0]?.text()).toBe("PAR1 rows PAR1");
    expect(fake.calls.find((c) => c.key === "GET /v1/telemetry/export")?.url).toBe(`/v1/telemetry/export?campaign=${A}`);
    click.mockRestore();
  });

  it("download: a truncated body is refused; a 413 shows the server's message", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    detail({
      [`GET /v1/bench/campaigns/${A}/labels`]: (_init, url) =>
        url.includes("format=parquet") ? new Response("PAR1 cut", { status: 200 }) : json(200, LABELS),
      "GET /v1/telemetry/export": () => json(413, { error: { code: "invalid_input", message: "more than 100000 records match" } }),
    });
    renderApp(`/benchmarks/${A}?tab=download`);
    await userEvent.click(await screen.findByRole("button", { name: "Download labels (Parquet)" }));
    expect(await screen.findByText("Could not download the labels")).toBeTruthy();
    expect(screen.getByText(/ended early \(not a complete Parquet file\); nothing was saved/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Download records (Parquet)" }));
    expect(await screen.findByText("more than 100000 records match (invalid_input)")).toBeTruthy();
    expect(click).not.toHaveBeenCalled();
    click.mockRestore();
  });
});
