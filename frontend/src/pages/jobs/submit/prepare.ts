/**
 * Picked folder/zip -> everything a submit needs, before any upload: the
 * manifest (compute.yaml or inferred), the walk (ignore rules), every file's
 * digests (hashed in the Web Worker pool) and the tree manifest.
 */
import type { FileHasher } from "./hasher";
import { readProjectManifest, type ManifestResult } from "./projectManifest";
import { walkProject, type ProjectSource, type WalkResult } from "./projectWalk";
import { SPEC } from "./spec";
import { buildTree, type BuiltTree, type FileDigest } from "./treeManifest";

export interface Prepared {
  source: ProjectSource;
  manifest: ManifestResult;
  walk: WalkResult;
  tree: BuiltTree;
  /** Hex digest -> one file with those bytes (what the upload sends). */
  blobs: Map<string, Blob>;
}

export interface HashProgress {
  totalBytes: number;
  hashedBytes: number;
  totalFiles: number;
  hashedFiles: number;
}

export async function prepareProject(
  source: ProjectSource,
  hasher: FileHasher,
  options: { signal?: AbortSignal; onProgress?: (p: HashProgress) => void; chunkSize?: number; parallel?: number } = {},
): Promise<Prepared> {
  const chunkSize = options.chunkSize ?? SPEC.chunk_size;
  const manifest = await readProjectManifest(source);
  const walk = await walkProject(source);
  const files = walk.entries.filter((e) => e.kind === "file");
  const progress: HashProgress = { totalBytes: walk.totalBytes, hashedBytes: 0, totalFiles: files.length, hashedFiles: 0 };
  const emit = () => options.onProgress?.({ ...progress });
  emit();
  const digests = new Map<string, FileDigest>();
  const blobs = new Map<string, Blob>();
  const queue = [...files];
  const lane = async () => {
    for (;;) {
      const item = queue.shift();
      if (item === undefined) return;
      if (options.signal?.aborted === true) throw new DOMException("cancelled", "AbortError");
      const result = await hasher.hash(
        item.blob,
        chunkSize,
        (delta) => {
          progress.hashedBytes += delta;
          emit();
        },
        options.signal,
      );
      digests.set(item.path, { sha256: result.sha256, chunks: result.chunks });
      if (!blobs.has(result.sha256)) blobs.set(result.sha256, item.blob);
      progress.hashedFiles += 1;
      emit();
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, options.parallel ?? 4) }, lane));
  const tree = await buildTree(walk.entries, digests, chunkSize);
  return { source, manifest, walk, tree, blobs };
}
