/**
 * V0.9 G6 pure pieces: chart data mapping (no re-statistics: every number is
 * the labels answer's), campaign controls per state, the spec budget reader,
 * formatting, the quality report parser (G5c §5g contract), the poll rule,
 * the Parquet completeness check and the binary download client.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiDownload } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { ConfigLabel, GroupLabel, LabelsOut, MetricStats } from "./benchApi";
import { campaignVerbs, formatDonePercent, formatEta, formatFraction, formatMargin, labelTone, specBudget } from "./campaignView";
import { familiesOf, familyCharts, formatParams, plottable, strategyChart, sweptParams, xParam } from "./chartData";
import { isCompleteParquet, labelsFileName, recordsFileName } from "./download";
import { MANUAL_POLL_MS, STREAM_DOWN_POLL_MS, pollInterval } from "./poll";
import { flaggedFirst, getQuality, parseQuality } from "./qualityApi";

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------ fixtures */

function stats(median: number, low: number, high: number, n = 3): MetricStats {
  return { n, median, mean: median, std: 1, cv: 0.05, ci_low: low, ci_high: high, ci_coverage: 0.75, outliers: 0 };
}

function config(id: string, node: string, strategy: string, total: MetricStats | null, state: ConfigLabel["state"] = "measured"): ConfigLabel {
  return {
    config_id: id,
    strategy,
    node_id: node,
    origin_node_id: "node_a",
    state,
    repetitions: total?.n ?? 0,
    planner_node_id: null,
    total_ms: total,
    compute_per_iter_ns: null,
    bytes: null,
  };
}

function group(id: string, family: string, params: Record<string, unknown>, configs: ConfigLabel[], over: Partial<GroupLabel> = {}): GroupLabel {
  return {
    group_id: id,
    family,
    condition: "none",
    params,
    label: "best",
    best_config_id: configs[0]?.config_id ?? null,
    best_strategy: configs[0]?.strategy ?? null,
    best_node_id: configs[0]?.node_id ?? null,
    margin_pct: 12.5,
    tied_config_ids: [],
    planner_node_id: null,
    planner_agrees: null,
    reason: null,
    configs,
    ...over,
  };
}

function labels(groups: GroupLabel[]): LabelsOut {
  return {
    campaign_id: "camp_1",
    labels_version: 1,
    campaign_state: "done",
    rows: 12,
    counts: { best: groups.length, tie: 0, insufficient: 0, pending: 0 },
    groups,
  };
}

const SWEEP = labels([
  group("grp_2", "cpu.primes", { limit: 100_000_000, repeats: 4 }, [
    config("cfg_2a", "node_a", "local", stats(9000, 8800, 9300)),
    config("cfg_2b", "node_b", "local", stats(7000, 6900, 7200)),
  ]),
  group("grp_1", "cpu.primes", { limit: 10_000_000, repeats: 4 }, [
    config("cfg_1a", "node_a", "local", stats(900, 880, 950)),
    config("cfg_1b", "node_b", "local", stats(700, 690, 720)),
  ]),
  group("grp_3", "cpu.zlib", { size_mb: 64 }, [
    config("cfg_3a", "node_a", "local", stats(5000, 4900, 5100)),
    config("cfg_3b", "node_b", "mode1", stats(6000, 5800, 6500)),
    config("cfg_3c", "node_b", "mode1", null, "unsupported"),
  ]),
]);

/* ------------------------------------------------------------ charts */

