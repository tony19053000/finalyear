/**
 * Tester r2: Retry-After edge values + cancel mid-wait (T-8), zip T-11 / T-12 with hand-built
 * hostile archives, and a walk pathspec-order battery (last-to-first, negations, nested files).
 */
import { deflateRawSync, crc32 as nodeCrc32 } from "node:zlib";

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../api/errors";
import { inlineHasher } from "./hasher";
import { ManifestYamlError, parseManifestYaml } from "./manifestYaml";
import { prepareProject } from "./prepare";
import { ProjectError } from "./projectWalk";
import { sha256Hex } from "./sha256";
import type { BuiltTree } from "./treeManifest";
import { MAX_RETRY_WAIT_MS, UploadError, uploadTree } from "./upload";
import { readZip } from "./zip";

/* ------------------------------------------------------------- Retry-After */

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

async function oneFile(): Promise<[BuiltTree, Map<string, Blob>, string]> {
  const data = new Uint8Array([7, 7, 7]);
  const sha = await sha256Hex(data);
  const t: BuiltTree = {
    chunkSize: 65_536,
    entries: [{ kind: "file", path: "a.txt", sha256: sha, size: 3, executable: false, chunks: [] }],
    manifest: new TextEncoder().encode("{}"),
    treeId: `sha256:${"f".repeat(64)}`,
  };
  return [t, new Map([[sha, new Blob([data])]]), sha];
}

/** First PUT answers `first` (a Response factory), later PUTs 201. Returns call log. */
function stub(sha: string, first: () => Response, times = 1) {
  let puts = 0;
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const m = init.method ?? "GET";
      calls.push(`${m} ${input}`);
      if (input === "/v1/blobs/have") return Promise.resolve(json(200, { missing: [`sha256:${sha}`] }));
      if (m === "PUT") {
        puts += 1;
        return Promise.resolve(puts <= times ? first() : json(201, {}));
      }
      return Promise.resolve(json(201, {}));
    }),
  );
  return { calls, puts: () => puts };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const busy = (retryAfter: string | null, status = 503) => () =>
  json(status, { error: { code: "resource_busy", message: "busy" } }, retryAfter === null ? {} : { "retry-after": retryAfter });

