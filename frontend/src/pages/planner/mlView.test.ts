/**
 * V0.15 G5b (D374): the Models tab's pure helpers - tolerant of the open
 * gate document (G3 rows with `regret: "pending_g4"`, G5a's regret block,
 * junk), the tab's state, the fallback counts and the scatter's domain.
 */
import { describe, expect, it } from "vitest";

import { PREDICTIONS, SETS, models } from "../../test/fixtures/mlModels";
import {
  busiestTerm,
  canPin,
  canUnpin,
  fallbackRows,
  gateText,
  inUseText,
  learnedText,
  mlState,
  regretText,
  retrainText,
  scatterOf,
  termMetricRows,
  termsOf,
} from "./mlView";

describe("mlState / inUseText", () => {
  it("names each state", () => {
    expect(mlState(models())).toBe("in_use");
    expect(mlState(models({}, { mode: "off" }))).toBe("off");
    expect(mlState(models({}, { in_use_set: null }))).toBe("no_model");
    expect(mlState(models({}, { in_use_set: null, unavailable: "library_missing" }))).toBe("not_loaded");
    expect(mlState(models({}, { in_use_set: null }, { available: false }))).toBe("unavailable_extra");
    // a pinned set still serving without the extra (numpy-only serving): in use wins
    expect(mlState(models({}, {}, { available: false }))).toBe("in_use");
    expect(inUseText(models({}, { mode: "pinned", kind: "pinned" }))).toBe("Planning with set bbbbbbbbbbbb (pinned, mode pinned).");
  });
});

describe("retrainText", () => {
  const r = models().retrain;
  it("every outcome of the last retrain", () => {
    const at = { trigger: "manual", started_at: "t", finished_at: "t", rows: 10, seconds: {}, superseded_set: null };
    expect(retrainText({ ...r, last: { ...at, created_set: SETS[0]?.set_version ?? null, status: "rejected", reason: "regret_worse: x", skipped: null, error: null, detail: null } })).toBe(
      "Last retrain: set cccccccccccc rejected - regret_worse: x.",
    );
    expect(retrainText({ ...r, last: { ...at, created_set: null, status: null, reason: null, skipped: "no_new_runs", error: null, detail: null } })).toBe(
      "Last retrain wrote nothing (no_new_runs).",
    );
    expect(retrainText({ ...r, last: { ...at, created_set: null, status: null, reason: null, skipped: null, error: "timeout", detail: "600 s" } })).toBe(
      "Last retrain FAILED (timeout: 600 s).",
    );
  });
});

describe("gate and regret texts", () => {
  it("reads the G3 pending marker, the G5a block, anything else", () => {
    expect(regretText("pending_g4")).toContain("not checked");
    expect(regretText({ status: "skipped", reason: "too_few_groups", groups: 4 })).toBe("skipped - groups 4 - too_few_groups");
    expect(regretText(null)).toBe("—");
    expect(regretText(42)).toBe("—");
    expect(gateText({})).toBe("No activation gate recorded.");
    expect(gateText("junk")).toBe("No activation gate recorded.");
    expect(gateText({ accepted: false, rule: "coverage", rules_failed: ["coverage", 7], reference: "x" })).toBe(
      "Gate rejected (coverage) against ?; failed coverage. Regret: —.",
    );
  });

  it("per-term rows survive a malformed gate", () => {
    const rows = termMetricRows(SETS[0]?.members ?? [], { terms: { compute: "junk", transfer: { held_rows: "x" } } });
    expect(rows.map((r) => [r.term, r.heldRows, r.maleModel])).toEqual([
      ["transfer", "—", "—"],
      ["compute", "—", "—"],
    ]);
  });
});

describe("sets", () => {
  it("learned terms and which action is offered", () => {
    expect(learnedText(SETS[0]?.members ?? [])).toBe("compute=lightgbm");
    expect(learnedText([])).toBe("none");
    const [rejected, active] = SETS;
    if (rejected === undefined || active === undefined) throw new Error("fixture");
    expect(canPin(rejected)).toBe(false);
    expect(canPin(active)).toBe(true);
    expect(canUnpin({ ...active, status: "pinned" })).toBe(true);
    expect(canPin({ ...active, status: "pinned" })).toBe(false);
  });
});

describe("predictions", () => {
  it("terms, the busiest term, fallback shares summing to 1", () => {
    expect(termsOf(PREDICTIONS.points)).toEqual(["env", "compute"]);
    expect(busiestTerm(PREDICTIONS.points)).toBe("compute");
    expect(busiestTerm([])).toBeNull();
    const rows = fallbackRows(PREDICTIONS.points);
    expect(rows.reduce((n, r) => n + r.count, 0)).toBe(PREDICTIONS.points.length);
    expect(rows.reduce((n, r) => n + r.share, 0)).toBeCloseTo(1);
    expect(fallbackRows([])).toEqual([]);
  });

  it("a single value gets a non-empty domain; a term with no point has none", () => {
    const one = PREDICTIONS.points.filter((p) => p.decision_id === 11 && p.term === "compute").map((p) => ({ ...p, calibrated_s: 2, actual_s: 2, used: false }));
    expect(scatterOf(one, "compute").domain).toEqual([1, 4]);
    expect(scatterOf(PREDICTIONS.points, "combine").domain).toBeNull();
  });
});
