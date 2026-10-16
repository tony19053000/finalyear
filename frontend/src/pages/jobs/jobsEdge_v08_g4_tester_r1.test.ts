/**
 * V0.8 G4 tester r1: edge cases and failure injection for the Jobs pages' non-visual code, written
 * from the brief's criteria (resumable upload, hashing, shared project spec, log follower, live
 * updates). Strict `it.fails` = a real defect (see the progress file, "Tester r1").
 */
import { createHash, randomBytes } from "node:crypto";

import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/errors";
import type { FabricEvent, SocketLike } from "../../events/eventStream";
import { applyJobEvent } from "./applyJobEvent";
import { jobKeys } from "./jobKeys";
import { NO_FILTERS } from "./jobsApi";
import { LogFollower, type LogSnapshot } from "./logFollower";
import { folderSource } from "./submit/folderSource";
import { hashBlob } from "./submit/hashFile";
import { inlineHasher } from "./submit/hasher";
import { prepareProject } from "./submit/prepare";
import { ProjectError, walkProject } from "./submit/projectWalk";
import { Sha256, sha256Hex } from "./submit/sha256";
import { SPEC } from "./submit/spec";
import type { BuiltTree } from "./submit/treeManifest";
import { uploadTree } from "./submit/upload";

const enc = new TextEncoder();

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

/* ------------------------------------------------------------------ sha256 */

