/**
 * Tester r1: differential fuzz of the browser walk (.gitignore/.computeignore semantics, sorting,
 * tree id) against the CLI's build_tree over 400 random projects. Corpus: walkCorpus_*.json made by
 * tests/_support/v08_g4_walk_corpus.py (kept honest by tests/unit/test_v08_g4_tester_r1_yaml_corpus.py).
 */
import { describe, expect, it } from "vitest";

import { inlineHasher } from "./hasher";
import { ProjectError, walkProject } from "./projectWalk";
import { SPEC } from "./spec";
import { buildTree, type FileDigest } from "./treeManifest";
import corpus from "./walkCorpus_v08_g4_tester_r1.json";

interface Case {
  files: Record<string, string>;
  expect: { entries?: Record<string, unknown>[]; ignored?: number; tree_id?: string; error?: string };
}

async function browserVerdict(files: Record<string, string>) {
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

describe("browser walk vs CLI build_tree on random projects (tester r1)", () => {
  it("same tree id + ignored count whenever the CLI accepts; ProjectError whenever it rejects", async () => {
    const bad: string[] = [];
    let refused = 0;
    for (const c of corpus as unknown as Case[]) {
      const got = await browserVerdict(c.files);
      if (c.expect.error !== undefined) {
        if (got.error !== "ProjectError") bad.push(`CLI rejects (${c.expect.error}) but browser: ${JSON.stringify(got)} for ${JSON.stringify(c.files)}`);
        continue;
      }
      if ("error" in got) {
        // A LOUD refusal of a rare bracket class (`[]a]`) is the documented D193 stance (T-5, minor).
        if (got.error === "ProjectError" && got.message.includes("unsupported ignore pattern")) {
          refused += 1;
          continue;
        }
        bad.push(`browser ${String(got.error)} but CLI ok for ${JSON.stringify(c.files)}`);
        continue;
      }
      const want = c.expect.entries?.map((e) => e.path);
      if (got.treeId !== c.expect.tree_id || got.ignored !== c.expect.ignored || JSON.stringify(got.paths) !== JSON.stringify(want)) {
        bad.push(`DIFF ${JSON.stringify(c.files)}\n  cli ${JSON.stringify(want)} ign ${String(c.expect.ignored)}\n  web ${JSON.stringify(got.paths)} ign ${String(got.ignored)}`);
      }
    }
    expect(bad.slice(0, 40), `${bad.length} disagreements`).toEqual([]);
    expect(refused).toBeLessThan(60); // loud refusals stay the exception
  }, 120_000);
});

describe("walk defects fixed in G4 fix round 1 (T-6, T-7)", () => {
  it("T-6 a nested `**/` rule + an earlier `!.computeignore` must not ignore a FILE (CLI keeps src/.computeignore)", async () => {
    const got = await browserVerdict({ ".gitignore": "!.computeignore\n", "src/.computeignore": "**/" });
    const want = (corpus as unknown as Case[]).find(
      (c) => JSON.stringify(c.files) === JSON.stringify({ ".gitignore": "!.computeignore\n", "src/.computeignore": "**/" }),
    );
    expect(got).toMatchObject({ treeId: want?.expect.tree_id, ignored: 0 });
  });

  it("T-7 .gitignore `*.log`,`trail\\ ` (escaped trailing space) ignores k.log like the CLI", async () => {
    const files = { ".gitignore": "*.log\n*.log\ntrail\\ \n!keep.log", "keep.log/build/trail /k.log": "" };
    const want = (corpus as unknown as Case[]).find((c) => JSON.stringify(c.files) === JSON.stringify(files));
    const got = await browserVerdict(files);
    expect(got).toMatchObject({ treeId: want?.expect.tree_id, ignored: 1 });
  });
});
