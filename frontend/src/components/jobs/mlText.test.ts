/**
 * V0.15 G5b (D373): the TypeScript copy of `common/ml_explain`. The number
 * table is `FIXED_CASES` of tests/unit/common/test_v015_g5b_ml_explain.py
 * (the two copies must print the same digits); the fixture's `ml_lines` are
 * the Python texts for the explain-contract payload.
 */
import { describe, expect, it } from "vitest";

import contract from "../../test/fixtures/explainContract.json";
import { ML_REASON_TEXT, fixed, mlDecisionText, mlLine, reasonText, shortVersion, type TermMlOut } from "./mlText";

const FIXED_CASES: [number, number, string][] = [
  [2.125, 2, "2.13"],
  [-2.125, 2, "-2.13"],
  [0.5, 0, "1"],
  [1.5, 0, "2"],
  [2.5, 0, "3"],
  [1.005, 2, "1.00"],
  [-0.001, 2, "0.00"],
  [-0.0, 2, "0.00"],
  [33.155, 2, "33.16"],
  [123456.789, 1, "123456.8"],
  [0.0, 0, "0"],
];

function used(extra: Partial<TermMlOut> = {}): TermMlOut {
  return {
    model_version: "m1",
    used: true,
    reason: null,
    ood_reason: null,
    y_hat: -0.235722,
    lo: -0.4,
    hi: -0.07,
    raw_s: 2.531,
    corrected_s: 2.0,
    rate: null,
    ...extra,
  };
}

describe("fixed (Number.toFixed, same digits as the Python copy)", () => {
  it.each(FIXED_CASES)("fixed(%s, %s) = %s", (value, digits, text) => {
    expect(fixed(value, digits)).toBe(text);
  });

  it.each([null, undefined, Number.NaN, Infinity, -Infinity, 1e15, -2e16])("refuses %s", (value) => {
    expect(fixed(value, 2)).toBe("-");
  });
});

describe("words", () => {
  it("shortens versions and names reasons like the CLI", () => {
    expect(shortVersion(`sha256:${"ab".repeat(32)}`)).toBe("abababababab");
    expect(shortVersion("mem-3a1f00aa-extra")).toBe("mem-3a1f00aa");
    expect(shortVersion(null)).toBe("?");
    expect(shortVersion("")).toBe("?");
    expect(reasonText("ood")).toBe("out of distribution");
    expect(reasonText("brand_new")).toBe("brand_new");
    expect(reasonText("toString")).toBe("toString");
    expect(reasonText(null)).toBe("?");
    expect(Object.keys(ML_REASON_TEXT)).toEqual(
      expect.arrayContaining(["no_model", "ood", "interval_too_wide", "exact_by_construction", "below_min_predicted", "predictor_error"]),
    );
  });
});

describe("mlLine", () => {
  it("used: raw -> corrected, version, factor and spread", () => {
    expect(mlLine(used())).toBe("ML 2.53 s -> 2.00 s (m1, x0.79, +-18 %)");
  });

  it("not used: the reason in words, the OOD test's reason, what the model said", () => {
    expect(mlLine(used({ used: false, reason: "ood", ood_reason: "range:x", corrected_s: null }))).toBe(
      "ML not used: out of distribution (range:x) (m1, x0.79, +-18 %)",
    );
  });

  it("does not trust raw/corrected when `used` is false (mutation: used ignored)", () => {
    const line = mlLine(used({ used: false, reason: "interval_too_wide" }));
    expect(line).not.toContain("->");
    expect(line).toContain("interval too wide");
  });

  it("a Mode 3 lane rate follows in ms per item (mutation: rate dropped)", () => {
    const line = mlLine(used({ rate: used({ raw_s: 0.11052, corrected_s: 0.1 }) }));
    expect(line).toBe("ML 2.53 s -> 2.00 s (m1, x0.79, +-18 %); rate: ML 110.52 ms/item -> 100.00 ms/item (m1, x0.79, +-18 %)");
  });

  it("no block, a missing answer, an absurd y_hat", () => {
    expect(mlLine(null)).toBeNull();
    expect(mlLine(undefined)).toBeNull();
    expect(mlLine(used({ y_hat: null, lo: null, hi: null, used: false, reason: "no_model" }))).toBe(
      "ML not used: no model for this term (m1)",
    );
    expect(mlLine(used({ y_hat: 51 }))).toBe("ML 2.53 s -> 2.00 s (m1, +-18 %)");
  });
});

describe("mlDecisionText", () => {
  it("names the set and a whole-decision fallback", () => {
    expect(mlDecisionText(null, null)).toBeNull();
    expect(mlDecisionText(`sha256:${"c".repeat(64)}`, null)).toBe("ML set cccccccccccc");
    expect(mlDecisionText("v", "predictor_error")).toBe("ML set v - fell back to calibration: predictor error");
  });
});

describe("the explain-contract fixture's ML texts", () => {
  interface Line {
    path: (string | number)[];
    text: string;
  }
  const lines = contract.ml_lines as unknown as Line[];

  function at(path: Line["path"]): Record<string, unknown> {
    let node: unknown = contract.payload;
    for (const token of path) node = (node as Record<string | number, unknown>)[token];
    return node as Record<string, unknown>;
  }

  it("covers decisions and terms (used, not used, a lane rate, a whole-decision fallback)", () => {
    expect(lines.length).toBeGreaterThan(8);
    const texts = lines.map((l) => l.text).join("\n");
    expect(texts).toContain(" -> ");
    expect(texts).toContain("ML not used:");
    expect(texts).toContain("; rate: ");
    expect(texts).toContain("fell back to calibration");
  });

  it.each(lines.map((l) => [l.path.join("."), l] as const))("%s: the TypeScript copy prints the Python text", (_p, l) => {
    const node = at(l.path);
    const mine =
      l.path.length === 2
        ? mlDecisionText(node.ml_model_version as string | null, node.ml_fallback as string | null)
        : mlLine(node.ml as TermMlOut | null);
    expect(mine).toBe(l.text);
  });
});
