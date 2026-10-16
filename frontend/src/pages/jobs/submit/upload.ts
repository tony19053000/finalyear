/**
 * Upload a project tree like `compute run` does (cli/transfer.py, D82) -
 * the existing resumable CAS API, no new endpoint:
 *
 * 1. `POST /v1/blobs/have` with every blob the tree names + the manifest
 *    (batched) -> what the controller is missing;
 * 2. a missing one-chunk file is one `PUT /v1/blobs/{hex}`;
 * 3. a missing chunked file goes part by part (`PUT .../parts/{i}` with the
 *    chunk's digest), skipping what `GET .../parts` says already arrived -
 *    an interrupted upload (cancel, reload, network) resumes there - then
 *    `POST .../complete`;
 * 4. the tree manifest last.
 *
 * Every part is re-hashed just before it is sent and checked against the
 * manifest: a file edited after hashing is reported, never sent under the
 * wrong name. Transient failures (no answer, 5xx, 429, 503 busy) are retried
 * a few times with backoff. A Retry-After hint is honoured up to
 * MAX_RETRY_WAIT_MS; a longer one fails the upload with a clear message (the
 * user can try again later) instead of freezing the page. Every wait ends at
 * once when the upload is cancelled.
 */
import { ApiError, NetworkError } from "../../../api/errors";
import { rawRequest, type BlobMissing, type BlobParts } from "../jobsApi";
import { sha256Hex } from "./sha256";
import { SPEC } from "./spec";
import type { BuiltTree } from "./treeManifest";

export interface UploadProgress {
  /** Bytes this run has to send (missing files, minus parts already there). */
  totalBytes: number;
  sentBytes: number;
  /** Blobs (unique file contents) to send / sent. */
  totalBlobs: number;
  sentBlobs: number;
  current: string | null;
}

export interface UploadReport {
  uploadedBlobs: number;
  uploadedBytes: number;
  skippedBlobs: number;
  skippedBytes: number;
  /** Parts an earlier, interrupted upload had already delivered. */
  resumedParts: number;
}

export class UploadError extends Error {
  override readonly name = "UploadError";
}

export interface UploadOptions {
  signal?: AbortSignal;
  onProgress?: (progress: UploadProgress) => void;
  /** Parallel blob uploads. */
  concurrency?: number;
  retries?: number;
  /** Tests: no real waiting. */
  sleep?: (ms: number) => Promise<void>;
}

interface Blobby {
  hex: string;
  path: string;
  size: number;
  chunks: string[];
  blob: Blob;
}

/** The longest server-requested wait (Retry-After) the upload sits through. */
export const MAX_RETRY_WAIT_MS = 60_000;

const defaultSleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

function cancelled(): DOMException {
  return new DOMException("cancelled", "AbortError");
}

/** `sleep(ms)` that rejects with an AbortError as soon as `signal` aborts. */
function abortableSleep(sleep: (ms: number) => Promise<void>, ms: number, signal: AbortSignal | undefined): Promise<void> {
  if (signal === undefined) return sleep(ms);
  if (signal.aborted) return Promise.reject(cancelled());
  return new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      reject(cancelled());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    sleep(ms).then(
      () => {
        signal.removeEventListener("abort", onAbort);
        resolve();
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(error instanceof Error ? error : new Error(String(error)));
      },
    );
  });
}

function transient(error: unknown): boolean {
  if (error instanceof NetworkError) return true;
  return error instanceof ApiError && (error.status >= 500 || error.status === 429);
}

async function withRetry<T>(call: () => Promise<T>, options: UploadOptions): Promise<T> {
  const retries = options.retries ?? 3;
  const sleep = options.sleep ?? defaultSleep;
  for (let attempt = 0; ; attempt += 1) {
    if (options.signal?.aborted === true) throw cancelled();
    try {
      return await call();
    } catch (error) {
      if (attempt >= retries || !transient(error)) throw error;
      const hinted = error instanceof ApiError && error.retryAfterS !== undefined ? error.retryAfterS * 1000 : 0;
      if (!(hinted <= MAX_RETRY_WAIT_MS)) {
        // NaN/Infinity/huge: never sit through it (T-8).
        const seconds = Number.isFinite(hinted) ? String(Math.ceil(hinted / 1000)) : "an unknown number of";
        throw new UploadError(
          `the controller is busy and asked to wait ${seconds} seconds (more than ${String(MAX_RETRY_WAIT_MS / 1000)}); try again later`,
        );
      }
      await abortableSleep(sleep, Math.min(MAX_RETRY_WAIT_MS, Math.max(hinted, 500 * 2 ** attempt)), options.signal);
    }
  }
}

