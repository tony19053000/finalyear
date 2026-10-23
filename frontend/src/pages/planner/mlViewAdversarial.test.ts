/**
 * V0.15 G5b tester round 1: adversarial inputs for the Models tab's pure
 * helpers (D374): hostile gate documents, an empty registry, huge point
 * counts, odd numbers.
 */
import { describe, expect, it } from "vitest";

import { PREDICTIONS, SETS, models } from "../../test/fixtures/mlModels";
import type { MlPredictionPointOut } from "./mlApi";
import {
  busiestTerm,
  fallbackRows,
  gateText,
  inUseText,
  learnedText,
  mlState,
  regretText,
  scatterOf,
  secondsText,
  termMetricRows,
  termsOf,
} from "./mlView";

const pt = (
  over: Partial<MlPredictionPointOut> = {},
): MlPredictionPointOut => ({
  decision_id: 1,
  term: "compute",
  node_id: "n",
  family: null,
  analytical_s: 1,
  calibrated_s: 1,
  ml_s: null,
  used: false,
  reason: null,
  actual_s: 1,
  ml_model_version: null,
  ...over,
});

describe("regretText never throws on a hostile gate document", () => {
  it.each([
    undefined,
    null,
    0,
    true,
    [],
    ["pending_g4"],
    {},
    { status: 5 },
    { status: "passed", groups: Number.POSITIVE_INFINITY },
    { status: "passed", groups: Number.NaN },
    { status: "rejected", reason: 7 },
    { status: "rejected", nested: { a: 1 }, list: [1, 2] },
    { status: "skipped", "": 1 },
    "",
  ])("%j", (value) => {
    const text = regretText(value);
    expect(typeof text).toBe("string");
    expect(text).not.toMatch(/NaN|Infinity|undefined|\[object/);
  });

  it("pending_g4 on a legacy set says not checked, a G5a block says its status", () => {
    expect(regretText("pending_g4")).toMatch(/not checked/);
    expect(
      regretText({ status: "passed", groups: 14, candidate_mean_pct: 4.5 }),
    ).toBe("passed - groups 14, candidate mean pct 4.500");
  });

  it("a missing regret block still gives a complete gate line", () => {
    const line = gateText({ accepted: true, terms: {} });
    expect(line).toBe("Gate accepted against ?. Regret: —.");
  });
});

describe("gateText with odd shapes", () => {
  it.each([
    null,
    undefined,
    3,
    [],
    [1],
    "x",
    { accepted: "yes" },
    { reference: null },
    { reference: "calibration" },
    { rules_failed: "regret_worse" },
    { rules_failed: [null, {}] },
  ])("%j", (gate) => {
    const text = gateText(gate);
    expect(typeof text).toBe("string");
    expect(text).not.toMatch(/undefined|\[object|NaN/);
  });
});

describe("empty registry", () => {
  it("no sets, nothing serving: state no_model; learned none", () => {
    const empty = models(
      { sets: [], serving_set: null },
      { in_use_set: null, kind: null },
    );
    expect(mlState(empty)).toBe("no_model");
    expect(inUseText(empty)).toContain("no model set in use");
    expect(learnedText([])).toBe("none");
    expect(termMetricRows([], null)).toEqual([]);
  });

  it("no points: no terms, no busiest, no fallback rows, an empty scatter", () => {
    expect(termsOf([])).toEqual([]);
    expect(busiestTerm([])).toBeNull();
    expect(fallbackRows([])).toEqual([]);
    const s = scatterOf([], "compute");
    expect(s).toMatchObject({
      calibration: [],
      ml: [],
      dropped: 0,
      domain: null,
    });
  });
});

describe("huge and odd point sets", () => {
  it("2000 points are summarised fast and exactly", () => {
    const points = Array.from({ length: 2000 }, (_, i) =>
      pt({
        decision_id: i,
        term: i % 2 === 0 ? "compute" : "env",
        used: i % 4 === 0,
        ml_s: i % 4 === 0 ? 1.5 : null,
        ml_model_version: `sha256:${"b".repeat(64)}`,
        reason: i % 4 === 0 ? null : "ood",
        actual_s: 1 + (i % 7),
      }),
    );
    const t0 = performance.now();
    const s = scatterOf(points, "compute");
    const rows = fallbackRows(points);
    expect(performance.now() - t0).toBeLessThan(500);
    expect(s.calibration).toHaveLength(1000);
    expect(s.ml).toHaveLength(500);
    expect(rows.reduce((a, r) => a + r.count, 0)).toBe(2000);
    expect(rows.reduce((a, r) => a + r.share, 0)).toBeCloseTo(1, 9);
  });

  it("a very large scatter does not overflow the stack (Math.min over spread) at the route ceiling", () => {
    const points = Array.from({ length: 20_000 }, (_, i) =>
      pt({ decision_id: i, calibrated_s: 1 + i, actual_s: 2 + i }),
    );
    expect(() => scatterOf(points, "compute")).not.toThrow();
  });

  it("extreme magnitudes stay finite; non-positive, negative and NaN-like values are dropped", () => {
    const s = scatterOf(
      [
        pt({ calibrated_s: 1e-300, actual_s: 1e300 }),
        pt({ calibrated_s: -1 }),
        pt({ actual_s: 0 }),
        pt({ calibrated_s: null }),
        pt({ used: true, ml_s: Number.POSITIVE_INFINITY }),
      ],
      "compute",
    );
    expect(s.calibration).toHaveLength(2); // the extreme one + the last point's calibrated 1/1
    expect(s.ml).toHaveLength(0);
    expect(s.dropped).toBe(3);
    expect(s.domain?.every(Number.isFinite)).toBe(true);
    expect(s.domain?.[0]).toBeGreaterThan(0);
  });

  it("an unknown term and a mixed set keep stable ordering", () => {
    expect(
      termsOf([
        pt({ term: "zzz" }),
        pt({ term: "env" }),
        pt({ term: "aaa" }),
        pt({ term: "compute" }),
      ]),
    ).toEqual(["env", "compute", "aaa", "zzz"]);
  });

  it("a used point whose reason is set, and a point with a set but no reason, bucket separately", () => {
    const rows = fallbackRows([
      pt({ used: true, reason: "ood", ml_model_version: "s" }),
      pt({ ml_model_version: "s" }),
      pt({}),
    ]);
    expect(rows.map((r) => r.key).sort()).toEqual([
      "no_answer",
      "no_model_set",
      "used",
    ]);
  });

  it("secondsText survives junk", () => {
    expect(secondsText(null)).toBe("—");
    expect(secondsText(Number.NaN)).toBe("—");
    expect(secondsText(Number.POSITIVE_INFINITY)).toBe("—");
    expect(secondsText(0)).toBe("0 s");
  });
});

describe("termMetricRows with a hostile gate / members", () => {
  it("terms is an array, held values are strings, failed has non-strings", () => {
    const m = SETS[0]?.members ?? [];
    expect(() => termMetricRows(m, { terms: [] })).not.toThrow();
    expect(() =>
      termMetricRows(m, {
        terms: {
          compute: {
            held_rows: "9",
            male_new: "x",
            coverage: "1",
            failed: [1, null, "a"],
          },
        },
      }),
    ).not.toThrow();
    const rows = termMetricRows(m, {
      terms: { compute: { held_rows: "9", failed: [1, null, "a"] } },
    });
    expect(rows.find((r) => r.term === "compute")?.failed).toBe("a");
    expect(rows.find((r) => r.term === "compute")?.heldRows).toBe("—");
  });

  it("members with missing metric values render dashes", () => {
    const base = SETS[0]?.members[0];
    if (base === undefined) throw new Error("fixture");
    const rows = termMetricRows([{ ...base, metrics: {} }], null);
    expect(rows[0]).toMatchObject({
      rows: "—",
      families: "—",
      selected: "—",
      innerMale: "—",
      intervalWidth: "—",
    });
  });
});

describe("the fixture itself", () => {
  it("is non-empty (guards the tests above)", () => {
    expect(PREDICTIONS.points.length).toBeGreaterThan(0);
  });
});
