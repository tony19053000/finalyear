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

/** WCAG 2.x contrast ratio of two #rrggbb colours. */
function contrast(a: string, b: string): number {
  const lum = (hex: string): number => {
    const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const [r = 0, g = 0, bl = 0] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi = 0, lo = 0] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("contrast (WCAG AA 4.5:1 for text)", () => {
  const surfaces = ["bg", "surface", "surface-raised", "surface-sunken"];
  const texts = [
    "fg",
    "fg-muted",
    "fg-subtle",
    "accent",
    "status-ok",
    "status-busy",
    "status-degraded",
    "status-failed",
    "status-unknown",
  ];
  for (const theme of ["dark", "light"]) {
    it(`every text token is readable on every surface in the ${theme} theme`, () => {
      const values = new Map(
        [...block(tokensCss, `[data-theme="${theme}"]`).matchAll(/--mf-([a-z0-9-]+):\s*(#[0-9a-f]{6})\b/g)].map(
          (m) => [m[1] ?? "", m[2] ?? ""] as const,
        ),
      );
      const get = (name: string): string => values.get(name) ?? "#000000";
      for (const text of texts) {
        for (const surface of surfaces) {
          expect(contrast(get(text), get(surface)), `${theme} ${text} on ${surface}`).toBeGreaterThanOrEqual(4.5);
        }
      }
      expect(contrast(get("accent-fg"), get("accent"))).toBeGreaterThanOrEqual(4.5);
    });
  }
});
