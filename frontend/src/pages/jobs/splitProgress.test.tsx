/**
 * V0.10 G7 (D270): the Job page's Chunks tab against a fake controller - every
 * number on screen is what `GET /v1/jobs/{id}/split` returned (checklist 37:
 * which the controller read from its rows), the grid draws only a window of a
 * 10 000-chunk map, a cell opens that chunk; `split.progress` events patch the
 * cache and stale ones are ignored (the V0.9 D257 `isStale` rule).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CELL, DEFAULT_COLUMNS, charAt, columnsFor, liveElapsed, mapCounts } from "../../components/jobs/splitView";
import { ToastProvider } from "../../components/Toast";
import { EVENT_APPLIERS } from "../../events/appliers";
import type { FabricEvent, SocketLike } from "../../events/eventStream";
import { SPLIT_PROGRESS, applySplitEvent, isStale, parseChunkCounts, parseSplitEvent } from "./applySplitEvent";
import { JobDetailPage } from "./JobDetailPage";
import { jobKeys } from "./jobKeys";
import type { JobDetail } from "./jobsApi";
import { isSplitJob, type ChunkOut, type SplitProgress } from "./splitApi";

const JOB = "job_01J0000000000000000000SPLT";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function progress(over: Partial<SplitProgress> = {}): SplitProgress {
  return {
    job_id: JOB,
    job_state: "running",
    queue_state: "open",
    items_total: 600,
    chunk_items: 10,
    chunks: { total: 60, pending: 25, leased: 3, done: 31, failed: 1 },
    items_done: 310,
    live_leases: 4,
    speculative_live: 1,
    speculated: 2,
    lanes: [
      {
        attempt_id: "att_A",
        node_id: "node_A",
        state: "running",
        live: true,
        chunks_done: 21,
        items_done: 210,
        failed_tries: 1,
        current: [
          { chunk_index: 40, lease_seq: 1, speculative: false, leased_at: "2026-10-20T10:00:30Z" },
          { chunk_index: 41, lease_seq: 2, speculative: true, leased_at: "2026-10-20T10:00:31Z" },
        ],
        rate_items_s: 4.25,
        started_at: "2026-10-20T10:00:01Z",
        first_lease_at: "2026-10-20T10:00:02Z",
        last_commit_at: "2026-10-20T10:00:29Z",
        ended_at: null,
      },
      {
        attempt_id: "att_B",
        node_id: "node_B",
        state: "lost",
        live: false,
        chunks_done: 10,
        items_done: 100,
        failed_tries: 0,
        current: [],
        rate_items_s: null,
        started_at: "2026-10-20T10:00:01Z",
        first_lease_at: "2026-10-20T10:00:02Z",
        last_commit_at: "2026-10-20T10:00:20Z",
        ended_at: "2026-10-20T10:00:25Z",
      },
    ],
    lanes_total: 2,
    rate_items_s: 4.25,
    eta_s: 68,
    started_at: "2026-10-20T10:00:00Z",
    ended_at: null,
    elapsed_s: 30,
    predicted_s: 95,
    combine_ms: null,
    warnings: [{ code: "nondeterministic_chunk", count: 2 }],
    computed_at: "2026-10-20T10:00:30Z",
    chunk_map: `${"d".repeat(31)}f${"l".repeat(2)}s${"p".repeat(25)}`,
    ...over,
  };
}

function job(over: Partial<JobDetail> = {}): JobDetail {
  return {
    job_id: JOB,
    name: "split-demo",
    state: "running",
    placement: { mode: "auto" },
    created_at: "2026-10-20T09:59:59Z",
    updated_at: "2026-10-20T10:00:30Z",
    priority: 0,
    manifest: { name: "split-demo", entrypoint: ["python", "main.py"], split: { over: { range: { stop: 600 } } } },
    transitions: [],
    attempts: [],
    inputs: [],
    warnings: [],
    ...over,
  };
}

const CHUNK: ChunkOut = {
  chunk_index: 3,
  state: "done",
  first_item: 30,
  item_count: 10,
  attempts: 1,
  speculations: 0,
  node_id: "node_B",
  attempt_id: "att_B",
  result_digest: `sha256:${"ab".repeat(32)}`,
  done_at: "2026-10-20T10:00:05Z",
  failed_kind: null,
  failed_nodes: [],
  leases: [],
};

class LogSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    // client close
  }
}

function fakeController(detail: JobDetail, summary: SplitProgress): URL[] {
  const calls: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string) => {
      const url = new URL(input, "https://ctl.test");
      calls.push(url);
      if (url.pathname === `/v1/jobs/${JOB}`) return Promise.resolve(json(200, detail));
      if (url.pathname === `/v1/jobs/${JOB}/split`) return Promise.resolve(json(200, summary));
      if (url.pathname === `/v1/jobs/${JOB}/chunks`) {
        const start = Number(url.searchParams.get("start"));
        return Promise.resolve(json(200, { job_id: JOB, chunks: [{ ...CHUNK, chunk_index: start }], count: 1, next_cursor: null }));
      }
      if (url.pathname === `/v1/jobs/${JOB}/decisions`) return Promise.resolve(json(200, { job_id: JOB, decisions: [], count: 0 }));
      return Promise.resolve(json(404, { error: { code: "not_found", message: `no route ${url.pathname}` } }));
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPage(detail: JobDetail, summary: SplitProgress) {
  const calls = fakeController(detail, summary);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/jobs/${JOB}`]}>
          <Routes>
            <Route path="/jobs/:jobId" element={<JobDetailPage logSocketFactory={(u) => new LogSocket(u)} />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return { calls, client };
}

function field(name: string): string {
  const el = document.querySelector(`[data-field="${name}"]`);
  return el?.textContent ?? "";
}

describe("Chunks tab (V0.10 G7)", () => {
  it("is offered for split jobs only", async () => {
    fakeController(job({ manifest: { name: "w", entrypoint: ["python", "main.py"] } }), progress());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ToastProvider>
          <MemoryRouter initialEntries={[`/jobs/${JOB}`]}>
            <Routes>
              <Route path="/jobs/:jobId" element={<JobDetailPage logSocketFactory={(u) => new LogSocket(u)} />} />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("tab", { name: "Logs" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Chunks" })).toBeNull();
    expect(isSplitJob({ split: {} })).toBe(true);
    expect(isSplitJob({ split: null })).toBe(false);
    expect(isSplitJob(undefined)).toBe(false);
  });

  it("shows exactly the API's numbers: progress, legend, nodes", async () => {
    const summary = progress();
    const { calls } = renderPage(job(), summary);
    await userEvent.click(await screen.findByRole("tab", { name: "Chunks" }));
    await screen.findByRole("region", { name: "Progress" });
    expect(calls.some((u) => u.pathname.endsWith("/split") && u.searchParams.get("map") === "true")).toBe(true);
    expect(field("items")).toBe("310 / 600");
    expect(field("chunks-done")).toBe("31 / 60");
    expect(field("chunks-leased")).toBe("3");
    expect(field("chunks-pending")).toBe("25");
    expect(field("chunks-failed")).toBe("1");
    expect(field("speculative")).toBe("1 live · 2 total");
    expect(field("rate")).toBe("4.25 items/s");
    expect(field("eta")).toBe("1 m 08 s");
    expect(field("makespan")).toMatch(/ \/ 1 m 35 s$/);
    expect(field("percent")).toBe("51%");
    expect(screen.getByRole("list", { name: "Split warnings" }).textContent).toContain("nondeterministic_chunk");
    // legend counts = the chunk map's characters
    const legend = screen.getByRole("list", { name: "Chunk states" });
    expect(within(legend).getByText(/^done/).textContent).toBe("done 31");
    expect(document.querySelector('[data-legend="f"]')?.textContent).toBe("failed 1");
    expect(document.querySelector('[data-legend="s"]')?.textContent).toBe("running + speculative copy 1");
    // one row per lane, with its own numbers
    const lanes = screen.getByRole("table", { name: /one row per lane/i });
    const rows = within(lanes).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("node_A");
    expect(rows[0]?.querySelector('[data-field="lane-chunks"]')?.textContent).toBe("21");
    expect(rows[0]?.querySelector('[data-field="lane-items"]')?.textContent).toBe("210");
    expect(rows[0]?.querySelector('[data-field="lane-current"]')?.textContent).toBe("#40 #41*");
    expect(rows[1]?.textContent).toContain("lost");
    expect(rows[1]?.textContent).toContain("—"); // no rate measured for the lost lane
  });

  it("draws only a window of a 10 000-chunk grid and opens a clicked chunk", async () => {
    const big = "d".repeat(4000) + "l".repeat(10) + "p".repeat(5990);
    const { calls } = renderPage(job(), progress({ chunk_map: big }));
    await userEvent.click(await screen.findByRole("tab", { name: "Chunks" }));
    const svg = await screen.findByRole("img", { name: "10000 chunks coloured by state" });
    const rows = Math.ceil(10000 / DEFAULT_COLUMNS);
    expect(svg.getAttribute("height")).toBe(String(rows * CELL));
    const drawn = Number(svg.getAttribute("data-drawn"));
    expect(drawn).toBeGreaterThan(0);
    // virtualised: not 10 000 nodes (jsdom has no layout, so the window is
    // the first-paint guess of a 1 200 px viewport; the e2e test measures the
    // real browser)
    expect(drawn).toBeLessThan(10000);
    expect(svg.querySelectorAll("rect")).toHaveLength(drawn);
    const cell = svg.querySelector('rect[data-chunk="3"]');
    expect(cell).not.toBeNull();
    fireEvent.click(cell as Element);
    await waitFor(() => {
      expect(field("chunk-node")).toBe("node_B");
    });
    expect(field("chunk-index")).toBe("#3");
    expect(field("chunk-items")).toBe("30–39 (10)");
    expect(field("chunk-state")).toBe("done");
    const asked = calls.find((u) => u.pathname.endsWith("/chunks"));
    expect(asked?.searchParams.get("start")).toBe("3");
    expect(asked?.searchParams.get("limit")).toBe("1");
  });
});

/* ------------------------------------------------------------ events */

