/**
 * The hashing Web Worker (D177): SHA-256 of project files off the main
 * thread, so a multi-GiB folder never freezes the page. One file per
 * message; progress messages while a large file is hashed.
 */
import { hashBlob } from "./hashFile";
import type { HashRequest, HashResponse } from "./hashProtocol";

const scope = self as unknown as {
  onmessage: ((event: MessageEvent<HashRequest>) => void) | null;
  postMessage: (message: HashResponse) => void;
};

const cancelled = new Set<number>();

scope.onmessage = (event: MessageEvent<HashRequest>) => {
  const request = event.data;
  if (request.type === "cancel") {
    cancelled.add(request.id);
    return;
  }
  const { id, blob, chunkSize } = request;
  let last = 0;
  hashBlob(
    blob,
    chunkSize,
    (bytes) => {
      scope.postMessage({ type: "progress", id, bytes: bytes - last });
      last = bytes;
    },
    () => cancelled.has(id),
  ).then(
    (result) => {
      cancelled.delete(id);
      scope.postMessage({ type: "done", id, result });
    },
    (error: unknown) => {
      cancelled.delete(id);
      const name = error instanceof Error ? error.name : "Error";
      const message = error instanceof Error ? error.message : String(error);
      scope.postMessage({ type: "error", id, name, message });
    },
  );
};