describe("chart data (from labels only)", () => {
  it("families, swept parameters and the x parameter", () => {
    expect(familiesOf(SWEEP)).toEqual(["cpu.primes", "cpu.zlib"]);
    const primes = SWEEP.groups.filter((g) => g.family === "cpu.primes");
    expect(sweptParams(primes)).toEqual(["limit"]); // repeats is the same everywhere
    expect(xParam(primes)).toBe("limit");
    expect(xParam(SWEEP.groups.filter((g) => g.family === "cpu.zlib"))).toBeNull();
    // a non-numeric swept param is never the x axis
    const dtype = [group("g1", "f", { dtype: "float32" }, []), group("g2", "f", { dtype: "float64" }, [])];
    expect(sweptParams(dtype)).toEqual(["dtype"]);
    expect(xParam(dtype)).toBeNull();
  });

  it("runtime vs parameter: one series per node, points sorted by x, the server's median and CI", () => {
    const charts = familyCharts(SWEEP, "cpu.primes");
    expect(charts).toHaveLength(1);
    const chart = charts[0];
    if (chart === undefined) throw new Error("no chart");
    expect(chart.param).toBe("limit");
    expect(chart.logX).toBe(false); // 1e7 .. 1e8 is 10x: under the 20x rule
    expect(chart.series.map((s) => s.label)).toEqual(["node_a", "node_b"]);
    const a = chart.series[0]?.points ?? [];
    expect(a.map((p) => [p.x, p.y, p.ciLow, p.ciHigh, p.configId])).toEqual([
      [10_000_000, 900, 880, 950, "cfg_1a"],
      [100_000_000, 9000, 8800, 9300, "cfg_2a"],
    ]);
    expect(a[0]?.err).toEqual([20, 50]); // median - low, high - median
    expect(plottable(chart)).toBe(true);
  });

  it("log x only from a >= 20x spread of positive values", () => {
    const wide = labels([
      group("g1", "f", { n: 1 }, [config("c1", "node_a", "local", stats(1, 1, 1))]),
      group("g2", "f", { n: 100 }, [config("c2", "node_a", "local", stats(2, 2, 2))]),
    ]);
    expect(familyCharts(wide, "f")[0]?.logX).toBe(true);
    const narrow = labels([
      group("g1", "f", { n: 10 }, [config("c1", "node_a", "local", stats(1, 1, 1))]),
      group("g2", "f", { n: 100 }, [config("c2", "node_a", "local", stats(2, 2, 2))]),
    ]);
    expect(familyCharts(narrow, "f")[0]?.logX).toBe(false);
  });

  it("several strategies: series per node + strategy; unsupported or unmeasured configs are left out", () => {
    const charts = familyCharts(SWEEP, "cpu.zlib");
    const chart = charts[0];
    expect(chart?.param).toBeNull();
    expect(chart?.series.map((s) => s.label)).toEqual(["node_a (local)", "node_b (mode1)"]);
    expect(chart?.series[1]?.points).toHaveLength(1); // cfg_3c unsupported: no point
    expect(chart !== undefined && plottable(chart)).toBe(false); // nothing swept
  });

  it("another swept parameter becomes a facet (one chart per fixed value)", () => {
    const two = labels([
      group("g1", "f", { n: 1, dtype: "float32" }, [config("c1", "node_a", "local", stats(1, 1, 1))]),
      group("g2", "f", { n: 2, dtype: "float32" }, [config("c2", "node_a", "local", stats(2, 2, 2))]),
      group("g3", "f", { n: 1, dtype: "float64" }, [config("c3", "node_a", "local", stats(3, 3, 3))]),
      group("g4", "f", { n: 2, dtype: "float64" }, [config("c4", "node_a", "local", stats(4, 4, 4))]),
    ]);
    const charts = familyCharts(two, "f");
    expect(charts.map((c) => [c.param, c.fixed])).toEqual([
      ["n", { dtype: "float32" }],
      ["n", { dtype: "float64" }],
    ]);
    expect(charts[1]?.series[0]?.points.map((p) => p.y)).toEqual([3, 4]);
  });

  it("conditions get their own charts", () => {
    const cond = labels([
      group("g1", "f", { n: 1 }, [config("c1", "node_a", "local", stats(1, 1, 1))]),
      group("g2", "f", { n: 1 }, [config("c2", "node_a", "local", stats(5, 5, 5))], { condition: "cpu_load:2" }),
    ]);
    expect(familyCharts(cond, "f").map((c) => c.condition)).toEqual(["cpu_load:2", "none"]);
  });

  it("non-finite medians are never plotted", () => {
    const bad = labels([group("g1", "f", { n: 1 }, [config("c1", "node_a", "local", stats(Number.NaN, 1, 2))])]);
    expect(familyCharts(bad, "f")[0]?.series).toEqual([]);
  });

  it("strategy comparison: a bar per measured config, best marked, in config_id order", () => {
    const zlib = SWEEP.groups.find((g) => g.group_id === "grp_3");
    if (zlib === undefined) throw new Error("fixture");
    const chart = strategyChart(zlib);
    expect(chart.bars.map((b) => [b.label, b.y, b.ciLow, b.ciHigh, b.best])).toEqual([
      ["local on node_a", 5000, 4900, 5100, true],
      ["mode1 → node_b", 6000, 5800, 6500, false],
    ]);
    expect(chart.bars[1]?.err).toEqual([200, 500]);
  });

  it("formats params with sorted keys", () => {
    expect(formatParams({ repeats: 4, limit: 10 })).toBe("limit=10, repeats=4");
    expect(formatParams({ dtype: "float32" })).toBe("dtype=float32");
    expect(formatParams({})).toBe("defaults");
  });
});

