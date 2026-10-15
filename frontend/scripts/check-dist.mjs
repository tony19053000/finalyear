// Post-build gate (V0.8 G2, D169/D181): the built dashboard must run under the
// controller's CSP (controller/web/spa.py):
//   default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; ...
// so the output may contain no inline <script>, no <style> element, no
// style="..." attribute, no setAttribute("style", ...) and no external
// (CDN) URL in HTML/CSS. Exit 1 with every finding listed.
//
//   node scripts/check-dist.mjs [DIST_DIR]
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const dist =
  process.argv[2] ??
  fileURLToPath(new URL("../../src/meshforge/controller/web/dist", import.meta.url));

/** @returns {string[]} */
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

/** [extension regex, rule regex, message] */
const RULES = [
  [/\.html$/, /\sstyle\s*=/i, "inline style attribute"],
  [/\.html$/, /<style[\s>]/i, "inline <style> element"],
  [/\.html$/, /<script(?![^>]*\bsrc=)[^>]*>/i, "inline <script> (no src)"],
  [/\.html$/, /\son[a-z]+\s*=/i, "inline event handler attribute"],
  [/\.(html|css)$/, /(?:src|href)\s*=\s*["']?(?:https?:)?\/\//i, "external URL"],
  [/\.css$/, /url\(\s*["']?(?:https?:)?\/\//i, "external url() in CSS"],
  [/\.css$/, /url\(\s*["']?data:font/i, "inlined font (CSP font-src 'self')"],
  [/\.js$/, /style="/, 'literal style="..." in JS'],
  [/\.js$/, /setAttribute\(\s*["']style["']/, 'setAttribute("style", ...) in JS'],
];

let files;
try {
  files = walk(dist);
} catch (err) {
  console.error(`check-dist: cannot read ${dist}: ${String(err)}`);
  process.exit(1);
}
if (!files.some((f) => f.endsWith("index.html"))) {
  console.error(`check-dist: ${dist} has no index.html - run the build first`);
  process.exit(1);
}

const findings = [];
for (const file of files) {
  const text = /\.(html|css|js|mjs)$/.test(file) ? readFileSync(file, "utf8") : null;
  if (text === null) continue;
  for (const [ext, rule, message] of RULES) {
    if (!ext.test(file)) continue;
    const match = rule.exec(text);
    if (match) {
      const at = Math.max(0, match.index - 40);
      const context = text.slice(at, match.index + 60).replace(/\s+/g, " ");
      findings.push(`${relative(dist, file)}: ${message}: ...${context}...`);
    }
  }
}

if (findings.length > 0) {
  console.error("check-dist: the build would break the dashboard CSP:");
  for (const finding of findings) console.error(`  ${finding}`);
  process.exit(1);
}
console.log(`check-dist: ${files.length} files OK (CSP-safe: no inline style/script, no CDN)`);
