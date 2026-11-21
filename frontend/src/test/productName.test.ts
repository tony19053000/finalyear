import { describe, expect, it } from "vitest";

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { PRODUCT_NAME } from "../config";

// The product is shown to people as "Hive". "MeshForge" stays only where it is
// not read by people: wire names (X-MeshForge-* headers), comments, generated
// API types. Lowercase "meshforge" (service, package, paths) and MESHFORGE_*
// env vars never match the case-sensitive "MeshForge" below.
const frontend = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = join(frontend, "src");

/** Wire-level names that keep the old spelling (not visible text). */
const ALLOWED = [/X-MeshForge-[A-Za-z0-9-]*/g];
/** Generated from the controller's OpenAPI (descriptions of wire headers). */
const SKIP_FILES = new Set(["api/schema.d.ts"]);

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      out.push(...sourceFiles(path));
    } else if (/\.(tsx?|html|css)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      out.push(path);
    }
  }
  return out;
}

/** Drop comments so only code, strings and JSX/HTML text remain. */
function stripComments(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

function visibleOldNames(text: string): string[] {
  let code = stripComments(text);
  for (const re of ALLOWED) code = code.replace(re, "");
  return code.split("\n").filter((line) => line.includes("MeshForge")).map((line) => line.trim());
}

describe("visible product name is Hive", () => {
  it("PRODUCT_NAME is Hive", () => {
    expect(PRODUCT_NAME).toBe("Hive");
  });

  it("the scanner flags visible text and ignores comments and wire headers", () => {
    expect(visibleOldNames('const t = "MeshForge on this laptop";')).toHaveLength(1);
    expect(visibleOldNames("<h1>Open MeshForge</h1>")).toHaveLength(1);
    expect(visibleOldNames("// MeshForge comment\n/* MeshForge */\n<!-- MeshForge -->")).toEqual([]);
    expect(visibleOldNames('h["X-MeshForge-CSRF"] = "x"; // https://x')).toEqual([]);
  });

  it("no non-test source file under frontend/ shows MeshForge to people", () => {
    const files = [...sourceFiles(src), join(frontend, "index.html")];
    expect(files.length).toBeGreaterThan(50);
    const offenders: string[] = [];
    for (const file of files) {
      const rel = relative(src, file).replaceAll("\\", "/");
      if (SKIP_FILES.has(rel)) continue;
      for (const line of visibleOldNames(readFileSync(file, "utf8"))) offenders.push(`${rel}: ${line}`);
    }
    expect(offenders).toEqual([]);
  });
});