/* ------------------------------------------------------------ view */

describe("campaign view helpers", () => {
  it("controls per state (bench/control.py state machine)", () => {
    expect(campaignVerbs("active", false)).toEqual(["start", "cancel"]);
    expect(campaignVerbs("active", undefined)).toEqual(["start", "cancel"]);
    expect(campaignVerbs("active", true)).toEqual(["pause", "cancel"]);
    expect(campaignVerbs("paused", true)).toEqual(["resume", "cancel"]);
    expect(campaignVerbs("done", true)).toEqual([]);
    expect(campaignVerbs("cancelled", false)).toEqual([]);
  });

  it("reads the spec budget in both YAML forms, null otherwise", () => {
    expect(specBudget("name: x\nbudget: {max_runs: 40, max_hours: 2}\n")).toEqual({ maxRuns: 40, maxHours: 2 });
    expect(specBudget("budget: {max_runs: 100}  # cap\n")).toEqual({ maxRuns: 100, maxHours: null });
    expect(specBudget("name: x\nbudget:\n  max_runs: 1200\n  # comment\n  max_hours: 9.5\nrepetitions: 3\n")).toEqual({ maxRuns: 1200, maxHours: 9.5 });
    expect(specBudget("name: x\n")).toBeNull();
    expect(specBudget("budget: &b {max_runs: 1\n")).toBeNull();
    expect(specBudget("budget:\n  other: 3\n")).toBeNull();
  });

  it("formats ETA, rates, done %, margins", () => {
    expect(formatEta(null)).toBe("—");
    expect(formatEta(45)).toBe("45 s");
    expect(formatEta(720)).toBe("12 min");
    expect(formatEta(3 * 3600 + 5 * 60)).toBe("3 h 05 min");
    expect(formatEta(3599 + 3600)).toBe("2 h 00 min");
    expect(formatEta(2 * 86_400 + 4 * 3600)).toBe("2 d 4 h");
    expect(formatEta(-1)).toBe("—");
    expect(formatFraction(0.0123)).toBe("1.2 %");
    expect(formatFraction(null)).toBe("—");
    expect(formatDonePercent(37.5)).toBe("37.5 %");
    expect(formatMargin(12.345)).toBe("12.3 %");
    expect(formatMargin(null)).toBe("—");
    expect(labelTone("best")).toBe("ok");
    expect(labelTone("weird")).toBe("neutral");
  });
});

/* ------------------------------------------------------------ quality */

