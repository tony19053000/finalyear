/**
 * A small .zip reader for "submit a zip" (D177), no dependency: the central
 * directory, stored (0) and deflated (8) entries - deflate through the
 * browser's own `DecompressionStream("deflate-raw")`. Every entry's CRC-32
 * and size are checked. Refused (with a reason): encryption, zip64, other
 * methods, symlinks, names that are not UTF-8, archives over `maxBytes`
 * unpacked. A folder zip whose entries all sit under one top folder is
 * unwrapped (that folder's name becomes the default job name). macOS Finder's
 * `__MACOSX/` resource-fork folder is archive junk, never project content: its
 * entries are skipped (so a Finder zip unwraps too). Inflating stops as soon
 * as the output passes the size the directory declares (a lying entry is
 * refused without materialising its real size).
 */
import { ProjectError, type ProjectSource, type SourceFile } from "./projectWalk";

/** Unpacked bytes held in memory at most (larger: pick the folder instead). */
export const MAX_ZIP_UNPACKED_BYTES = 2 * 1024 ** 3;

const EOCD = 0x06054b50;
const CENTRAL = 0x02014b50;
const LOCAL = 0x04034b50;

let crcTable: Uint32Array | null = null;

export function crc32(data: Uint8Array, seed = 0): number {
  if (crcTable === null) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let crc = (seed ^ 0xffffffff) >>> 0;
  for (const byte of data) crc = ((crcTable[(crc ^ byte) & 0xff] ?? 0) ^ (crc >>> 8)) >>> 0;
  return (crc ^ 0xffffffff) >>> 0;
}

async function bytes(blob: Blob, start: number, end: number): Promise<DataView> {
  return new DataView(await blob.slice(start, end).arrayBuffer());
}

interface CentralEntry {
  name: string;
  method: number;
  crc: number;
  compressedSize: number;
  size: number;
  localOffset: number;
  mode: number;
  madeByUnix: boolean;
}

const utf8 = new TextDecoder("utf-8", { fatal: true });

