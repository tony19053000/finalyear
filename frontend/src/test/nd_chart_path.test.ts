/** Node detail charts: the smooth path never overshoots a sample, and the screen-reader summary. */
import { describe, expect, it } from "vitest";

import { lineSummary, monotonePath } from "../pages/nodes/chartPath";

function numbers(d: string): number[] {
  return (d.match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
}

describe("monotonePath", () => {
  it("is empty for no points and a bare move for one", () => {
    expect(monotonePath([])).toBe("");
    expect(monotonePath([{ x: 3, y: 4 }])).toBe("M3.0,4.0");
  });

  it("passes through every sample and ends on the last one", () => {
    const pts = [
      { x: 0, y: 10 },
      { x: 10, y: 40 },
      { x: 20, y: 25 },
      { x: 30, y: 25 },
    ];
    const d = monotonePath(pts);
    expect(d.startsWith("M0.0,10.0C")).toBe(true);
    expect(d.endsWith(" 30.0,25.0")).toBe(true);
    for (const p of pts) expect(d).toContain(`${p.x.toFixed(1)},${p.y.toFixed(1)}`);
    expect(d.match(/C/g)?.length).toBe(3);
  });

  it("never overshoots: control points stay within the samples' y range, a flat run stays flat", () => {
    const pts = [
      { x: 0, y: 0 },
      { x: 10, y: 100 },
      { x: 20, y: 100 },
      { x: 30, y: 0 },
    ];
    const ys = numbers(monotonePath(pts)).filter((_, i) => i % 2 === 1);
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...ys)).toBeLessThanOrEqual(100);
    // the middle (flat) segment's controls sit on y = 100
    expect(monotonePath(pts)).toContain("C13.3,100.0 16.7,100.0 20.0,100.0");
  });
});

describe("lineSummary", () => {
  it("says now / min / max over the sample count, or that there are none", () => {
    expect(lineSummary("CPU", [{ v: 20.2 }, { v: 25 }])).toBe("CPU: now 25%, min 20%, max 25% over 2 samples");
    expect(lineSummary("GPU", [])).toBe("GPU: no samples");
  });
});