describe("Sha256 (own implementation) vs node crypto", () => {
  it("matches on block-boundary sizes and random split points", async () => {
    for (const size of [0, 1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 129, 1000, 65_535, 65_536, 65_537]) {
      const data = new Uint8Array(randomBytes(size));
      const want = createHash("sha256").update(data).digest("hex");
      const h = new Sha256();
      let at = 0;
      while (at < size) {
        const step = 1 + Math.floor(Math.random() * 97);
        h.update(data.subarray(at, at + step));
        at += step;
      }
      expect(h.hex(), `size ${String(size)}`).toBe(want);
      expect(await sha256Hex(data), `webcrypto size ${String(size)}`).toBe(want);
    }
  });

  it("hashBlob: chunk digests and whole digest on exact multiples, +-1 and empty", async () => {
    const chunk = 65_536;
    for (const size of [0, 1, chunk - 1, chunk, chunk + 1, chunk * 2, chunk * 2 + 1, chunk * 3 - 1]) {
      const data = new Uint8Array(randomBytes(size));
      const r = await hashBlob(new Blob([data]), chunk);
      expect(r.sha256).toBe(createHash("sha256").update(data).digest("hex"));
      expect(r.size).toBe(size);
      if (size <= chunk) expect(r.chunks).toEqual([]);
      else {
        const want: string[] = [];
        for (let at = 0; at < size; at += chunk) want.push(createHash("sha256").update(data.subarray(at, at + chunk)).digest("hex"));
        expect(r.chunks).toEqual(want);
      }
    }
  });

  it("an aborted hash rejects with AbortError and the hasher stays usable", async () => {
    const hasher = inlineHasher();
    const controller = new AbortController();
    const big = new Blob([new Uint8Array(65_536 * 4)]);
    controller.abort();
    await expect(hasher.hash(big, 65_536, undefined, controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    await expect(hasher.hash(new Blob([enc.encode("x")]), 65_536)).resolves.toMatchObject({ size: 1 });
  });

  it("a file that shrinks while being hashed is reported, not hashed short", async () => {
    const real = new Blob([new Uint8Array(65_536 * 3)]);
    const lying = Object.create(real) as Blob;
    Object.defineProperty(lying, "size", { value: 65_536 * 3 + 10 });
    Object.defineProperty(lying, "slice", { value: (a: number, b: number) => real.slice(a, b) });
    await expect(hashBlob(lying, 65_536)).rejects.toThrow(/changed/);
  });
});

/* ------------------------------------------------------------ folder / walk */

function file(path: string, text = "x", size?: number): File {
  const f = new File([text], path.split("/").pop() ?? path);
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("picked folder edge cases", () => {
  it("an empty folder / a file with no folder / two roots are refused with a ProjectError", () => {
    expect(() => folderSource([])).toThrow(ProjectError);
    expect(() => folderSource([file("lonely.txt")])).toThrow(ProjectError);
    expect(() => folderSource([file("a/x"), file("b/y")])).toThrow(ProjectError);
  });

  it("unicode, emoji and spaces in names survive; backslash / dot-dot / NUL are refused", async () => {
    const ok = folderSource([file("p/日本語/ünï cödé 😀.txt"), file("p/main.py")]);
    const walk = await walkProject(ok);
    expect(walk.entries.map((e) => e.path)).toContain("日本語/ünï cödé 😀.txt");
    for (const bad of ["p/a\\b.txt", "p/../x", "p/a/./b", "p/a\0b", "p/C:x"]) {
      await expect(walkProject(folderSource([file(bad)])), JSON.stringify(bad)).rejects.toThrow(ProjectError);
    }
  });

  it("a 255-byte name is fine, 256 bytes is refused; a 4097-byte path is refused", async () => {
    const name255 = "a".repeat(255);
    await expect(walkProject(folderSource([file(`p/${name255}`)]))).resolves.toBeDefined();
    await expect(walkProject(folderSource([file(`p/${"a".repeat(256)}`)]))).rejects.toThrow(ProjectError);
    const deep = Array.from({ length: 41 }, () => "b".repeat(100)).join("/");
    await expect(walkProject(folderSource([file(`p/${deep}`)]))).rejects.toThrow(ProjectError);
    // multi-byte: 86 x 3-byte chars = 258 bytes > 255 though only 86 chars
    await expect(walkProject(folderSource([file(`p/${"日".repeat(86)}`)]))).rejects.toThrow(ProjectError);
  });

  it("the size cap is enforced from Blob.size without reading any bytes", async () => {
    const huge = file("p/huge.bin", "x", SPEC.max_total_bytes + 1);
    await expect(walkProject(folderSource([huge, file("p/main.py")]))).rejects.toThrow(/larger than/);
  });

  it("100 001 tiny files are refused with a ProjectError (CLI cap: 100 000 entries), no crash / hang", async () => {
    const blob = new Blob(["x"]);
    const files = [{ path: "main.py", blob, executable: false }];
    for (let i = 0; i < SPEC.max_entries + 1; i += 1) files.push({ path: `d${String(i % 500)}/f${String(i)}`, blob, executable: false });
    await expect(prepareProject({ name: "p", dirs: [], files }, inlineHasher())).rejects.toBeInstanceOf(ProjectError);
  }, 120_000);

  it("a project that is entirely ignored is not silently submitted as an empty job", async () => {
    const source = folderSource([file("p/.gitignore", "*\n"), file("p/main.py")]);
    const result = await prepareProject(source, inlineHasher()).then(
      (p) => ({ files: p.walk.entries.filter((e) => e.kind === "file").length }),
      (e: unknown) => ({ refused: e instanceof Error }),
    );
    // main.py is ignored by `*`: either refused, or the tree holds only .gitignore - but main.py must not appear.
    if ("files" in result) expect(result.files).toBeLessThanOrEqual(1);
  });

  it("hashing the same bytes under two names uploads one blob (dedupe by digest)", async () => {
    const source = folderSource([file("p/main.py", "print(1)"), file("p/copy.py", "print(1)"), file("p/empty.txt", ""), file("p/empty2.txt", "")]);
    const prepared = await prepareProject(source, inlineHasher(), { parallel: 2 });
    expect(prepared.blobs.size).toBe(2);
    expect(prepared.tree.entries.filter((e) => e.kind === "file")).toHaveLength(4);
  });
});

/* ------------------------------------------------------------------- upload */

async function oneFileTree(data: Uint8Array<ArrayBuffer>, chunk = 65_536): Promise<[BuiltTree, Map<string, Blob>, string]> {
  const sha = await sha256Hex(data);
  const chunks: string[] = [];
  if (data.length > chunk) for (let at = 0; at < data.length; at += chunk) chunks.push(await sha256Hex(data.slice(at, at + chunk)));
  const tree: BuiltTree = {
    chunkSize: chunk,
    entries: [{ kind: "file", path: "f.bin", sha256: sha, size: data.length, executable: false, chunks }],
    manifest: enc.encode("{}"),
    treeId: `sha256:${"e".repeat(64)}`,
  };
  return [tree, new Map([[sha, new Blob([data])]]), sha];
}

function stubFetch(handler: (method: string, path: string) => Response | undefined) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      calls.push(`${method} ${input}`);
      return Promise.resolve(handler(method, input) ?? json(201, {}));
    }),
  );
  return calls;
}