async function centralDirectory(zip: Blob): Promise<CentralEntry[]> {
  const tailStart = Math.max(0, zip.size - (0xffff + 22));
  const tail = await bytes(zip, tailStart, zip.size);
  let eocd = -1;
  for (let i = tail.byteLength - 22; i >= 0; i -= 1) {
    if (tail.getUint32(i, true) === EOCD) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new ProjectError("this file is not a zip archive");
  const count = tail.getUint16(eocd + 10, true);
  const cdSize = tail.getUint32(eocd + 12, true);
  const cdOffset = tail.getUint32(eocd + 16, true);
  if (count === 0xffff || cdSize === 0xffffffff || cdOffset === 0xffffffff) {
    throw new ProjectError("zip64 archives are not supported here - pick the folder instead");
  }
  const cd = await bytes(zip, cdOffset, cdOffset + cdSize);
  const raw = new Uint8Array(cd.buffer);
  const entries: CentralEntry[] = [];
  let at = 0;
  for (let n = 0; n < count; n += 1) {
    if (at + 46 > cd.byteLength || cd.getUint32(at, true) !== CENTRAL) throw new ProjectError("the zip's directory is damaged");
    const madeBy = cd.getUint16(at + 4, true);
    const flags = cd.getUint16(at + 8, true);
    const method = cd.getUint16(at + 10, true);
    const crc = cd.getUint32(at + 16, true);
    const compressedSize = cd.getUint32(at + 20, true);
    const size = cd.getUint32(at + 24, true);
    const nameLength = cd.getUint16(at + 28, true);
    const extraLength = cd.getUint16(at + 30, true);
    const commentLength = cd.getUint16(at + 32, true);
    const external = cd.getUint32(at + 38, true);
    const localOffset = cd.getUint32(at + 42, true);
    let name: string;
    try {
      name = utf8.decode(raw.subarray(at + 46, at + 46 + nameLength));
    } catch {
      throw new ProjectError("a name in the zip is not UTF-8 - pick the folder instead");
    }
    if (flags & 1) throw new ProjectError("encrypted zip entries are not supported", name);
    if (compressedSize === 0xffffffff || size === 0xffffffff || localOffset === 0xffffffff) {
      throw new ProjectError("zip64 archives are not supported here - pick the folder instead");
    }
    entries.push({
      name,
      method,
      crc,
      compressedSize,
      size,
      localOffset,
      mode: external >>> 16,
      madeByUnix: madeBy >>> 8 === 3,
    });
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

/** Compressed bytes fed per step: one step inflates to at most ~1032x this. */
const INFLATE_STEP = 16 * 1024;

/**
 * Inflate `data`, or null as soon as the output exceeds `limit` bytes (T-12):
 * the input is fed in small steps and the output read chunk by chunk, so a
 * lying size costs at most `limit` + one step's output, never the real size.
 */
async function inflate(data: Blob, limit: number): Promise<Uint8Array | null> {
  // From the bytes (not Blob.stream(): not every Blob implementation has it).
  const packed = new Uint8Array(await data.arrayBuffer());
  let fed = 0;
  const input = new ReadableStream<Uint8Array<ArrayBuffer>>({
    pull(controller) {
      if (fed >= packed.length) {
        controller.close();
        return;
      }
      controller.enqueue(packed.slice(fed, fed + INFLATE_STEP));
      fed += INFLATE_STEP;
    },
  });
  const reader = input.pipeThrough(new DecompressionStream("deflate-raw")).getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    parts.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

async function entryData(zip: Blob, entry: CentralEntry): Promise<Blob> {
  const local = await bytes(zip, entry.localOffset, entry.localOffset + 30);
  if (local.byteLength < 30 || local.getUint32(0, true) !== LOCAL) throw new ProjectError("the zip is damaged", entry.name);
  const start = entry.localOffset + 30 + local.getUint16(26, true) + local.getUint16(28, true);
  const packed = zip.slice(start, start + entry.compressedSize);
  let data: Uint8Array | null;
  if (entry.method === 0) {
    data = entry.compressedSize === entry.size ? new Uint8Array(await packed.arrayBuffer()) : null;
  } else if (entry.method === 8) {
    try {
      data = await inflate(packed, entry.size);
    } catch {
      throw new ProjectError("a zip entry cannot be decompressed", entry.name);
    }
  } else {
    throw new ProjectError(`zip compression method ${entry.method} is not supported (use deflate or store)`, entry.name);
  }
  if (data === null || data.length !== entry.size || crc32(data) !== entry.crc) throw new ProjectError("a zip entry is damaged (size or CRC)", entry.name);
  return new Blob([data as Uint8Array<ArrayBuffer>]);
}

const S_IFMT = 0o170000;
const S_IFLNK = 0o120000;

/** Read a zip into a project source (see the module comment). */
const MACOS_JUNK = "__MACOSX";

export async function readZip(zip: File, maxBytes = MAX_ZIP_UNPACKED_BYTES): Promise<ProjectSource> {
  const entries = (await centralDirectory(zip)).filter(
    (e) => e.name.replace(/^\.\//, "").split("/")[0] !== MACOS_JUNK,
  );
  const declared = entries.reduce((sum, e) => sum + e.size, 0);
  if (declared > maxBytes) {
    throw new ProjectError(`the zip unpacks to more than ${maxBytes} bytes - pick the folder instead (or use the CLI)`);
  }
  const names = entries.map((e) => e.name.replace(/^\.\//, ""));
  const tops = new Set(names.map((n) => n.split("/")[0] ?? ""));
  const top = tops.size === 1 && names.every((n) => n.includes("/") || n.endsWith("/")) ? [...tops][0] ?? "" : "";
  const strip = (name: string) => (top !== "" ? name.slice(top.length + 1) : name);
  const files: SourceFile[] = [];
  const dirs: string[] = [];
  for (const [i, entry] of entries.entries()) {
    const rel = strip(names[i] ?? "");
    if (rel === "" || rel === "/") continue;
    if (entry.madeByUnix && (entry.mode & S_IFMT) === S_IFLNK) {
      throw new ProjectError("symlinks inside a zip are not supported - pick the folder or use the CLI", rel);
    }
    if (rel.endsWith("/")) {
      dirs.push(rel.slice(0, -1));
      continue;
    }
    files.push({
      path: rel,
      blob: await entryData(zip, entry),
      executable: entry.madeByUnix && (entry.mode & 0o100) !== 0,
    });
  }
  const fallback = zip.name.replace(/\.zip$/i, "") || "job";
  return { name: top !== "" ? top : fallback, files, dirs };
}
