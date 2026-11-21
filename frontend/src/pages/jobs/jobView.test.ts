import { describe, expect, it } from "vitest";

import type { DecisionOut } from "./jobsApi";
import { JOB_STATES } from "./jobsApi";
import { STATE_TABS, countByTab, durationMs, planView, progressOf, speedFigure, tabOf } from "./jobView";

function decision(candidates: DecisionOut["candidates"], chosen: string): DecisionOut[] {
  return [
    {
      id: 1,
      attempt_no: 1,
      attempt_id: "a",
      outcome: "dispatched",
      policy: "cost_model",
      chosen_node_id: chosen,
      created_at: "2026-10-16T08:00:00Z",
      snapshot_digest: "x",
      candidates,
    },
  ];
}

const est = (total_s: number) => ({ total_s, terms: {} });

describe("state tabs", () => {
  it("every API state belongs to exactly one tab (besides All)", () => {
    for (const state of JOB_STATES) {
      expect(STATE_TABS.filter((t) => t.states.includes(state))).toHaveLength(1);
    }
    expect(tabOf([])?.key).toBe("all");
    expect(tabOf(["queued", "submitted", "planning"])?.key).toBe("queued");
    expect(tabOf(["retrying"])).toBeNull(); // a hand-made filter no tab matches
  });

  it("counts by tab, zero when none", () => {
    const counts = countByTab([{ state: "running" }, { state: "retrying" }, { state: "failed" }]);
    expect(counts.get("all")).toBe(3);
    expect(counts.get("running")).toBe(2);
    expect(counts.get("failed")).toBe(1);
    expect(counts.get("cancelled")).toBe(0);
  });
});

describe("plan and progress", () => {
  it("a sent-away job: mode, node, speed-up from the planner's estimates", () => {
    const d = decision(
      [
        { node_id: "B", mode: "offload", feasible: true, rank: 1, estimate: est(100) },
        { node_id: "A", mode: "local", feasible: true, rank: 2, estimate: est(320) },
      ],
      "B",
    );
    const plan = planView({ placement: { mode: "auto" } }, d);
    expect(plan.mode).toBe("offload");
    expect(plan.nodeIds).toEqual(["B"]);
    expect(speedFigure(plan.speed)).toEqual({ figure: "3.2", unit: "×", label: "faster" });
    const t0 = Date.parse("2026-10-16T08:00:00Z");
    const p = progressOf({ state: "running", created_at: "2026-10-16T08:00:00Z" }, plan, undefined, t0 + 50_000);
    expect(p).toEqual({ percent: 50, estimated: true });
    // never claims done before the job is
    expect(progressOf({ state: "running", created_at: "2026-10-16T08:00:00Z" }, plan, undefined, t0 + 900_000).percent).toBe(95);
    expect(progressOf({ state: "queued", created_at: "2026-10-16T08:00:00Z" }, plan, undefined, t0).percent).toBeNull();
  });

  it("a split: the node set, real % from chunk counts; succeeded is 100", () => {
    const d = decision([{ node_id: "A", mode: "split", feasible: true, rank: 1, node_set: ["A", "B"], estimate: est(60) }], "A");
    const plan = planView({ placement: { mode: "auto" } }, d);
    expect(plan.nodeIds).toEqual(["A", "B"]);
    const split = { chunks: { total: 40, done: 10, leased: 2, pending: 28, failed: 0 } };
    expect(progressOf({ state: "running", created_at: "2026-10-16T08:00:00Z" }, plan, split, 0)).toEqual({ percent: 25, estimated: false });
    expect(progressOf({ state: "succeeded", created_at: "2026-10-16T08:00:00Z" }, plan, split, 0).percent).toBe(100);
  });

  it("no decision: a pinned placement, else nothing; duration ends at the last update when finished", () => {
    expect(planView({ placement: { mode: "local", node_id: "A" } }, undefined)).toEqual({ mode: "local", nodeIds: ["A"], speed: null });
    expect(planView({ placement: { mode: "auto" } }, []).mode).toBeNull();
    const job = { state: "failed" as const, created_at: "2026-10-16T08:00:00Z", updated_at: "2026-10-16T08:02:00Z" };
    expect(durationMs(job, Date.parse("2026-10-17T00:00:00Z"))).toBe(120_000);
  });
});
