/**
 * The tree manifest (D81, common/tree.py): the project as canonical JSON
 * `{"chunk_size", "entries", "v": 1}` - sorted keys, no spaces, UTF-8,
 * entries sorted by the path's UTF-8 bytes; `tree_id` = its SHA-256. The
 * same folder gives the same bytes as `compute run` builds (tested against
 * the CLI's digests in projectSpecVectors.json).
 */
import { ProjectError, utf8Length, type WalkEntry } from "./projectWalk";
import { sha256Hex } from "./sha256";
import { SPEC } from "./spec";

export type TreeEntry =
  | { kind: "file"; path: string; sha256: string; size: number; executable: boolean; chunks: string[] }
  | { kind: "dir"; path: string };

export interface BuiltTree {
  chunkSize: number;
  entries: TreeEntry[];
  manifest: Uint8Array<ArrayBuffer>;
  treeId: string;
}

const encoder = new TextEncoder();

/** Compare by UTF-8 bytes (Python `sorted(key=lambda e: e.path.encode())`). */
export function compareUtf8(a: string, b: string): number {
  const x = encoder.encode(a);
  const y = encoder.encode(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i += 1) {
    const d = (x[i] ?? 0) - (y[i] ?? 0);
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

/** One entry as canonical JSON (keys in sorted order). */
function entryJson(entry: TreeEntry): string {
  const path = JSON.stringify(entry.path);
  if (entry.kind === "dir") return `{"dir":true,"path":${path}}`;
  const chunks = entry.chunks.length > 0 ? `"chunks":[${entry.chunks.map((c) => `"${c}"`).join(",")}],` : "";
  return `{${chunks}"exec":${entry.executable ? "true" : "false"},"path":${path},"sha256":"${entry.sha256}","size":${entry.size}}`;
}

/** common/tree.py `canonical_bytes`. */
export function canonicalBytes(chunkSize: number, entries: readonly TreeEntry[]): Uint8Array<ArrayBuffer> {
  const text = `{"chunk_size":${chunkSize},"entries":[${entries.map(entryJson).join(",")}],"v":${SPEC.tree_version}}`;
  return encoder.encode(text);
}

export interface FileDigest {
  sha256: string;
  chunks: string[];
}

/**
 * Build the tree from the walk and each file's digests (`digests` keyed by
 * path). Folders with any listed entry below them are implied (not listed).
 */
export async function buildTree(
  walked: readonly WalkEntry[],
  digests: ReadonlyMap<string, FileDigest>,
  chunkSize: number = SPEC.chunk_size,
): Promise<BuiltTree> {
  if (chunkSize < SPEC.min_chunk_size || chunkSize > SPEC.max_chunk_size) throw new Error("chunk size out of range");
  const hasChildren = new Set<string>();
  for (const entry of walked) {
    const cut = entry.path.lastIndexOf("/");
    if (cut > 0) hasChildren.add(entry.path.slice(0, cut));
  }
  const entries: TreeEntry[] = [];
  for (const item of walked) {
    if (utf8Length(item.path) > SPEC.max_path_bytes) {
      throw new ProjectError(`path longer than ${SPEC.max_path_bytes} bytes`, item.path);
    }
    if (item.kind === "dir") {
      if (!hasChildren.has(item.path)) entries.push({ kind: "dir", path: item.path });
    } else {
      const digest = digests.get(item.path);
      if (digest === undefined) throw new Error(`no digest for ${item.path}`);
      entries.push({
        kind: "file",
        path: item.path,
        sha256: digest.sha256,
        size: item.size,
        executable: item.executable,
        chunks: item.size > chunkSize ? digest.chunks : [],
      });
    }
    if (entries.length > SPEC.max_entries) {
      throw new ProjectError(`more than ${SPEC.max_entries} entries (add rules to .computeignore)`);
    }
  }
  entries.sort((a, b) => compareUtf8(a.path, b.path));
  const manifest = canonicalBytes(chunkSize, entries);
  if (manifest.length > SPEC.max_tree_manifest_bytes) {
    throw new ProjectError(`tree manifest larger than ${SPEC.max_tree_manifest_bytes} bytes`);
  }
  return { chunkSize, entries, manifest, treeId: `sha256:${await sha256Hex(manifest)}` };
}
