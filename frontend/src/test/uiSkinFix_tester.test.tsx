/**
 * UI-SKIN-FIX tester suite: derived from the task success criteria.
 * (1) demo is dev-only, (5) QR/jsqr gone, (6) login focus + listbox focus ring.
 */
import { QueryClient } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "../app/App";
import { consoleDemoRequested } from "../demo/consoleDemo";
import { demoRequested, demoSessionRoute } from "../demo/install";

const root = join(__dirname, "..", "..");

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("C1: ?demo=1 does nothing in production (DEV false), any URL shape", () => {
  const shapes = ["/?demo=1", "/?demo=1&x", "/?x=1&demo=1", "/?demo=1#DEMO", "/#demo=1", "/?DEMO=1", "/?demo=true"];
  for (const url of shapes) {
    it(`prod: ${url} -> no demo`, () => {
      vi.stubEnv("DEV", false);
      window.history.replaceState(null, "", url);
      expect(demoRequested()).toBe(false);
      expect(consoleDemoRequested()).toBe(false);
      expect(demoSessionRoute("/v1/session")).toBeUndefined();
    });
  }
  it("dev: DEMO in the hash or ?demo=true / DEMO=1 does not enable it either", () => {
    vi.stubEnv("DEV", true);
    for (const url of ["/#DEMO", "/#demo=1", "/?DEMO=1", "/?demo=true", "/?demo=0"]) {
      window.history.replaceState(null, "", url);
      expect(demoRequested()).toBe(false);
    }
  });
  it("prod entry points import demo only behind import.meta.env.DEV", () => {
    for (const f of ["src/main.tsx", "src/agentConsole/main.tsx"]) {
      const src = readFileSync(join(root, f), "utf8");
      const idx = src.indexOf("await import(");
      expect(idx).toBeGreaterThan(-1);
      const before = src.slice(0, idx);
      expect(before.lastIndexOf("import.meta.env.DEV")).toBeGreaterThan(before.lastIndexOf("}\n"));
      expect(src).not.toMatch(/^import .*demo/m);
    }
  });
  it("built bundles (when present) contain no demo code", () => {
    for (const d of ["../src/meshforge/controller/web/dist", "../src/meshforge/agent/console/dist"]) {
      const idx = join(root, d, "index.html");
      if (!existsSync(idx)) continue;
      expect(readFileSync(idx, "utf8").toLowerCase()).not.toContain("demo");
    }
  });
});

describe("C5: QR scanner removed, jsqr gone", () => {
  it("jsqr is not in package.json or lock; QrScanner file is gone", () => {
    expect(readFileSync(join(root, "package.json"), "utf8").toLowerCase()).not.toContain("jsqr");
    expect(readFileSync(join(root, "package-lock.json"), "utf8").toLowerCase()).not.toContain("jsqr");
    expect(existsSync(join(root, "src/agentConsole/components/QrScanner.tsx"))).toBe(false);
  });
});

describe("C6: focus", () => {
  it("login name field is focused on first render", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "unauthorized", message: "no" } }), { status: 401, headers: { "Content-Type": "application/json" } }))),
    );
    window.history.replaceState(null, "", "/login");
    render(<App queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })} />);
    await screen.findByRole("heading", { name: "Sign in" });
    const name = document.activeElement as HTMLElement;
    expect(name.tagName).toBe("INPUT");
    expect(name.getAttribute("type")).not.toBe("password");
  });
  it(".jb-listbox has a :focus-visible outline and no later rule removes it", () => {
    const css = readFileSync(join(root, "src/styles/index.css"), "utf8");
    expect(css).toMatch(/\.jb-listbox:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--mf-focus\)/);
    const after = css.slice(css.indexOf(".jb-listbox:focus-visible"));
    expect(after).not.toMatch(/\.jb-listbox:focus-visible\s*\{[^}]*outline:\s*none/);
  });
});
