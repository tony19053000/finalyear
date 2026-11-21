import { afterEach, describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { consoleDemoRequested } from "./consoleDemo";
import { demoRequested, demoSessionRoute } from "./install";

afterEach(() => {
  vi.unstubAllEnvs();
  window.history.replaceState(null, "", "/");
});

describe("demo admin session is development-only", () => {
  it("the vite dev server (DEV) answers /v1/session and /v1/auth/whoami with the demo admin", () => {
    vi.stubEnv("DEV", true);
    expect(demoSessionRoute("/v1/session")).toMatchObject({ session: { user: { role: "admin" } } });
    expect(demoSessionRoute("/v1/auth/whoami")).toMatchObject({ user: { role: "admin" } });
  });

  it("a production build (DEV false) never answers the session routes", () => {
    vi.stubEnv("DEV", false);
    expect(demoSessionRoute("/v1/session")).toBeUndefined();
    expect(demoSessionRoute("/v1/auth/whoami")).toBeUndefined();
  });

  it("other paths are never session answers", () => {
    vi.stubEnv("DEV", true);
    expect(demoSessionRoute("/v1/nodes")).toBeUndefined();
  });
});

describe("?demo=1 is development-only (never in a production build)", () => {
  it("under the vite dev server (DEV) on localhost, ?demo=1 turns the dashboard and console demos on", () => {
    vi.stubEnv("DEV", true);
    window.history.replaceState(null, "", "/?demo=1");
    expect(window.location.hostname).toBe("localhost");
    expect(demoRequested()).toBe(true);
    expect(consoleDemoRequested()).toBe(true);
  });

  it("a production build (DEV false) ignores ?demo=1, even on localhost", () => {
    vi.stubEnv("DEV", false);
    window.history.replaceState(null, "", "/?demo=1&joined=1");
    expect(demoRequested()).toBe(false);
    expect(consoleDemoRequested()).toBe(false);
  });

  it("without ?demo=1 the demo is off in DEV too", () => {
    vi.stubEnv("DEV", true);
    expect(demoRequested()).toBe(false);
    expect(consoleDemoRequested()).toBe(false);
  });

  it("the entry points load the demo modules only inside `if (import.meta.env.DEV)` (no static import)", () => {
    const src = join(__dirname, "..");
    for (const [file, mod] of [
      ["main.tsx", "./demo/install"],
      ["agentConsole/main.tsx", "../demo/consoleDemo"],
    ] as const) {
      const text = readFileSync(join(src, file), "utf8");
      expect(text).not.toMatch(/^import[^;]*from "[^"]*demo\//m);
      const guard = text.indexOf("if (import.meta.env.DEV) {");
      expect(guard).toBeGreaterThan(-1);
      expect(text.indexOf(`await import("${mod}")`)).toBeGreaterThan(guard);
    }
  });
});
