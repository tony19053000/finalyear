/**
 * The page's side of hashing (D177): a small pool of hashing Web Workers
 * behind one interface. Where no Worker exists (unit tests in jsdom) the same
 * `hashBlob` runs inline - same results, just on the main thread.
 */
import { FileChangedError, hashBlob, type HashResult } from "./hashFile";
import type { HashRequest, HashResponse } from "./hashProtocol";

export interface FileHasher {
  /** `onProgress` gets the bytes hashed since its last call. */
  hash(blob: Blob, chunkSize: number, onProgress?: (deltaBytes: number) => void, signal?: AbortSignal): Promise<HashResult>;
  close(): void;
}

function abortError(): DOMException {
  return new DOMException("cancelled", "AbortError");
}

export function inlineHasher(): FileHasher {
  return {
    async hash(blob, chunkSize, onProgress, signal) {
      if (signal?.aborted === true) throw abortError();
      let last = 0;
      return hashBlob(
        blob,
        chunkSize,
        (done) => {
          onProgress?.(done - last);
          last = done;
        },
        () => signal?.aborted === true,
      );
    },
    close() {
      // nothing to release
    },
  };
}

interface Pending {
  resolve: (result: HashResult) => void;
  reject: (error: Error) => void;
  onProgress?: (deltaBytes: number) => void;
  worker: Worker;
}

/** A pool of `size` workers; requests are spread round-robin by load. */
export function workerHasher(size = Math.max(1, Math.min(4, Math.floor((navigator.hardwareConcurrency || 2) / 2)))): FileHasher {
  const workers: Worker[] = [];
  const load = new Map<Worker, number>();
  const pending = new Map<number, Pending>();
  let nextId = 1;

  for (let i = 0; i < size; i += 1) {
    const worker = new Worker(new URL("./hashWorker.ts", import.meta.url), { type: "module", name: "meshforge-hash" });
    worker.onmessage = (event: MessageEvent<HashResponse>) => {
      const message = event.data;
      const job = pending.get(message.id);
      if (job === undefined) return;
      if (message.type === "progress") {
        job.onProgress?.(message.bytes);
        return;
      }
      pending.delete(message.id);
      load.set(job.worker, (load.get(job.worker) ?? 1) - 1);
      if (message.type === "done") {
        job.resolve(message.result);
      } else if (message.name === "AbortError") {
        job.reject(abortError());
      } else if (message.name === "FileChangedError") {
        job.reject(new FileChangedError(message.message));
      } else {
        job.reject(new Error(message.message));
      }
    };
    worker.onerror = (event) => {
      event.preventDefault();
      for (const [id, job] of pending) {
        if (job.worker !== worker) continue;
        pending.delete(id);
        job.reject(new Error("the hashing worker failed"));
      }
      load.set(worker, 0);
    };
    workers.push(worker);
    load.set(worker, 0);
  }

  return {
    hash(blob, chunkSize, onProgress, signal) {
      if (signal?.aborted === true) return Promise.reject(abortError());
      const worker = workers.reduce((best, w) => ((load.get(w) ?? 0) < (load.get(best) ?? 0) ? w : best));
      const id = nextId;
      nextId += 1;
      load.set(worker, (load.get(worker) ?? 0) + 1);
      return new Promise<HashResult>((resolve, reject) => {
        pending.set(id, { resolve, reject, onProgress, worker });
        signal?.addEventListener(
          "abort",
          () => {
            const cancel: HashRequest = { type: "cancel", id };
            worker.postMessage(cancel);
          },
          { once: true },
        );
        const request: HashRequest = { type: "hash", id, blob, chunkSize };
        worker.postMessage(request);
      });
    },
    close() {
      for (const worker of workers) worker.terminate();
      for (const job of pending.values()) job.reject(abortError());
      pending.clear();
    },
  };
}

/** Workers in a browser; inline where the platform has none (tests). */
export function createHasher(): FileHasher {
  return typeof Worker === "undefined" ? inlineHasher() : workerHasher();
}