const REPORT = {
  campaign_id: "camp_1",
  quality_version: 1,
  cv_threshold: 0.1,
  summary: {
    runs_by_state: { done: 10, failed: 1 },
    infra_failure_rate: 0.0091,
    owner_preempted: 2,
    condition_failures: { "cpu_load:2": 1 },
    workload_failures: {},
    fillers: 3,
    crit1_infra_ok: true,
    crit3_reps_ok: false,
    pending: false,
  },
  configs: [
    {
      group_id: "grp_1",
      config_id: "cfg_a",
      planned: 3,
      done: 3,
      n: 3,
      total_ms: { median: 900, cv: 0.02, ci_low: 880, ci_high: 950 },
      compute_per_iter_ns: { median: 5.5, cv: 0.01, ci: [5, 6] },
      outliers: 0,
      outlier_attempt_ids: [],
      cv_flag: false,
      reps_flag: false,
      missing: { bytes_in: 0 },
    },
    {
      group_id: "grp_1",
      config_id: "cfg_b",
      planned: 3,
      done: 2,
      n: 2,
      total_ms: { median: 700, cv: 0.3, ci_low: 600, ci_high: 900 },
      outliers: 1,
      outlier_attempt_ids: ["att_1", 7],
      cv_flag: true,
      reps_flag: true,
      missing: { planner_node_id: 2 },
    },
    { config_id: "no group" },
  ],
};

describe("quality report (G5c §5g contract)", () => {
  it("parses what is there, nothing more", () => {
    const result = parseQuality(REPORT);
    if (result.kind !== "report") throw new Error(result.kind);
    const r = result.report;
    expect(r.cv_threshold).toBe(0.1);
    expect(r.summary.crit1_infra_ok).toBe(true);
    expect(r.summary.crit3_reps_ok).toBe(false);
    expect(r.summary.condition_failures).toEqual({ "cpu_load:2": 1 });
    expect(r.configs).toHaveLength(2); // the row without a group_id is dropped
    expect(r.configs[0]?.compute_per_iter_ns).toEqual({ median: 5.5, cv: 0.01, ci_low: 5, ci_high: 6 });
    expect(r.configs[0]?.missing).toEqual({}); // zero counts are not "missing"
    expect(r.configs[1]?.outlier_attempt_ids).toEqual(["att_1"]);
    expect(r.configs[1]?.compute_per_iter_ns).toBeNull();
    expect(r.configs[0]?.family).toBeNull(); // not sent: never invented
  });

  it("flagged configurations first", () => {
    const result = parseQuality(REPORT);
    if (result.kind !== "report") throw new Error(result.kind);
    expect(flaggedFirst(result.report.configs).map((c) => c.config_id)).toEqual(["cfg_b", "cfg_a"]);
  });

  it("another version or a non-object is unsupported", () => {
    expect(parseQuality({ ...REPORT, quality_version: 2 })).toEqual({ kind: "unsupported", version: 2 });
    expect(parseQuality("x")).toEqual({ kind: "unsupported", version: undefined });
  });

  it("a missing summary gives empty/null fields, not zeros", () => {
    const result = parseQuality({ quality_version: 1 });
    if (result.kind !== "report") throw new Error(result.kind);
    expect(result.report.summary.infra_failure_rate).toBeNull();
    expect(result.report.summary.crit1_infra_ok).toBeNull();
    expect(result.report.configs).toEqual([]);
  });

  it("reads the shape G5c's endpoint sends (threshold in summary, fillers by role, outliers in the metric)", () => {
    const result = parseQuality({
      campaign_id: "camp_1",
      quality_version: 1,
      summary: {
        campaign_state: "finished",
        pending: false,
        runs_by_state: { planned: 0, submitted: 0, done: 4, failed: 0, skipped: 0 },
        bench_role_records: { filler: 2, warmup: 1 },
        owner_preempted: 0,
        infra_failure_rate: 0,
        condition_failures: {},
        workload_failures: {},
        crit1_infra_ok: true,
        crit3_reps_ok: true,
        cv_threshold: 0.15,
      },
      configs: [
        {
          group_id: "grp_1",
          config_id: "cfg_a",
          total_ms: { n: 4, median: 900, mean: 910, std: 9, cv: 0.01, ci_low: 880, ci_high: 950, ci_coverage: 0.94, outliers: 1 },
          compute_per_iter_ns: null,
          outlier_attempt_ids: ["att_9"],
          cv_flag: false,
          reps_flag: false,
          missing: { total_ms: 0, bytes_in: 0 },
        },
        { group_id: "grp_1", config_id: "cfg_b", total_ms: null, outlier_attempt_ids: [], cv_flag: false, reps_flag: true },
      ],
    });
    if (result.kind !== "report") throw new Error(result.kind);
    expect(result.report.cv_threshold).toBe(0.15);
    expect(result.report.summary.fillers).toBe(3);
    expect(result.report.configs[0]?.outliers).toBe(1);
    expect(result.report.configs[1]?.outliers).toBe(0); // from the (empty) id list, not invented
    expect(result.report.configs[0]?.missing).toEqual({});
  });

  it("a 404 'no such campaign' is an error, not 'controller too old'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "not_found", message: "no such campaign" } }), { status: 404 }))),
    );
    await expect(getQuality("camp_gone")).rejects.toBeInstanceOf(ApiError);
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "not_found", message: "Not Found" } }), { status: 404 }))),
    );
    await expect(getQuality("camp_1")).resolves.toEqual({ kind: "unavailable" });
  });

  it("404 from the controller = unavailable; other errors throw", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response('{"detail":"Not Found"}', { status: 404 }))));
    await expect(getQuality("camp_1")).resolves.toEqual({ kind: "unavailable" });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "network_error", message: "db down" } }), { status: 503 }))),
    );
    await expect(getQuality("camp_1")).rejects.toBeInstanceOf(ApiError);
  });
});

