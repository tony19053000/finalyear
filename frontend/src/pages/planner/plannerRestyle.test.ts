/** The restyled Planner page's pure helpers: the headline's "within N %", the error distribution's points and bins. */
import { describe, expect, it } from "vitest";

import { PREDICTIONS } from "../../test/fixtures/mlModels";
import { errorBins, estimateErrors, withinShare } from "./mlView";
import { calibrationLight, verdictLight, withinPct } from "./plannerView";

describe("headline: the held-out error as 'within N % of measured'", () => {
  it("is e^error - 1 (the error is a mean |ln(predicted / actual)|)", () => {
    expect(withinPct(0.1388)?.toFixed(0)).toBe("15");
    expect(withinPct(0)).toBe(0);
    expect(withinPct(null)).toBeNull();
    expect(withinPct(Number.NaN)).toBeNull();
    expect(withinPct(-0.1)).toBeNull();
  });

  it("lights: a word beside a flat light, never a badge", () => {
    expect(verdictLight("PASS")).toBe("bg-status-ok");
    expect(verdictLight("leaked")).toBe("bg-status-failed");
    expect(verdictLight("too_few_groups")).toBe("bg-fg-subtle");
    expect(calibrationLight("pinned")).toBe("bg-[#ffc93c]");
    expect(calibrationLight("superseded")).toBe("bg-fg-subtle");
  });
});

describe("error distribution", () => {
  it("uses the ML prediction where ML acted, else the calibrated one; zero / missing values are left out", () => {
    const compute = estimateErrors(PREDICTIONS.points, "compute");
    // #11 ML 9.5 vs 9.2, ... #15 calibrated 44 vs 70 (ood), #16 calibrated 1.2 vs 1.1 (no set)
    expect(compute).toHaveLength(6);
    expect(compute[0]).toBeCloseTo((9.5 / 9.2 - 1) * 100);
    expect(compute[4]).toBeCloseTo((44 / 70 - 1) * 100);
    // env #12 has a calibrated 0: no estimate to compare
    expect(estimateErrors(PREDICTIONS.points, "env")).toHaveLength(1);
  });

  it("bins of 5 % centred on 0 with an open bin at each end; every error lands in one", () => {
    const errors = [-80, -41, -2.4, 0, 2.4, 2.5, 14.9, 60];
    const bins = errorBins(errors);
    expect(bins).toHaveLength(19);
    expect(bins.reduce((n, b) => n + b.count, 0)).toBe(errors.length);
    expect(bins.find((b) => b.centre === 0)?.count).toBe(3);
    expect(bins.find((b) => b.centre === 5)?.count).toBe(1);
    expect(bins[0]).toMatchObject({ overflow: true, count: 1 });
    expect(bins.at(-1)).toMatchObject({ overflow: true, count: 1 });
    expect(withinShare(errors)).toBeCloseTo(5 / 8);
    expect(withinShare([])).toBeNull();
  });
});