describe("Retry-After edge values (T-8, D196: capped at 60 s, abortable)", () => {
  const waits = async (header: string | null, status = 503) => {
    const [t, blobs, sha] = await oneFile();
    const s = stub(sha, busy(header, status));
    const slept: number[] = [];
    const run = uploadTree(t, blobs, { sleep: (ms) => (slept.push(ms), Promise.resolve()) });
    return { slept, s, run };
  };

  it.each([
    ["0", 500],
    ["", 500], // Number("") === 0
    ["-5", 500], // negative: ignored, default backoff
    ["-0", 500],
    ["Wed, 21 Oct 2026 07:28:00 GMT", 500], // HTTP-date form: not parsed, default backoff (no crash)
    ["abc", 500],
    ["NaN", 500],
    ["Infinity", 500], // not finite -> ignored
    ["1", 1000],
    ["2.5", 2500],
    ["59.999", 59_999],
    ["60", 60_000], // exactly the ceiling is honoured
    [" 7 ", 7000],
  ])("Retry-After %j -> one retry after %d ms", async (header, ms) => {
    const { slept, run } = await waits(header);
    await run;
    expect(slept).toEqual([ms]);
  });

  it.each(["60.001", "61", "3600", "1e2", "86400", "1e309"])("Retry-After %j fails at once with a clear UploadError, no wait", async (header) => {
    const { slept, run, s } = await waits(header);
    // 1e309 -> Number() = Infinity -> parseRetryAfter treats as absent, so it retries with backoff instead.
    if (header === "1e309") {
      await run;
      expect(slept).toEqual([500]);
      return;
    }
    await expect(run).rejects.toBeInstanceOf(UploadError);
    await expect(run).rejects.toThrow(/more than 60/);
    expect(slept).toEqual([]);
    expect(s.puts()).toBe(1); // no second request
  });

  it("429 honours / caps Retry-After the same way", async () => {
    const a = await waits("30", 429);
    await a.run;
    expect(a.slept).toEqual([30_000]);
    const b = await waits("120", 429);
    await expect(b.run).rejects.toBeInstanceOf(UploadError);
  });

  it("the cap constant is 60 s", () => {
    expect(MAX_RETRY_WAIT_MS).toBe(60_000);
  });

  it("the LAST attempt rethrows the server's ApiError (not an UploadError about waiting)", async () => {
    const [t, blobs, sha] = await oneFile();
    stub(sha, busy("3600"), 99);
    await expect(uploadTree(t, blobs, { retries: 0, sleep: () => Promise.resolve() })).rejects.toBeInstanceOf(ApiError);
  });

  it("exponential backoff never exceeds the 60 s ceiling even with many retries", async () => {
    const [t, blobs, sha] = await oneFile();
    stub(sha, busy(null), 30);
    const slept: number[] = [];
    await uploadTree(t, blobs, { retries: 40, sleep: (ms) => (slept.push(ms), Promise.resolve()) });
    expect(slept).toHaveLength(30);
    expect(Math.max(...slept)).toBeLessThanOrEqual(60_000);
    expect(slept[slept.length - 1]).toBe(60_000);
  });

  it("cancel mid-wait settles with AbortError at once, and the late-resolving sleep causes NO further request", async () => {
    const [t, blobs, sha] = await oneFile();
    const s = stub(sha, busy("30"), 99);
    let release: () => void = () => undefined;
    const c = new AbortController();
    const run = uploadTree(t, blobs, {
      signal: c.signal,
      sleep: () => new Promise<void>((r) => (release = r)),
    });
    const settled = run.then(
      () => "ok",
      (e: unknown) => (e instanceof DOMException ? e.name : String(e)),
    );
    await vi.waitFor(() => { expect(s.puts()).toBe(1); });
    await new Promise((r) => setTimeout(r, 20));
    c.abort();
    expect(await settled).toBe("AbortError");
    const before = s.puts();
    release(); // the sleeper wakes after the cancel
    await new Promise((r) => setTimeout(r, 30));
    expect(s.puts()).toBe(before);
  });

  it("cancel with REAL timers during a 60 s wait returns within 100 ms", async () => {
    const [t, blobs, sha] = await oneFile();
    const s = stub(sha, busy("60"), 99);
    const c = new AbortController();
    const run = uploadTree(t, blobs, { signal: c.signal }).catch((e: unknown) => e);
    await vi.waitFor(() => { expect(s.puts()).toBe(1); });
    const t0 = performance.now();
    c.abort();
    const err = await run;
    expect(performance.now() - t0).toBeLessThan(100);
    expect((err as Error).name).toBe("AbortError");
  });

  it("abort BEFORE the wait starts (signal already aborted) never sleeps or sends again", async () => {
    const [t, blobs, sha] = await oneFile();
    const c = new AbortController();
    let sleeps = 0;
    stub(sha, () => {
      c.abort(); // the cancel lands while the failing response is processed
      return busy("5")();
    }, 99);
    const err = await uploadTree(t, blobs, { signal: c.signal, sleep: () => (sleeps += 1, new Promise<void>(() => undefined)) }).catch((e: unknown) => e);
    expect((err as Error).name).toBe("AbortError");
    expect(sleeps).toBeLessThanOrEqual(1);
  });

  it("several lanes waiting on Retry-After all stop on cancel", async () => {
    const data = [new Uint8Array([1]), new Uint8Array([2]), new Uint8Array([3]), new Uint8Array([4])];
    const shas = await Promise.all(data.map((d) => sha256Hex(d)));
    const t: BuiltTree = {
      chunkSize: 65_536,
      entries: shas.map((sha, i) => ({ kind: "file", path: `f${String(i)}`, sha256: sha, size: 1, executable: false, chunks: [] })),
      manifest: new TextEncoder().encode("{}"),
      treeId: `sha256:${"f".repeat(64)}`,
    };
    const blobs = new Map(shas.map((s, i) => [s, new Blob([data[i] as Uint8Array<ArrayBuffer>])]));
    let puts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, init: RequestInit = {}) => {
        if (input === "/v1/blobs/have") return Promise.resolve(json(200, { missing: shas.map((s) => `sha256:${s}`) }));
        if (init.method === "PUT") {
          puts += 1;
          return Promise.resolve(busy("45")());
        }
        return Promise.resolve(json(201, {}));
      }),
    );
    const c = new AbortController();
    const run = uploadTree(t, blobs, { signal: c.signal, concurrency: 4 }).catch((e: unknown) => e);
    await vi.waitFor(() => { expect(puts).toBe(4); });
    const t0 = performance.now();
    c.abort();
    const err = await run;
    expect(performance.now() - t0).toBeLessThan(200);
    expect(["AbortError", "UploadError"]).toContain((err as Error).name);
    expect(puts).toBe(4);
  });
});

