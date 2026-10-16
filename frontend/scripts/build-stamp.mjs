// Build stamp (V0.8 G7, D206): which frontend sources a dashboard build came from.
//
//   node scripts/build-stamp.mjs [DIST_DIR]   # write DIST_DIR/build-info.json (npm run build)
//   node scripts/build-stamp.mjs --print      # print the digest of the sources on disk
//
// The built dashboard (src/meshforge/controller/web/dist) is git-ignored, so a
// checkout can hold a build made from OLDER sources. Tests that read the build
// compare its stamp with the sources on disk (tests/_support/mf_dashboard.py,
// `dist_problem()`, same algorithm) and skip with "rebuild" as the reason, instead
// of failing on a symptom of the stale bundle (D206).
//
// Digest: SHA-256 over, for each input file in sorted order of its POSIX path
// relative to frontend/, the line `<path>\0<sha256 hex of its bytes>\n`.
// Inputs = what `vite build` reads: index.html, package.json, package-lock.json,
// vite.config.ts, tsconfig*.json except the test one, public/**, src/** except
// tests (src/test/**, *.test.ts, *.test.tsx).
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const TOP_FILES = [
  "index.html",
  "package.json",
  "package-lock.json",
  "vite.config.ts",
  "tsconfig.json",
  "tsconfig.app.json",
  "tsconfig.node.json",
];
const TREES = ["public", "src"];

/** @param {string} rel */
function isTest(rel) {
  return rel.startsWith("src/test/") || /\.test\.tsx?$/.test(rel);
}

/** @returns {string[]} */
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

export function sourceFiles() {
  const found = TOP_FILES.filter((name) => {
    try {
      return statSync(join(root, name)).isFile();
    } catch {
      return false;
    }
  });
  for (const tree of TREES) {
    for (const path of walk(join(root, tree))) {
      const rel = relative(root, path).split(sep).join("/");
      if (!isTest(rel)) found.push(rel);
    }
  }
  return found.sort();
}

export function sourcesDigest() {
  const outer = createHash("sha256");
  const files = sourceFiles();
  for (const rel of files) {
    const inner = createHash("sha256").update(readFileSync(join(root, rel))).digest("hex");
    outer.update(`${rel}\0${inner}\n`, "utf8");
  }
  return { digest: outer.digest("hex"), files: files.length };
}

if (process.argv[2] === "--print") {
  console.log(sourcesDigest().digest);
} else {
  const dist =
    process.argv[2] ?? fileURLToPath(new URL("../../src/meshforge/controller/web/dist", import.meta.url));
  const { digest, files } = sourcesDigest();
  const stamp = { schema: 1, sources_sha256: digest, source_files: files };
  writeFileSync(join(dist, "build-info.json"), `${JSON.stringify(stamp, null, 2)}\n`);
  console.log(`build-stamp: ${String(files)} source files, sha256 ${digest.slice(0, 12)}…`);
}