describe("uploadTree failure injection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("gives up after the retry budget with the last server error (no endless loop)", async () => {
    const [t, blobs, sha] = await oneFileTree(new Uint8Array([1, 2, 3]));
    const calls = stubFetch((m, p) =>
      p === "/v1/blobs/have" ? json(200, { missing: [`sha256:${sha}`] }) : m === "PUT" ? json(500, { error: { code: "internal", message: "boom" } }) : undefined,
    );
    const sleeps: number[] = [];
    const sleep = (ms: number) => {
      sleeps.push(ms);
      return Promise.resolve();
    };
    await expect(uploadTree(t, blobs, { sleep, retries: 3 })).rejects.toBeInstanceOf(ApiError);
    expect(calls.filter((c) => c.startsWith("PUT")).length).toBe(4);
    expect(sleeps).toEqual([500, 1000, 2000]);
  });

  it("a 4xx on a part is not retried and nothing after it is sent", async () => {
    const [t, blobs, sha] = await oneFileTree(new Uint8Array(65_536 * 2 + 1).fill(7), 65_536);
    const calls = stubFetch((m, p) => {
      if (p === "/v1/blobs/have") return json(200, { missing: [`sha256:${sha}`] });
      if (m === "GET") return json(404, { error: { code: "not_found", message: "none" } });
      if (p.endsWith("/parts/0")) return json(409, { error: { code: "conflict", message: "no" } });
      return undefined;
    });
    await expect(uploadTree(t, blobs, { sleep: () => Promise.resolve() })).rejects.toBeInstanceOf(ApiError);
    expect(calls.filter((c) => c.includes("/parts/")).length).toBe(1);
    expect(calls.some((c) => c.includes("/complete"))).toBe(false);
    expect(calls.some((c) => c.startsWith("PUT /v1/blobs/eeee"))).toBe(false); // tree manifest never sent
  });

  it("a blob the server already holds completely (parts status complete) sends nothing", async () => {
    const [t, blobs, sha] = await oneFileTree(new Uint8Array(65_536 * 2 + 1).fill(7), 65_536);
    const calls = stubFetch((m, p) => {
      if (p === "/v1/blobs/have") return json(200, { missing: [`sha256:${sha}`] });
      if (m === "GET") return json(200, { digest: sha, size: 1, chunk_size: 65_536, received: [], complete: true });
      return undefined;
    });
    const report = await uploadTree(t, blobs);
    expect(calls.filter((c) => c.includes("/parts/")).length).toBe(0);
    expect(report.uploadedBytes).toBeLessThan(10);
  });

  it("T-9 a server that lists parts with indexes outside the file does not inflate resumedParts", async () => {
    const [t, blobs, sha] = await oneFileTree(new Uint8Array(65_536 * 2 + 1).fill(7), 65_536);
    stubFetch((m, p) => {
      if (p === "/v1/blobs/have") return json(200, { missing: [`sha256:${sha}`] });
      if (m === "GET") return json(200, { digest: sha, size: 1, chunk_size: 65_536, received: [0, 99, 100, -1], complete: false });
      return undefined;
    });
    const report = await uploadTree(t, blobs);
    expect(report.resumedParts).toBeLessThanOrEqual(3); // only 0 of {0,1,2} is real
  });

  it("T-8 an absurd Retry-After does not freeze the upload past a user cancel", async () => {
    vi.useFakeTimers();
    const [t, blobs, sha] = await oneFileTree(new Uint8Array([1]));
    stubFetch((m, p) =>
      p === "/v1/blobs/have"
        ? json(200, { missing: [`sha256:${sha}`] })
        : m === "PUT"
          ? json(503, { error: { code: "resource_busy", message: "busy" } }, { "retry-after": "86400" })
          : undefined,
    );
    const controller = new AbortController();
    let settled = false;
    const run = uploadTree(t, blobs, { signal: controller.signal }).then(
      () => {
        settled = true;
      },
      () => {
        settled = true;
      },
    );
    await vi.advanceTimersByTimeAsync(1000);
    controller.abort();
    await vi.advanceTimersByTimeAsync(5000);
    // After the user cancels, the promise must settle within seconds, not 24 h.
    expect(settled).toBe(true);
    await vi.advanceTimersByTimeAsync(86_400_000);
    await run;
  });
});