/* -------------------------------------------------------------------- zip */

interface ZE {
  name: string;
  data: Uint8Array;
  method?: 0 | 8;
  /** override what the central directory declares */
  declaredSize?: number;
  declaredCompressed?: number;
  packed?: Uint8Array;
  unixMode?: number;
}

function makeZip(entries: ZE[], filename = "p.zip"): File {
  const chunks: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  const u16 = (n: number) => [n & 0xff, (n >>> 8) & 0xff];
  const u32 = (n: number) => [n & 0xff, (n >>> 8) & 0xff, (n >>> 16) & 0xff, (n >>> 24) & 0xff];
  for (const e of entries) {
    const name = new TextEncoder().encode(e.name);
    const method = e.method ?? 8;
    const packed = e.packed ?? (method === 8 ? deflateRawSync(e.data) : e.data);
    const crc = nodeCrc32(e.data);
    const size = e.declaredSize ?? e.data.length;
    const csize = e.declaredCompressed ?? packed.length;
    const local = Uint8Array.from([...u32(0x04034b50), ...u16(20), ...u16(0x0800), ...u16(method), ...u16(0), ...u16(0), ...u32(crc), ...u32(csize), ...u32(size), ...u16(name.length), ...u16(0), ...name]);
    chunks.push(local, packed);
    const mode = e.unixMode ?? (e.name.endsWith("/") ? 0o40755 : 0o100644);
    central.push(
      Uint8Array.from([...u32(0x02014b50), ...u16((3 << 8) | 20), ...u16(20), ...u16(0x0800), ...u16(method), ...u16(0), ...u16(0), ...u32(crc), ...u32(csize), ...u32(size), ...u16(name.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32((mode << 16) >>> 0), ...u32(offset), ...name]),
    );
    offset += local.length + packed.length;
  }
  const cdSize = central.reduce((s, c) => s + c.length, 0);
  const eocd = Uint8Array.from([...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(entries.length), ...u16(entries.length), ...u32(cdSize), ...u32(offset), ...u16(0)]);
  return new File([...chunks, ...central, eocd] as BlobPart[], filename);
}
const b = (s: string) => new TextEncoder().encode(s);

describe("zip T-11: __MACOSX junk", () => {
  it("a Finder zip unwraps to the project folder; junk (even corrupt / traversal / unsupported) is never read", async () => {
    const z = makeZip([
      { name: "proj/", data: b("") , method: 0 },
      { name: "proj/main.py", data: b("print(1)") },
      { name: "proj/sub/a.txt", data: b("a") },
      { name: "__MACOSX/", data: b(""), method: 0 },
      { name: "__MACOSX/proj/._main.py", data: b("junk"), packed: b("not deflate at all!!") },
      { name: "__MACOSX/../../evil", data: b("x"), method: 0 },
      { name: "__MACOSX/proj/link", data: b("x"), method: 0, unixMode: 0o120777 },
      { name: "./__MACOSX/q", data: b("x") },
    ]);
    const src = await readZip(z);
    expect(src.name).toBe("proj");
    expect(src.files.map((f) => f.path).sort()).toEqual(["main.py", "sub/a.txt"]);
    for (const f of src.files) expect(f.path).not.toContain("MACOSX");
  });

  it("only-junk + root files: the root files stay, nothing crashes", async () => {
    const src = await readZip(makeZip([{ name: "main.py", data: b("1") }, { name: "__MACOSX/._main.py", data: b("j") }], "flat.zip"));
    expect(src.files.map((f) => f.path)).toEqual(["main.py"]);
    expect(src.name).toBe("flat");
  });

  it("a zip that is ONLY junk gives an empty project (refused later by the walk), not a crash", async () => {
    const src = await readZip(makeZip([{ name: "__MACOSX/._x", data: b("j") }], "junk.zip"));
    expect(src.files).toEqual([]);
    const prepared = await prepareProject(src, inlineHasher()).catch((e: unknown) => e);
    expect((prepared as Error).message).toMatch(/entrypoint/); // a clean refusal, not a TypeError
  });

  it("only the TOP-level __MACOSX is junk: lookalikes and nested folders are real content", async () => {
    const src = await readZip(
      makeZip([
        { name: "proj/main.py", data: b("1") },
        { name: "proj/__MACOSX/x.py", data: b("2") },
        { name: "proj/__macosx/y.py", data: b("3") },
        { name: "proj/__MACOSX_notes.txt", data: b("4") },
      ]),
    );
    expect(src.files.map((f) => f.path).sort()).toEqual(["__MACOSX/x.py", "__MACOSX_notes.txt", "__macosx/y.py", "main.py"]);
    const lookalike = await readZip(makeZip([{ name: "__MACOSXX/a.py", data: b("1") }, { name: "__MACOSXX/b.py", data: b("2") }]));
    expect(lookalike.name).toBe("__MACOSXX");
  });

  it("a real project folder is not unwrapped wrongly when junk is the only sibling AND files sit at root", async () => {
    const src = await readZip(makeZip([{ name: "a/x.py", data: b("1") }, { name: "root.py", data: b("2") }, { name: "__MACOSX/a/._x.py", data: b("j") }]));
    expect(src.files.map((f) => f.path).sort()).toEqual(["a/x.py", "root.py"]);
  });
});

describe("zip T-12: bounded inflate, lying headers", () => {
  it("a 256 MiB-of-zeros entry declared as 10 bytes is refused fast without materialising it", async () => {
    const zeros = new Uint8Array(256 * 1024 * 1024);
    const z = makeZip([{ name: "p/bomb.bin", data: zeros.subarray(0, 10), declaredSize: 10, packed: deflateRawSync(zeros) }, { name: "p/m.py", data: b("1") }]);
    expect(z.size).toBeLessThan(2 * 1024 * 1024);
    let peak = 0;
    const base = process.memoryUsage().arrayBuffers;
    const timer = setInterval(() => (peak = Math.max(peak, process.memoryUsage().arrayBuffers - base)), 1);
    const t0 = performance.now();
    await expect(readZip(z)).rejects.toBeInstanceOf(ProjectError);
    clearInterval(timer);
    expect(performance.now() - t0).toBeLessThan(10_000);
    expect(peak, `peak extra arraybuffers ${String(Math.round(peak / 1e6))} MB`).toBeLessThan(96 * 1024 * 1024);
  }, 60_000);

  it("declared size one too small / one too large / zero are all 'damaged'", async () => {
    for (const declared of [9, 11, 0, 1]) {
      const z = makeZip([{ name: "p/a.bin", data: new Uint8Array(10).fill(3), declaredSize: declared }]);
      await expect(readZip(z), `declared ${String(declared)}`).rejects.toThrow(/damaged|cannot be decompressed/);
    }
  });

  it("a truncated deflate stream and a flipped bit are ProjectErrors, never a raw TypeError", async () => {
    const data = b("hello hello hello hello hello hello ".repeat(100));
    const good = deflateRawSync(data);
    const flipped = Uint8Array.from(good);
    flipped[Math.floor(good.length / 2)] = (flipped[Math.floor(good.length / 2)] ?? 0) ^ 0x55;
    for (const packed of [good.subarray(0, Math.floor(good.length / 2)), flipped, new Uint8Array(0)]) {
      const z = makeZip([{ name: "p/a.txt", data, packed, declaredCompressed: packed.length }]);
      const err = await readZip(z).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(ProjectError);
    }
  });

  it("stored entry whose compressed size differs from its size is refused; a valid empty deflate entry works", async () => {
    await expect(readZip(makeZip([{ name: "p/a", data: b("abc"), method: 0, declaredSize: 5 }]))).rejects.toBeInstanceOf(ProjectError);
    const ok = await readZip(makeZip([{ name: "p/e", data: new Uint8Array(0) }, { name: "p/m", data: b("1") }]));
    expect(ok.files.find((f) => f.path === "e")?.blob.size).toBe(0);
  });

  it("declared sizes summing past the cap are refused before any inflate (4 x ~4 GiB lies)", async () => {
    const lie = (n: string): ZE => ({ name: `p/${n}`, data: b("x"), declaredSize: 0xfffffffe });
    await expect(readZip(makeZip([lie("a"), lie("b"), lie("c")]))).rejects.toThrow(/more than/);
  });

  it("honest large-ish entry (3 MiB random-ish) round-trips byte for byte", async () => {
    const data = new Uint8Array(3 * 1024 * 1024);
    for (let i = 0; i < data.length; i += 1) data[i] = (i * 2654435761) >>> 24;
    const src = await readZip(makeZip([{ name: "p/big.bin", data }, { name: "p/m", data: b("1") }]));
    const got = new Uint8Array(await (src.files.find((f) => f.path === "big.bin")?.blob.arrayBuffer() ?? Promise.resolve(new ArrayBuffer(0))));
    expect(got.length).toBe(data.length);
    expect(nodeCrc32(got)).toBe(nodeCrc32(data));
  }, 60_000);

  it("a highly compressible but HONEST 40 MiB entry still reads", async () => {
    const data = new Uint8Array(40 * 1024 * 1024);
    const src = await readZip(makeZip([{ name: "p/z.bin", data }, { name: "p/m", data: b("1") }]));
    expect(src.files.find((f) => f.path === "z.bin")?.blob.size).toBe(data.length);
  }, 60_000);
});


/* ------------------------------------------------- YAML: hostile shapes */

describe("compute.yaml reader on hostile shapes (refuse or equal PyYAML; never crash or hang)", () => {
  const attempt = (text: string) => {
    const t0 = performance.now();
    let outcome: string;
    try {
      parseManifestYaml(text);
      outcome = "ok";
    } catch (error) {
      outcome = error instanceof ManifestYamlError ? "refused" : `CRASH ${String(error).slice(0, 80)}`;
    }
    return { outcome, ms: performance.now() - t0 };
  };

  it.each([
    ["deep flow nesting 20000", "a: " + "[".repeat(20_000) + "]".repeat(20_000) + "\n"],
    ["deep block nesting 3000", Array.from({ length: 3000 }, (_, i) => " ".repeat(i) + "k:").join("\n") + " 1\n"],
    ["deep seq nesting 3000", Array.from({ length: 3000 }, (_, i) => " ".repeat(i) + "-").join("\n") + " 1\n"],
    ["1 MiB single scalar", "a: " + "x".repeat(1 << 20) + "\n"],
    ["100k-entry flow list", "a: [" + "1,".repeat(100_000) + "1]\n"],
    ["50k keys", Array.from({ length: 50_000 }, (_, i) => `k${String(i)}: ${String(i)}`).join("\n") + "\n"],
    ["BOM first", "﻿a: 1\n"],
    ["CRLF only file", "a: 1\r\nb: [1,\r\n 2]\r\n"],
    ["lone CR line endings", "a: 1\rb: 2\r"],
    ["python tag", "a: !!python/object/apply:os.system ['id']\n"],
    ["merge key", "a: &x {b: 1}\nc: {<<: *x}\n"],
    ["multi-document", "a: 1\n---\nb: 2\n"],
    ["NUL byte", "a: 1\u0000\n"],
    ["quadratic quotes", "a: " + "'".repeat(50_000) + "\n"],
    ["quadratic backslashes", 'a: "' + "\\".repeat(50_000) + '"\n'],
  ])("%s", (_name, text) => {
    const r = attempt(text);
    expect(r.outcome.startsWith("CRASH"), r.outcome).toBe(false);
    expect(r.ms).toBeLessThan(5000);
  });

  it("agrees with PyYAML on the verdict for tag / merge / multi-doc / lone-CR (refuse or equal)", () => {
    // PyYAML: python tag -> error; merge with alias -> error (no aliases allowed); multi-doc -> error;
    // lone CR is a line break -> {a: 1, b: 2}; BOM first -> {a: 1}.
    expect(attempt("a: !!python/object/apply:os.system ['id']\n").outcome).toBe("refused");
    expect(attempt("a: &x {b: 1}\nc: {<<: *x}\n").outcome).toBe("refused");
    expect(attempt("a: 1\n---\nb: 2\n").outcome).toBe("refused");
    expect(attempt("a: 1\u0000\n").outcome).toBe("refused");
    expect(parseManifestYaml("﻿a: 1\n")).toEqual({ a: 1 }); // PyYAML strips a leading BOM
  });
});
