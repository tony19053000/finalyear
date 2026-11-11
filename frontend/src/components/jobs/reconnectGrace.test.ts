import { describe, expect, it } from "vitest";
import { reconnectGraceText } from "./reconnectGrace";

describe("reconnectGraceText (V1.0 G10, D445)", () => {
  const now = Date.parse("2026-11-11T10:00:00Z");
  it("shows the seconds left while the attempt waits for its node", () => {
    expect(reconnectGraceText("2026-11-11T10:00:42Z", now)).toBe(
      "waiting for node to reconnect (42s)",
    );
  });
  it("never shows a negative wait", () => {
    expect(reconnectGraceText("2026-11-11T09:59:00Z", now)).toBe(
      "waiting for node to reconnect (0s)",
    );
  });
  it("is empty when the attempt is not waiting or the time is unreadable", () => {
    expect(reconnectGraceText(null, now)).toBe("");
    expect(reconnectGraceText(undefined, now)).toBe("");
    expect(reconnectGraceText("not a time", now)).toBe("");
  });
});