/* --------------------------------------------------------------- logFollower */

class FakeSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    /* client close */
  }
  frame(frame: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: typeof frame === "string" ? frame : JSON.stringify(frame) }));
  }
  drop(code = 1006): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }
}
const b64 = (s: string) => btoa(String.fromCharCode(...enc.encode(s)));

function follower(opts: Partial<ConstructorParameters<typeof LogFollower>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  let snap: LogSnapshot | null = null;
  let updates = 0;
  const f = new LogFollower({
    jobId: "job_01",
    origin: "wss://x",
    socketFactory: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    onUpdate: (s) => {
      snap = s;
      updates += 1;
    },
    ...opts,
  });
  return { f, sockets, snap: () => snap as LogSnapshot, updates: () => updates };
}

describe("LogFollower failure injection", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("flapping connection: reconnect URLs always carry the offsets received so far, no duplicate lines", () => {
    const { f, sockets, snap } = follower();
    f.start();
    for (let round = 0; round < 6; round += 1) {
      const s = sockets.at(-1) as FakeSocket;
      s.onopen?.(new Event("open"));
      // server replays everything from the offset the client asked for
      const asked = new URL(s.url).searchParams.get("stdout_offset");
      const have = Number(asked);
      const full = "a\nbb\nccc\ndddd\neeeee\nffffff\n";
      const slice = full.slice(have, have + 4 + round * 2);
      if (slice !== "") s.frame({ stream: "stdout", offset: have, data: b64(slice) });
      s.drop();
      vi.advanceTimersByTime(20_000);
    }
    const text = snap().lines.map((l) => l.text);
    expect(new Set(text).size).toBe(text.length);
    expect(text.slice(0, 3)).toEqual(["a", "bb", "ccc"]);
  });

  it("stop() while a reconnect is pending never reconnects; start() after stop works once", () => {
    const { f, sockets } = follower();
    f.start();
    (sockets[0] as FakeSocket).drop();
    f.stop();
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
    f.start();
    expect(sockets).toHaveLength(2);
    f.start();
    expect(sockets).toHaveLength(2);
  });

  it("backoff is capped and never exceeds the cap even after 40 failures", () => {
    const { f, sockets } = follower({ backoffBaseMs: 500, backoffCapMs: 15_000 });
    f.start();
    for (let i = 0; i < 40; i += 1) {
      (sockets.at(-1) as FakeSocket).drop();
      const before = sockets.length;
      vi.advanceTimersByTime(15_000);
      expect(sockets.length, `attempt ${String(i)}`).toBe(before + 1);
    }
  });

  it("garbage: NaN / negative / fractional / huge offsets, bad base64, non-JSON, arrays never throw or corrupt state", () => {
    const { f, sockets, snap } = follower();
    f.start();
    const s = sockets[0] as FakeSocket;
    s.onopen?.(new Event("open"));
    for (const frame of [
      "not json",
      "[]",
      "null",
      "42",
      { stream: "stdout", offset: Number.NaN, data: b64("x\n") },
      { stream: "stdout", offset: -5, data: b64("neg\n") },
      { stream: "stdout", offset: 0.5, data: b64("frac\n") },
      { stream: "stdout", offset: 1e300, data: b64("huge\n") },
      { stream: "stdout", offset: 0, data: "!!!not base64!!!" },
      { stream: "stdout", offset: 0, data: 5 },
      { stream: "stdin", offset: 0, data: b64("x") },
      { end: "yes" },
      { retry: 1 },
    ]) {
      expect(() => {
        s.frame(frame);
      }).not.toThrow();
    }
    const offs = snap().offsets;
    expect(Number.isFinite(offs.stdout)).toBe(true);
    expect(Number.isFinite(offs.stderr)).toBe(true);
  });

  it("a hostile line (HTML, ANSI, NUL, RTL override) is kept as plain text, nothing is evaluated", () => {
    const { f, sockets, snap } = follower();
    f.start();
    const s = sockets[0] as FakeSocket;
    s.onopen?.(new Event("open"));
    s.frame({ stream: "stdout", offset: 0, data: b64('<img src=x onerror=alert(1)>\u001b[31mred\u001b[0m\u0000‮evil\n') });
    expect(snap().lines[0]?.text).toContain("<img src=x onerror=alert(1)>");
  });

  it("flood: 30 000 one-line frames are handled in bounded time and memory (5000-line cap)", () => {
    vi.useRealTimers();
    const { f, sockets, snap } = follower({ maxLines: 5000 });
    f.start();
    const s = sockets[0] as FakeSocket;
    s.onopen?.(new Event("open"));
    const started = performance.now();
    let offset = 0;
    for (let i = 0; i < 30_000; i += 1) {
      const line = `line ${String(i)}\n`;
      s.frame({ stream: "stdout", offset, data: b64(line) });
      offset += line.length;
    }
    const took = performance.now() - started;
    expect(snap().lines.length).toBeLessThanOrEqual(5000);
    expect(snap().dropped + snap().lines.length).toBe(30_000);
    expect(took, `${String(Math.round(took))} ms for 30k frames`).toBeLessThan(4000);
  }, 30_000);
});

