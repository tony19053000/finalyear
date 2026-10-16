/**
 * Tester r2: FRESH differential runs (new seeds, new generators, no filters) of the browser walk
 * vs the CLI's build_tree and of the compute.yaml reader vs PyYAML. Corpora made by
 * tests/_support/v08_g4_r2_corpus.py (kept honest by tests/unit/test_v08_g4_tester_r2.py).
 */
import { describe, expect, it } from "vitest";

import { inlineHasher } from "./hasher";
import { ManifestYamlError, parseManifestYaml } from "./manifestYaml";
import { ProjectError, walkProject } from "./projectWalk";
import { SPEC } from "./spec";
import { buildTree, type FileDigest } from "./treeManifest";
import walkCorpus from "./walkCorpus_v08_g4_tester_r2.json";
import yamlCorpus from "./yamlCorpus_v08_g4_tester_r2.json";

interface WalkCase {
  files: Record<string, string>;
  expect: { entries?: { path: string }[]; ignored?: number; tree_id?: string; error?: string };
}
interface YamlCase {
  text: string;
  expect: { ok?: unknown; error?: boolean; exotic?: boolean };
}

async function browser(files: Record<string, string>) {
  const enc = new TextEncoder();
  const source = {
    name: "p",
    files: Object.entries(files).map(([path, text]) => ({ path, blob: new Blob([enc.encode(text)]), executable: false })),
    dirs: [],
  };
  try {
    const walk = await walkProject(source);
    const hasher = inlineHasher();
    const digests = new Map<string, FileDigest>();
    for (const e of walk.entries) {
      if (e.kind !== "file") continue;
      const r = await hasher.hash(e.blob, SPEC.chunk_size);
      digests.set(e.path, { sha256: r.sha256, chunks: r.chunks });
    }
    const tree = await buildTree(walk.entries, digests, SPEC.chunk_size);
    return { treeId: tree.treeId, ignored: walk.ignored, paths: tree.entries.map((e) => e.path) };
  } catch (error) {
    return { error: error instanceof ProjectError ? "ProjectError" : `CRASH ${String(error)}`, message: String(error) };
  }
}

describe("fresh walk differential (seed 4242424, 1200 order-heavy projects)", () => {
  it("same entries, ignored count and tree id as the CLI; refusals only where documented (T-5)", async () => {
    const bad: string[] = [];
    let refused = 0;
    let compared = 0;
    for (const c of walkCorpus as unknown as WalkCase[]) {
      const got = await browser(c.files);
      if (c.expect.error !== undefined) {
        if (got.error !== "ProjectError") bad.push(`CLI rejects but browser ${JSON.stringify(got)} ${JSON.stringify(c.files)}`);
        continue;
      }
      if ("error" in got) {
        if (got.error === "ProjectError" && got.message.includes("unsupported ignore pattern")) {
          refused += 1;
          continue;
        }
        bad.push(`browser ${String(got.error)} ${String(got.message)} but CLI ok ${JSON.stringify(c.files)}`);
        continue;
      }
      compared += 1;
      const want = c.expect.entries?.map((e) => e.path);
      if (got.treeId !== c.expect.tree_id || got.ignored !== c.expect.ignored || JSON.stringify(got.paths) !== JSON.stringify(want)) {
        bad.push(`DIFF ${JSON.stringify(c.files)}\n  cli ${JSON.stringify(want)} ign ${String(c.expect.ignored)}\n  web ${JSON.stringify(got.paths)} ign ${String(got.ignored)}`);
      }
    }
    expect(bad.slice(0, 10), `${String(bad.length)} disagreements (${String(compared)} compared, ${String(refused)} loud refusals)`).toEqual([]);
    expect(compared).toBeGreaterThan(1000);
  }, 600_000);
});

function run(text: string): { value?: unknown; refused?: true; crashed?: string } {
  try {
    return { value: parseManifestYaml(text) };
  } catch (error) {
    if (error instanceof ManifestYamlError) return { refused: true };
    return { crashed: String(error) };
  }
}

describe("fresh YAML differential (seed 9090909, ~14.9k token-soup / unicode-injected texts)", () => {
  const cases = yamlCorpus as unknown as YamlCase[];
  it("never mis-parses; never accepts what PyYAML rejects or JSON cannot carry; never crashes", () => {
    const misparse: string[] = [];
    const lenient: string[] = [];
    const crash: string[] = [];
    const tabLine: string[] = [];
    let accepted = 0;
    for (const c of cases) {
      const r = run(c.text);
      if (r.crashed !== undefined) {
        crash.push(`${JSON.stringify(c.text)} ${r.crashed}`);
        continue;
      }
      if (r.refused === true) continue;
      accepted += 1;
      // T-17 (pinned below): a whitespace-only line holding a TAB after the content is accepted.
      if (c.expect.error === true && /[\r\n][ ]*\t[ \t]*[\r\n]*$/.test(c.text) && c.text.trimEnd().length > 0) tabLine.push(c.text);
      else if (c.expect.error === true || c.expect.exotic === true) lenient.push(`${JSON.stringify(c.text)} -> ${JSON.stringify(r.value)} (${c.expect.error === true ? "PyYAML rejects" : "exotic"})`);
      else if (!(JSON.stringify(r.value) === JSON.stringify(c.expect.ok))) misparse.push(`${JSON.stringify(c.text)} -> ${JSON.stringify(r.value)} vs ${JSON.stringify(c.expect.ok)}`);
    }
    expect(misparse.slice(0, 15), `${String(misparse.length)} mis-parses`).toEqual([]);
    expect(lenient.slice(0, 15), `${String(lenient.length)} accepted where CLI rejects`).toEqual([]);
    expect(crash.slice(0, 15), `${String(crash.length)} crashes`).toEqual([]);
    expect(accepted).toBeGreaterThan(300);
    expect(tabLine.length).toBeGreaterThan(0); // the T-17 class is present in this corpus
  }, 120_000);

  // T-17 (minor, pinned): PyYAML rejects a line made only of a TAB; the reader accepts it.
  it.fails("T-17 refuses a tab-only line after the content like the CLI does", () => {
    for (const text of ["a: 1\n\t", "- 1\n\t\n", "a: 1\n\t\nb: 2\n"]) {
      expect(run(text).refused, JSON.stringify(text)).toBe(true);
    }
  });
});
