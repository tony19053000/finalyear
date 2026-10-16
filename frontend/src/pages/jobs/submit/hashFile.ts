/**
 * Hash one file the way the tree manifest needs it (D80/D81): the whole
 * file's SHA-256 and, when it is larger than one chunk, the SHA-256 of every
 * `chunkSize` piece (common/tree.py `_hash_file`). Runs in the hashing Web
 * Worker (hashWorker.ts) or, where workers are unavailable, inline.
 */
import { Sha256, sha256Hex } from "./sha256";

export interface HashResult {
  sha256: string;
  /** Per-chunk digests; empty when the file is one chunk (size <= chunkSize). */
  chunks: string[];
  size: number;
}

export class FileChangedError extends Error {
  override readonly name = "FileChangedError";
}

/** Read at most this much at a time (bounded memory, steady progress). */
const READ_BYTES = 8 * 1024 * 1024;

export async function hashBlob(
  blob: Blob,
  chunkSize: number,
  onProgress?: (bytesDone: number) => void,
  isCancelled?: () => boolean,
): Promise<HashResult> {
  const size = blob.size;
  if (size <= chunkSize) {
    const data = new Uint8Array(await blob.arrayBuffer());
    if (data.length !== size) throw new FileChangedError("the file changed while it was hashed");
    onProgress?.(size);
    return { sha256: await sha256Hex(data), chunks: [], size };
  }
  const whole = new Sha256();
  const chunks: string[] = [];
  let offset = 0;
  while (offset < size) {
    if (isCancelled?.() === true) throw new DOMException("hashing cancelled", "AbortError");
    const end = Math.min(size, offset + chunkSize);
    // A chunk is read in READ_BYTES pieces only if it is larger than that.
    const pieces: Uint8Array<ArrayBuffer>[] = [];
    for (let at = offset; at < end; at += READ_BYTES) {
      const piece = new Uint8Array(await blob.slice(at, Math.min(end, at + READ_BYTES)).arrayBuffer());
      if (piece.length !== Math.min(end, at + READ_BYTES) - at) {
        throw new FileChangedError("the file changed while it was hashed");
      }
      whole.update(piece);
      pieces.push(piece);
    }
    const chunk = pieces.length === 1 ? (pieces[0] ?? new Uint8Array()) : concat(pieces, end - offset);
    chunks.push(await sha256Hex(chunk));
    offset = end;
    onProgress?.(offset);
  }
  return { sha256: whole.hex(), chunks, size };
}

function concat(pieces: Uint8Array<ArrayBuffer>[], length: number): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(length);
  let at = 0;
  for (const piece of pieces) {
    out.set(piece, at);
    at += piece.length;
  }
  return out;
}