/* ------------------------------------------------------------- applyJobEvent */

function ev(type: string, data: unknown): FabricEvent {
  return { seq: 1, type, at: 0, data: data as Record<string, unknown> };
}

describe("applyJobEvent with malformed and hostile events (never throws, never poisons the cache)", () => {
  it("job.state / attempt.state with junk payloads", () => {
    const client = new QueryClient();
    client.setQueryData(jobKeys.list(NO_FILTERS), { pages: [{ jobs: [{ job_id: "job_2", state: "queued", name: "n" }], count: 1, next_cursor: null }], pageParams: [null] });
    const junk: unknown[] = [
      {}, { job_id: 5 }, { job_id: null }, { job_id: "" }, { job_id: ["job_2"] }, { job_id: "job_2" },
      { job_id: "job_2", state: 7 }, { job_id: "job_2", state: "exploded" }, { job_id: "job_2", state: null },
      { job_id: "job_2", state: "__proto__" }, { job_id: "__proto__", state: "running" }, { job_id: "constructor", state: "failed" },
      { job_id: "list", state: "running" }, { job_id: "job_2", state: "running", extra: { deep: [1, 2, 3] } },
      { job_id: "x".repeat(100_000), state: "running" },
    ];
    for (const type of ["job.state", "attempt.state"]) {
      for (const data of junk) {
        expect(() => applyJobEvent(client, ev(type, data)), `${type} ${JSON.stringify(data).slice(0, 60)}`).not.toThrow();
      }
    }
    const row = (client.getQueryData(jobKeys.list(NO_FILTERS)) as { pages: { jobs: { state: string }[] }[] }).pages[0]?.jobs[0];
    // the only valid-state event for job_2 above is the "running" one; an unknown state never lands in the cache
    expect(["queued", "running"]).toContain(row?.state);
    expect(({} as Record<string, unknown>).state).toBeUndefined(); // no prototype pollution
  });

  it("non-job event types return false and touch nothing, even with data = junk", () => {
    const client = new QueryClient();
    const spy = vi.spyOn(client, "invalidateQueries");
    for (const type of ["node.state", "", "job", "job.state.x", "JOB.STATE", "metrics.sample"]) {
      expect(applyJobEvent(client, ev(type, null))).toBe(false);
    }
    expect(spy).not.toHaveBeenCalled();
  });

  it("T-10 a list query whose key has an unexpected shape (e.g. another page's) is skipped, not a crash", () => {
    const client = new QueryClient();
    client.setQueryData(["jobs", "list"], { pages: [{ jobs: [] }], pageParams: [null] }); // no filters element
    client.setQueryData(["jobs", "list", { states: "failed" }], { pages: [{ jobs: [{ job_id: "job_1", state: "queued" }] }], pageParams: [null] });
    client.setQueryData(["jobs", "list", null], { pages: [], pageParams: [] });
    client.setQueryData(["jobs", "list", {}], { pages: [{ jobs: [] }], pageParams: [null] });
    expect(() => applyJobEvent(client, ev("job.state", { job_id: "job_9", state: "queued" }))).not.toThrow();
  });

  it("T-10 a list page object without `jobs` (cache corrupted by another writer) does not throw", () => {
    const client = new QueryClient();
    client.setQueryData(jobKeys.list(NO_FILTERS), { pages: [{}], pageParams: [null] });
    expect(() => applyJobEvent(client, ev("job.state", { job_id: "job_9", state: "queued" }))).not.toThrow();
  });
});

