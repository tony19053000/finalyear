/**
 * The browser follows the ONE project spec (D177): the shared vectors of
 * projectSpecVectors.json - whose expectations come from the CLI's own code
 * (tests/unit/test_v08_g4_project_spec.py) - run through the browser's walk,
 * hashing, tree manifest and compute.yaml reader. Same bytes, same tree id.
 */
import { describe, expect, it } from "vitest";

import { inlineHasher } from "./hasher";
import { ManifestYamlError, parseManifestYaml } from "./manifestYaml";
import { walkProject, type ProjectSource } from "./projectWalk";
import vectors from "./projectSpecVectors.json";
import { SPEC } from "./spec";
import { buildTree, type FileDigest, type TreeEntry } from "./treeManifest";

type FileSpec = string | { text: string; exec?: boolean } | { repeat: string; size: number; exec?: boolean };

interface WalkVector {
  name: string;
  files: Record<string, FileSpec>;
  dirs?: string[];
  chunk_size?: number;
  expect_entries: Record<string, unknown>[];
  expect_ignored: number;
  expect_tree_id: string;
}

interface YamlVector {
  name: string;
  text: string;
  expect?: unknown;
  error?: boolean;
  browser_unsupported?: boolean;
}

const encoder = new TextEncoder();

function content(spec: FileSpec): [Uint8Array<ArrayBuffer>, boolean] {
  if (typeof spec === "string") return [encoder.encode(spec), false];
  if ("text" in spec) return [encoder.encode(spec.text), spec.exec === true];
  const unit = encoder.encode(spec.repeat);
  const out = new Uint8Array(spec.size);
  for (let i = 0; i < spec.size; i += 1) out[i] = unit[i % unit.length] ?? 0;
  return [out, spec.exec === true];
}

function vectorSource(vector: Pick<WalkVector, "files" | "dirs">): ProjectSource {
  return {
    name: "project",
    files: Object.entries(vector.files).map(([path, spec]) => {
      const [data, executable] = content(spec);
      return { path, blob: new Blob([data]), executable };
    }),
    dirs: vector.dirs ?? [],
  };
}

function entryJson(entry: TreeEntry): Record<string, unknown> {
  if (entry.kind === "dir") return { path: entry.path, dir: true };
  const out: Record<string, unknown> = { path: entry.path, sha256: entry.sha256, size: entry.size, exec: entry.executable };
  if (entry.chunks.length > 0) out.chunks = entry.chunks;
  return out;
}

describe("walk + tree vectors (same tree id as the CLI)", () => {
  for (const vector of vectors.walk_vectors as unknown as WalkVector[]) {
    it(vector.name, async () => {
      const chunkSize = vector.chunk_size ?? SPEC.chunk_size;
      const walk = await walkProject(vectorSource(vector));
      const hasher = inlineHasher();
      const digests = new Map<string, FileDigest>();
      for (const entry of walk.entries) {
        if (entry.kind !== "file") continue;
        const result = await hasher.hash(entry.blob, chunkSize);
        digests.set(entry.path, { sha256: result.sha256, chunks: result.chunks });
      }
      const tree = await buildTree(walk.entries, digests, chunkSize);
      expect(tree.entries.map(entryJson)).toEqual(vector.expect_entries);
      expect(walk.ignored).toBe(vector.expect_ignored);
      expect(tree.treeId).toBe(vector.expect_tree_id);
    });
  }
});

describe("compute.yaml vectors (read like PyYAML's strict safe loader)", () => {
  for (const vector of vectors.yaml_vectors as unknown as YamlVector[]) {
    it(vector.name, () => {
      if (vector.error === true) {
        expect(() => parseManifestYaml(vector.text)).toThrow(ManifestYamlError);
        return;
      }
      if (vector.browser_unsupported === true) {
        let caught: unknown;
        try {
          parseManifestYaml(vector.text);
        } catch (error) {
          caught = error;
        }
        expect(caught).toBeInstanceOf(ManifestYamlError);
        expect((caught as ManifestYamlError).unsupported).toBe(true);
        expect((caught as ManifestYamlError).message).toContain("compute run");
        return;
      }
      expect(parseManifestYaml(vector.text)).toEqual(vector.expect ?? null);
    });
  }
});