/** Upload what the controller lacks of `tree`; `blobs` = hex -> a file with those bytes. */
export async function uploadTree(
  tree: BuiltTree,
  blobs: ReadonlyMap<string, Blob>,
  outer: UploadOptions = {},
): Promise<UploadReport> {
  // One failed lane stops the others (their requests are aborted).
  const inner = new AbortController();
  if (outer.signal?.aborted === true) inner.abort();
  outer.signal?.addEventListener("abort", () => {
    inner.abort();
  }, { once: true });
  const signal = inner.signal;
  const options: UploadOptions = { ...outer, signal };
  const unique = new Map<string, Blobby>();
  for (const entry of tree.entries) {
    if (entry.kind !== "file" || unique.has(entry.sha256)) continue;
    const blob = blobs.get(entry.sha256);
    if (blob === undefined) throw new Error(`no source for ${entry.path}`);
    unique.set(entry.sha256, { hex: entry.sha256, path: entry.path, size: entry.size, chunks: entry.chunks, blob });
  }
  const treeHex = tree.treeId.replace(/^sha256:/, "");
  const digests = [...unique.keys(), treeHex].map((hex) => `sha256:${hex}`);
  const missing = new Set<string>();
  for (let start = 0; start < digests.length; start += SPEC.have_batch) {
    const batch = digests.slice(start, start + SPEC.have_batch);
    const answer = await withRetry(
      () =>
        rawRequest<BlobMissing>("/v1/blobs/have", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ digests: batch }),
          signal,
        }),
      options,
    );
    for (const digest of answer.missing) missing.add(digest);
  }

  const report: UploadReport = { uploadedBlobs: 0, uploadedBytes: 0, skippedBlobs: 0, skippedBytes: 0, resumedParts: 0 };
  const todo: Blobby[] = [];
  for (const item of unique.values()) {
    if (missing.has(`sha256:${item.hex}`)) todo.push(item);
    else {
      report.skippedBlobs += 1;
      report.skippedBytes += item.size;
    }
  }
  const progress: UploadProgress = {
    totalBytes: todo.reduce((sum, b) => sum + b.size, 0),
    sentBytes: 0,
    totalBlobs: todo.length,
    sentBlobs: 0,
    current: null,
  };
  const emit = () => options.onProgress?.({ ...progress });
  emit();

  const sendWhole = async (item: Blobby) => {
    await withRetry(
      () =>
        rawRequest(`/v1/blobs/${item.hex}`, {
          method: "PUT",
          headers: { "Content-Type": "application/octet-stream" },
          body: item.blob,
          signal,
        }),
      options,
    );
    progress.sentBytes += item.size;
    report.uploadedBytes += item.size;
  };

  const sendParts = async (item: Blobby) => {
    let received = new Set<number>();
    try {
      const status = await withRetry(() => rawRequest<BlobParts>(`/v1/blobs/${item.hex}/parts`, { method: "GET", signal }), options);
      if (status.complete) {
        progress.totalBytes -= item.size;
        return;
      }
      // Only real part indexes of THIS file count (T-9: a confused server may list others).
      received = new Set(status.received.filter((i) => Number.isInteger(i) && i >= 0 && i < item.chunks.length));
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) throw error;
    }
    report.resumedParts += received.size;
    for (const [index, expected] of item.chunks.entries()) {
      const start = index * tree.chunkSize;
      const length = Math.min(tree.chunkSize, item.size - start);
      if (received.has(index)) {
        progress.totalBytes -= length;
        emit();
        continue;
      }
      const data = new Uint8Array(await item.blob.slice(start, start + length).arrayBuffer());
      if (data.length !== length || (await sha256Hex(data)) !== expected) {
        throw new UploadError(`${item.path} changed after it was hashed; pick the folder again`);
      }
      await withRetry(
        () =>
          rawRequest(`/v1/blobs/${item.hex}/parts/${String(index)}`, {
            method: "PUT",
            headers: {
              "Content-Type": "application/octet-stream",
              "X-MeshForge-Blob-Size": String(item.size),
              "X-MeshForge-Chunk-Size": String(tree.chunkSize),
              "X-MeshForge-Chunk-Sha256": expected,
            },
            body: data,
            signal,
          }),
        options,
      );
      progress.sentBytes += length;
      report.uploadedBytes += length;
      emit();
    }
    await withRetry(() => rawRequest(`/v1/blobs/${item.hex}/complete`, { method: "POST", signal }), options);
  };

  const queue = [...todo];
  const worker = async () => {
    for (;;) {
      const item = queue.shift();
      if (item === undefined) return;
      progress.current = item.path;
      emit();
      try {
        if (item.chunks.length > 0) await sendParts(item);
        else await sendWhole(item);
      } catch (error) {
        inner.abort();
        if (error instanceof ApiError && error.code === "integrity_error") {
          throw new UploadError(`${item.path} changed after it was hashed; pick the folder again`);
        }
        throw error;
      }
      progress.sentBlobs += 1;
      report.uploadedBlobs += 1;
      emit();
    }
  };
  const lanes = Math.max(1, Math.min(options.concurrency ?? 3, todo.length));
  await Promise.all(Array.from({ length: lanes }, worker));

  if (missing.has(tree.treeId)) {
    await withRetry(
      () =>
        rawRequest(`/v1/blobs/${treeHex}`, {
          method: "PUT",
          headers: { "Content-Type": "application/octet-stream" },
          body: tree.manifest,
          signal,
        }),
      options,
    );
    report.uploadedBytes += tree.manifest.length;
  }
  progress.current = null;
  emit();
  return report;
}