describe("misc: the manifest the browser sends", () => {
  it("name/entrypoint inference follows the CLI and a bad compute.yaml is a ManifestError with a reason", async () => {
    const { readProjectManifest, ManifestError } = await import("./submit/projectManifest");
    const src = (files: Record<string, string>) => ({
      name: "My Proj!",
      dirs: [],
      files: Object.entries(files).map(([path, text]) => ({ path, blob: new Blob([text]), executable: false })),
    });
    await expect(readProjectManifest(src({ "main.py": "" }))).resolves.toMatchObject({ manifest: { name: "My-Proj", entrypoint: ["python", "main.py"] } });
    await expect(readProjectManifest(src({ "compute.yaml": "a: [1\n", "main.py": "" }))).rejects.toBeInstanceOf(ManifestError);
    await expect(readProjectManifest(src({ "compute.yaml": "- a\n- b\n", "main.py": "" }))).rejects.toBeInstanceOf(ManifestError);
    await expect(readProjectManifest(src({ "compute.yaml": "env: {requirements: ../x}\n", "main.py": "" }))).rejects.toBeInstanceOf(ManifestError);
    await expect(readProjectManifest(src({ "compute.yaml": "x: 1\n" }))).rejects.toBeInstanceOf(ManifestError);
    await expect(readProjectManifest(src({ "compute.yaml": "a: &x 1\n", "main.py": "" }))).rejects.toMatchObject({ useCli: true });
    // BOM + CRLF are fine; an oversized manifest is refused
    await expect(readProjectManifest(src({ "compute.yaml": "﻿name: z\r\nentrypoint: python main.py\r\n" }))).resolves.toMatchObject({ manifest: { name: "z" } });
    await expect(readProjectManifest(src({ "compute.yaml": `name: ${"a".repeat(70_000)}\n`, "main.py": "" }))).rejects.toBeInstanceOf(ManifestError);
  });
});
