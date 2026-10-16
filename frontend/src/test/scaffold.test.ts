import { describe, expect, it } from "vitest";

describe("toolchain", () => {
  it("runs in jsdom", () => {
    expect(document.body).toBeInstanceOf(HTMLElement);
  });
});