function event(data: Record<string, unknown>, type = SPLIT_PROGRESS): FabricEvent {
  return { seq: 1, type, at: 1, data };
}

function eventData(over: Partial<SplitProgress> = {}): Record<string, unknown> {
  // the event carries no chunk map
  const rest: Record<string, unknown> = { ...progress(over) };
  delete rest.chunk_map;
  return rest;
}

describe("split.progress -> cache (isStale, D257 rule)", () => {
  it("patches the cached summary, keeps the map, refetches when counts move", () => {
    const client = new QueryClient();
    client.setQueryData(jobKeys.split(JOB), progress());
    const spy = vi.spyOn(client, "invalidateQueries");
    const newer = eventData({
      chunks: { total: 60, pending: 20, leased: 3, done: 36, failed: 1 },
      items_done: 360,
      computed_at: "2026-10-20T10:00:31Z",
    });
    expect(applySplitEvent(client, event(newer))).toBe(true);
    const cached = client.getQueryData<SplitProgress>(jobKeys.split(JOB));
    expect(cached?.chunks.done).toBe(36);
    expect(cached?.chunk_map).toBe(progress().chunk_map);
    expect(spy).toHaveBeenCalledWith({ queryKey: jobKeys.split(JOB), exact: true });
  });

  it("ignores an event older than the cache", () => {
    const client = new QueryClient();
    const cached = progress({ computed_at: "2026-10-20T10:00:40Z" });
    client.setQueryData(jobKeys.split(JOB), cached);
    const cases = [
      eventData({ computed_at: "2026-10-20T10:00:39Z" }), // older read
      eventData({ chunks: { total: 60, pending: 30, leased: 0, done: 30, failed: 0 }, computed_at: "2026-10-20T10:00:50Z" }),
      eventData({ items_done: 100, computed_at: "2026-10-20T10:00:50Z" }),
    ];
    for (const data of cases) {
      applySplitEvent(client, event(data));
      expect(client.getQueryData(jobKeys.split(JOB))).toBe(cached);
    }
    const ended = progress({ job_state: "succeeded", computed_at: "2026-10-20T10:00:40Z" });
    client.setQueryData(jobKeys.split(JOB), ended);
    const before = client.getQueryData(jobKeys.split(JOB)); // structural sharing
    applySplitEvent(client, event(eventData({ job_state: "running", computed_at: "2026-10-20T10:01:00Z" })));
    expect(client.getQueryData(jobKeys.split(JOB))).toBe(before);
    expect(isStale(ended, progress({ job_state: "succeeded", computed_at: "2026-10-20T10:01:00Z" }))).toBe(false);
  });

  it("never writes a bad event and ignores jobs nobody shows", () => {
    const client = new QueryClient();
    const cached = progress();
    client.setQueryData(jobKeys.split(JOB), cached);
    const bad = [
      { ...eventData(), job_id: 7 },
      { ...eventData(), job_state: "dancing" },
      { ...eventData(), chunks: { total: 1 } },
      { ...eventData(), items_done: -1 },
      { ...eventData(), computed_at: "yesterday" },
      { ...eventData(), lanes: "x" },
    ];
    for (const data of bad) {
      expect(parseSplitEvent(data)).toBeUndefined();
      expect(applySplitEvent(client, event(data))).toBe(true);
      expect(client.getQueryData(jobKeys.split(JOB))).toBe(cached);
    }
    expect(applySplitEvent(client, event(eventData({ job_id: "job_other" })))).toBe(true);
    expect(client.getQueryData(jobKeys.split("job_other"))).toBeUndefined();
    expect(applySplitEvent(client, event(eventData(), "job.state"))).toBe(false);
    expect(parseChunkCounts({ total: 1, pending: 0, leased: 0, done: 1, failed: 0.5 })).toBeUndefined();
  });

  it("is wired into the shell's appliers", () => {
    expect(EVENT_APPLIERS).toContain(applySplitEvent);
  });
});

describe("chunk grid helpers", () => {
  it("fits columns to the width and reads the map defensively", () => {
    expect(columnsFor(0)).toBe(DEFAULT_COLUMNS);
    expect(columnsFor(600)).toBe(50);
    expect(columnsFor(30)).toBe(10);
    expect(charAt("dlx", 0)).toBe("d");
    expect(charAt("dlx", 2)).toBe("p");
    expect(charAt("d", 5)).toBe("p");
    expect(mapCounts("ddlspf?")).toEqual({ d: 2, l: 1, s: 1, p: 1, f: 1 });
  });

  it("moves the elapsed time on while the job runs, not after it ended", () => {
    const running = progress({ elapsed_s: 30, computed_at: "2026-10-20T10:00:30Z" });
    expect(liveElapsed(running, Date.parse("2026-10-20T10:00:35Z"))).toBe(35);
    expect(liveElapsed({ ...running, ended_at: "2026-10-20T10:00:31Z" }, Date.parse("2026-10-20T11:00:00Z"))).toBe(30);
    expect(liveElapsed({ ...running, elapsed_s: null }, 0)).toBeNull();
  });
});
