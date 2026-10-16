/** Messages between the page and the hashing worker (hashWorker.ts). */
import type { HashResult } from "./hashFile";

export type HashRequest =
  | { type: "hash"; id: number; blob: Blob; chunkSize: number }
  | { type: "cancel"; id: number };

export type HashResponse =
  | { type: "progress"; id: number; bytes: number }
  | { type: "done"; id: number; result: HashResult }
  | { type: "error"; id: number; name: string; message: string };
