import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../api/errors";
import { sha256Hex } from "./sha256";
import type { BuiltTree } from "./treeManifest";
import { MAX_RETRY_WAIT_MS, UploadError, uploadTree } from "./upload";

const CHUNK = 65_536;

interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

function fakeController(handler: (call: Call) => Response | undefined) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const call = { method: init.method ?? "GET", path: input, headers: new Headers(init.headers), body: init.body };
      calls.push(call);
      return Promise.resolve(handler(call) ?? json(201, {}));
    }),
  );
  return calls;
}

async function tree(files: { path: string; data: Uint8Array<ArrayBuffer>; chunks?: boolean }[]): Promise<[BuiltTree, Map<string, Blob>]> {
  const blobs = new Map<string, Blob>();
  const entries: BuiltTree["entries"] = [];
  for (const f of files) {
    const sha = await sha256Hex(f.data);
    const chunks: string[] = [];
    if (f.chunks === true) {
      for (let at = 0; at < f.data.length; at += CHUNK) chunks.push(await sha256Hex(f.data.slice(at, at + CHUNK)));
    }
    entries.push({ kind: "file", path: f.path, sha256: sha, size: f.data.length, executable: false, chunks });
    blobs.set(sha, new Blob([f.data]));
  }
  const manifest = new TextEncoder().encode("{}");
  return [{ chunkSize: CHUNK, entries, manifest, treeId: `sha256:${"f".repeat(64)}` }, blobs];
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "mf_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("uploadTree (resumable CAS upload, as cli/transfer.py)", () => {
  it("sends only what the controller lacks: whole blobs, then parts it has not got, then the manifest", async () => {
    document.cookie = "mf_csrf=tok123";
    const small = new TextEncoder().encode("small");
    const have = new TextEncoder().encode("already there");
    const big = new Uint8Array(CHUNK * 2 + 5).fill(9);
    const [t, blobs] = await tree([
      { path: "small.txt", data: small },
      { path: "have.txt", data: have },
      { path: "big.bin", data: big, chunks: true },
    ]);
    const [smallSha, haveSha, bigSha] = t.entries.map((e) => (e.kind === "file" ? e.sha256 : ""));
    const calls = fakeController((c) => {
      if (c.path === "/v1/blobs/have") return json(200, { missing: [`sha256:${smallSha ?? ""}`, `sha256:${bigSha ?? ""}`, t.treeId] });
      if (c.method === "GET" && c.path.endsWith("/parts")) return json(200, { digest: "x", size: big.length, chunk_size: CHUNK, received: [0], complete: false });
      return undefined;
    });
    const progress: number[] = [];
    const report = await uploadTree(t, blobs, { concurrency: 1, onProgress: (p) => progress.push(p.sentBytes) });

    const sequence = calls.map((c) => `${c.method} ${c.path}`);
    expect(sequence).toEqual([
      "POST /v1/blobs/have",
      `PUT /v1/blobs/${smallSha ?? ""}`,
      `GET /v1/blobs/${bigSha ?? ""}/parts`,
      `PUT /v1/blobs/${bigSha ?? ""}/parts/1`,
      `PUT /v1/blobs/${bigSha ?? ""}/parts/2`,
      `POST /v1/blobs/${bigSha ?? ""}/complete`,
      `PUT /v1/blobs/${"f".repeat(64)}`,
    ]);
    expect(JSON.parse(String(calls[0]?.body))).toEqual({ digests: t.entries.map((e) => `sha256:${e.kind === "file" ? e.sha256 : ""}`).concat(t.treeId) });
    const part = calls[3];
    expect(part?.headers.get("X-MeshForge-Blob-Size")).toBe(String(big.length));
    expect(part?.headers.get("X-MeshForge-Chunk-Size")).toBe(String(CHUNK));
    expect(part?.headers.get("X-MeshForge-Chunk-Sha256")).toBe(t.entries[2]?.kind === "file" ? t.entries[2].chunks[1] : "");
    // Every mutating call carries the CSRF header; GETs do not.
    for (const c of calls) expect(c.headers.get("X-MeshForge-CSRF")).toBe(c.method === "GET" ? null : "tok123");
    expect(report).toEqual({
      uploadedBlobs: 2,
      uploadedBytes: small.length + CHUNK + 5 + 2,
      skippedBlobs: 1,
      skippedBytes: have.length,
      resumedParts: 1,
    });
    expect(haveSha).toBeDefined();
    expect(progress.at(-1)).toBe(small.length + CHUNK + 5);
  });

  it("nothing missing = nothing uploaded (an unchanged project sends 0 bytes)", async () => {
    const [t, blobs] = await tree([{ path: "a", data: new Uint8Array([1]) }]);
    const calls = fakeController(() => json(200, { missing: [] }));
    const report = await uploadTree(t, blobs);
    expect(calls).toHaveLength(1);
    expect(report.uploadedBytes).toBe(0);
    expect(report.skippedBlobs).toBe(1);
  });

  it("a part whose bytes changed after hashing is refused, never sent under the wrong name", async () => {
    const big = new Uint8Array(CHUNK + 1).fill(1);
    const [t] = await tree([{ path: "big.bin", data: big, chunks: true }]);
    const changed = new Map([[t.entries[0]?.kind === "file" ? t.entries[0].sha256 : "", new Blob([new Uint8Array(CHUNK + 1).fill(2)])]]);
    const calls = fakeController((c) => {
      if (c.path === "/v1/blobs/have") return json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] });
      if (c.path.endsWith("/parts")) return json(404, { error: { code: "not_found", message: "no part upload" } });
      return undefined;
    });
    await expect(uploadTree(t, changed)).rejects.toThrow(UploadError);
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("the server's integrity_error on a whole blob becomes 'file changed'", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1, 2]) }]);
    fakeController((c) =>
      c.path === "/v1/blobs/have"
        ? json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] })
        : json(400, { error: { code: "integrity_error", message: "bad" } }),
    );
    await expect(uploadTree(t, blobs)).rejects.toThrow(/changed after it was hashed/);
  });

  it("retries transient failures (503 with Retry-After, no answer), never auth failures", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1]) }]);
    let puts = 0;
    fakeController((c) => {
      if (c.path === "/v1/blobs/have") return json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] });
      puts += 1;
      return puts < 3 ? json(503, { error: { code: "resource_busy", message: "busy" } }, { "retry-after": "5" }) : undefined;
    });
    const sleep = vi.fn(() => Promise.resolve());
    await uploadTree(t, blobs, { sleep });
    expect(puts).toBe(3);
    expect(sleep).toHaveBeenCalledWith(5000);

    fakeController(() => json(401, { error: { code: "unauthorized", message: "no session" } }));
    const sleep2 = vi.fn(() => Promise.resolve());
    await expect(uploadTree(t, blobs, { sleep: sleep2 })).rejects.toBeInstanceOf(ApiError);
    expect(sleep2).not.toHaveBeenCalled();
  });

  it("cancel (abort) stops the upload", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1]) }]);
    fakeController(() => json(200, { missing: [] }));
    const controller = new AbortController();
    controller.abort();
    await expect(uploadTree(t, blobs, { signal: controller.signal })).rejects.toThrow(/cancelled|abort/i);
  });

  it("cancel during a Retry-After wait settles at once (the wait is abortable)", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1]) }]);
    fakeController((c) =>
      c.path === "/v1/blobs/have"
        ? json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] })
        : json(503, { error: { code: "resource_busy", message: "busy" } }, { "retry-after": "30" }),
    );
    const controller = new AbortController();
    let waited = -1;
    // A sleep that never ends on its own: only the abort can end the wait.
    const sleep = (ms: number) => {
      waited = ms;
      return new Promise<void>(() => undefined);
    };
    const run = uploadTree(t, blobs, { signal: controller.signal, sleep });
    await vi.waitFor(() => {
      expect(waited).toBe(30_000);
    });
    controller.abort();
    await expect(run).rejects.toThrow(/cancelled|abort/i);
  });

  it("a Retry-After beyond the ceiling fails with a clear message instead of waiting", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1]) }]);
    let puts = 0;
    fakeController((c) => {
      if (c.path === "/v1/blobs/have") return json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] });
      puts += 1;
      return json(503, { error: { code: "resource_busy", message: "busy" } }, { "retry-after": String(MAX_RETRY_WAIT_MS / 1000 + 1) });
    });
    const sleep = vi.fn(() => Promise.resolve());
    const error: unknown = await uploadTree(t, blobs, { sleep }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(UploadError);
    expect(String(error)).toMatch(/busy.*61 seconds.*try again later/);
    expect(sleep).not.toHaveBeenCalled();
    expect(puts).toBe(1);
  });

  it("a Retry-After exactly at the ceiling is still honoured", async () => {
    const [t, blobs] = await tree([{ path: "a.txt", data: new Uint8Array([1]) }]);
    let puts = 0;
    fakeController((c) => {
      if (c.path === "/v1/blobs/have") return json(200, { missing: [`sha256:${t.entries[0]?.kind === "file" ? t.entries[0].sha256 : ""}`] });
      puts += 1;
      return puts < 2 ? json(503, { error: { code: "resource_busy", message: "busy" } }, { "retry-after": String(MAX_RETRY_WAIT_MS / 1000) }) : undefined;
    });
    const sleep = vi.fn(() => Promise.resolve());
    await uploadTree(t, blobs, { sleep });
    expect(sleep).toHaveBeenCalledWith(MAX_RETRY_WAIT_MS);
  });
});
