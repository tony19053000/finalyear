/**
 * D462 (ADR-0026): the Models tab shows which parts a set serves with ML
 * and which keep calibration.
 */
import { describe, expect, it } from "vitest";

import { SETS } from "../../test/fixtures/mlModels";
import { learnedText, mlPartsText, termMetricRows } from "./mlView";

describe("per-part gate (D462)", () => {
  const base = SETS[1];
  if (base === undefined) throw new Error("fixture");
  const member = base.members[0];
  if (member === undefined) throw new Error("fixture");
  const set = {
    ...base,
    approved_terms: ["env"],
    members: [
      { ...member, term: "env", algorithm: "knn", serves: "ml", gate_reason: null },
      {
        ...member,
        term: "compute",
        algorithm: "lightgbm",
        serves: "calibration",
        gate_reason: "worse_than_reference: compute",
      },
    ],
  };

  it("names the ML parts; a set gated before D462 serves all learned", () => {
    expect(mlPartsText(set)).toBe("env");
    expect(mlPartsText({ approved_terms: [] })).toBe("none");
    expect(mlPartsText({ approved_terms: null })).toBe("all learned");
    expect(mlPartsText({})).toBe("all learned");
  });

  it("marks a learned part that keeps calibration", () => {
    expect(learnedText(set.members)).toBe("env=knn, compute=lightgbm (cal)");
  });

  it("shows the source and the gate's reason per term", () => {
    const rows = termMetricRows(set.members, {});
    const env = rows.find((r) => r.term === "env");
    const compute = rows.find((r) => r.term === "compute");
    expect(env?.serves).toBe("ml");
    expect(compute?.serves).toBe("calibration");
    expect(compute?.reason).toBe("worse_than_reference: compute");
  });
});
