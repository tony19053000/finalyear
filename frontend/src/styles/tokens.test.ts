import { describe, expect, it } from "vitest";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Read as files: Vitest does not process CSS imports (css: false).
const here = dirname(fileURLToPath(import.meta.url));
const read = (name: string): string => readFileSync(join(here, name), "utf8");
const indexCss = read("index.css");
const tokensCss = read("tokens.css");
const fontsCss = read("fonts.css");

function block(css: string, selector: string): string {
  const start = css.indexOf(selector);
  expect(start, `${selector} block`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

function names(cssBlock: string): string[] {
  return [...cssBlock.matchAll(/(--mf-[a-z0-9-]+)\s*:/g)].map((m) => m[1] ?? "").sort();
}

describe("design tokens", () => {
  const dark = names(block(tokensCss, '[data-theme="dark"]'));
  const light = names(block(tokensCss, '[data-theme="light"]'));

  it("light theme defines exactly the dark theme's tokens", () => {
    expect(dark.length).toBeGreaterThan(20);
    expect(light).toEqual(dark);
  });

  it("has five status tones and one accent", () => {
    for (const tone of ["ok", "busy", "degraded", "failed", "unknown"]) {
      expect(dark).toContain(`--mf-status-${tone}`);
    }
    expect(dark.filter((n) => /^--mf-accent(-hover|-fg|-soft)?$/.test(n))).toHaveLength(4);
  });

  it("removes Tailwind's default palette so only tokens are utilities", () => {
    expect(indexCss).toContain("--color-*: initial;");
    for (const token of dark.filter((n) => !["--mf-shadow"].includes(n))) {
      expect(indexCss, token).toContain(`var(${token})`);
    }
  });

  it("loads fonts only from vendored files (no CDN)", () => {
    const urls = [...fontsCss.matchAll(/url\("([^"]+)"\)/g)].map((m) => m[1] ?? "");
    expect(urls).toHaveLength(4);
    for (const url of urls) expect(url.startsWith("../assets/fonts/")).toBe(true);
  });
});
