import { afterEach, describe, expect, it, vi } from "vitest";

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { ApiError, NetworkError } from "../../api/errors";
import { formatBytes, formatDuration, isoToLocalInput, localInputToIso, shortDigest } from "../../components/jobs/format";
import { WINDOW_THRESHOLD, computeWindow } from "../../components/jobs/useWindowedRows";
import { ACTIVE_STATES, filtersFromSearch, searchFromFilters, stateChoice, statesFromChoice } from "./jobFilters";
import { NO_FILTERS, blobUrl, logDownloadUrl, rawRequest } from "./jobsApi";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("computeWindow (rows virtualised over 200, D174)", () => {
  const opts = { rowHeight: 40, overscan: 5 };

  it("renders everything up to the threshold", () => {
    expect(computeWindow(WINDOW_THRESHOLD, 0, 400, opts)).toMatchObject({ start: 0, end: 200, padTop: 0, padBottom: 0, windowed: false });
  });

  it("over the threshold: the visible rows + overscan, spacers for the rest", () => {
    const w = computeWindow(10_000, 4000, 400, opts); // first visible row 100, 10 visible
    expect(w).toEqual({ start: 95, end: 115, padTop: 95 * 40, padBottom: (10_000 - 115) * 40, windowed: true, lastVisible: 109 });
  });

  it("clamps at both ends and accounts for a sticky header offset", () => {
    expect(computeWindow(1000, 0, 400, opts).start).toBe(0);
    const bottom = computeWindow(1000, 1_000_000, 400, opts);
    expect(bottom.end).toBe(1000);
    expect(bottom.padBottom).toBe(0);
    expect(computeWindow(1000, 432, 400, { ...opts, offset: 32 }).lastVisible).toBe(19);
  });
});

describe("format", () => {
  it("durations, sizes, digests, datetime-local round trip", () => {
    expect(formatDuration(850)).toBe("850 ms");
    expect(formatDuration(12_400)).toBe("12.4 s");
    expect(formatDuration(185_000)).toBe("3 m 05 s");
    expect(formatDuration(7_260_000)).toBe("2 h 01 m");
    expect(formatDuration(null)).toBe("—");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatBytes(8 * 1024 * 1024)).toBe("8.0 MiB");
    expect(shortDigest(`sha256:${"a".repeat(64)}`)).toBe(`${"a".repeat(12)}…`);
    const iso = localInputToIso("2026-10-01T10:30");
    expect(isoToLocalInput(iso)).toBe("2026-10-01T10:30");
    expect(localInputToIso("")).toBe("");
  });
});

describe("list filters <-> URL", () => {
  it("round-trips and maps the state choice", () => {
    const filters = { ...NO_FILTERS, states: ACTIVE_STATES, node: "node_A", createdAfter: "2026-10-01T00:00:00.000Z" };
    expect(filtersFromSearch(searchFromFilters(filters))).toEqual(filters);
    expect(stateChoice(ACTIVE_STATES)).toBe("active");
    expect(statesFromChoice("finished")).toEqual(["succeeded", "failed", "cancelled"]);
    expect(statesFromChoice("running")).toEqual(["running"]);
    expect(filtersFromSearch(new URLSearchParams("state=bogus&state=failed")).states).toEqual(["failed"]);
  });
});

describe("jobsApi", () => {
  it("download URLs", () => {
    expect(blobUrl(`sha256:${"b".repeat(64)}`)).toBe(`/v1/blobs/${"b".repeat(64)}`);
    expect(logDownloadUrl("job_1", "att_2")).toBe("/v1/jobs/job_1/logs?stream=both&attempt=att_2");
  });

  it("rawRequest: /v1 only, same-origin cookie, CSRF on mutations only, typed errors", async () => {
    document.cookie = "mf_csrf=c-9";
    const seen: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_: string, init: RequestInit) => {
        seen.push(init);
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
      }),
    );
    await expect(rawRequest("/elsewhere", { method: "GET" })).rejects.toThrow(/start with \/v1\//);
    await rawRequest("/v1/x", { method: "PUT", body: "b" });
    await rawRequest("/v1/x", { method: "GET" });
    expect(seen[0]?.credentials).toBe("same-origin");
    expect(new Headers(seen[0]?.headers).get("X-MeshForge-CSRF")).toBe("c-9");
    expect(new Headers(seen[1]?.headers).get("X-MeshForge-CSRF")).toBeNull();

    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "conflict", message: "busy" } }), { status: 409 }))));
    await expect(rawRequest("/v1/x", { method: "POST" })).rejects.toMatchObject({ status: 409, code: "conflict" });
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("down"))));
    await expect(rawRequest("/v1/x", { method: "POST" })).rejects.toBeInstanceOf(NetworkError);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("<html>", { status: 200 }))));
    await expect(rawRequest("/v1/x", { method: "GET" })).rejects.toBeInstanceOf(ApiError);
  });
});

describe("refused-project alert contrast (fix round 1, T-16)", () => {
  // The submit page's alert prints `text-status-failed` on `bg-status-failed-soft` (a translucent
  // tone) over a card. axe measures the BLENDED background, so check exactly that, both themes.
  const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../styles/tokens.css"), "utf8");
  const theme = (selector: string) => {
    const open = css.indexOf("{", css.indexOf(selector));
    const body = css.slice(open + 1, css.indexOf("}", open));
    return new Map([...body.matchAll(/--mf-([a-z0-9-]+):\s*#([0-9a-f]{6,8})\b/g)].map((m) => [m[1] ?? "", m[2] ?? ""] as const));
  };
  const rgb = (hex: string) => [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const lum = (c: number[]) => {
    const [r = 0, g = 0, b = 0] = c.map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a: number[], b: number[]) => {
    const [hi = 0, lo = 0] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  for (const selector of ['[data-theme="dark"]', '[data-theme="light"]']) {
    it(`status-failed text on its soft tone is >= 4.5:1 over every surface (${selector})`, () => {
      const t = theme(selector);
      const text = rgb(t.get("status-failed") ?? "000000");
      const soft = t.get("status-failed-soft") ?? "00000000";
      const alpha = parseInt(soft.slice(6, 8), 16) / 255;
      for (const surface of ["bg", "surface", "surface-raised", "surface-sunken"]) {
        const under = rgb(t.get(surface) ?? "000000");
        const blended = rgb(soft).map((v, i) => Math.round(v * alpha + (under[i] ?? 0) * (1 - alpha)));
        expect(ratio(text, blended), `${selector} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    });
  }
});
