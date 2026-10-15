import { describe, expect, it } from "vitest";

import { cn } from "./cn";
import { jobTone, nodeTone } from "./status";

describe("cn", () => {
  it("joins truthy class names", () => {
    expect(cn("a", false, null, undefined, "b")).toBe("a b");
  });
});

describe("status tones", () => {
  it("maps every node state", () => {
    expect(nodeTone("online")).toBe("ok");
    expect(nodeTone("suspect")).toBe("degraded");
    expect(nodeTone("draining")).toBe("busy");
    expect(nodeTone("offline")).toBe("failed");
    expect(nodeTone("revoked")).toBe("failed");
    expect(nodeTone(null)).toBe("unknown");
    expect(nodeTone("something-new")).toBe("unknown");
  });

  it("maps job states", () => {
    expect(jobTone("running")).toBe("busy");
    expect(jobTone("succeeded")).toBe("ok");
    expect(jobTone("failed")).toBe("failed");
    expect(jobTone("retrying")).toBe("degraded");
    expect(jobTone(undefined)).toBe("unknown");
  });
});