/* ------------------------------------------------------------ poll */

describe("fallback poll", () => {
  it("polls when the stream is down, or for a hand-driven campaign; never when nothing is open", () => {
    expect(pollInterval(false, { open: true, unowned: false })).toBe(STREAM_DOWN_POLL_MS);
    expect(pollInterval(true, { open: true, unowned: false })).toBe(false);
    expect(pollInterval(true, { open: true, unowned: true })).toBe(MANUAL_POLL_MS);
    expect(pollInterval(false, { open: false, unowned: true })).toBe(false);
  });
});

/* ------------------------------------------------------------ downloads */

function blobOf(text: string): Blob {
  return new Blob([new TextEncoder().encode(text)]);
}

describe("downloads", () => {
  it("only a complete Parquet file passes", async () => {
    expect(await isCompleteParquet(blobOf("PAR1....data....PAR1"))).toBe(true);
    expect(await isCompleteParquet(blobOf("PAR1....data...."))).toBe(false); // truncated: no footer
    expect(await isCompleteParquet(blobOf("{\"error\":1}"))).toBe(false);
    expect(await isCompleteParquet(blobOf("PAR1PAR1"))).toBe(false);
  });

  it("file names", () => {
    expect(recordsFileName("camp_1")).toBe("camp_1-records.parquet");
    expect(labelsFileName("camp_1")).toBe("camp_1-labels.parquet");
  });

  it("apiDownload: cookie session, Accept, the blob; a refusal is a typed ApiError", async () => {
    const fetchFn = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(() => Promise.resolve(new Response("PAR1xxxxPAR1", { status: 200 })));
    vi.stubGlobal("fetch", fetchFn);
    const blob = await apiDownload("/v1/telemetry/export", { query: { campaign: "camp_1" } }, "application/vnd.apache.parquet");
    expect(await blob.text()).toBe("PAR1xxxxPAR1");
    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe("/v1/telemetry/export?campaign=camp_1");
    expect(init?.credentials).toBe("same-origin");
    expect(new Headers(init?.headers).get("Accept")).toBe("application/vnd.apache.parquet");
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(new Response(JSON.stringify({ error: { code: "invalid_input", message: "more than 100 records match" } }), { status: 413 })),
      ),
    );
    await expect(apiDownload("/v1/telemetry/export")).rejects.toMatchObject({ status: 413, code: "invalid_input", message: "more than 100 records match" });
  });
});
