/**
 * V0.10 G8: (a) the chunk detail shows the chunk's recorded tries with their
 * stderr tails (`GET /v1/jobs/{id}/chunks/{n}/tries`), as text - never markup;
 * (d) the `?map=true` summary is refetched at most once per
 * `MAP_REFETCH_MIN_MS` per job, with one trailing refetch, and at once when the
 * job ends.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../../components/Toast";
import type { FabricEvent, SocketLike } from "../../events/eventStream";
import {
  MAP_REFETCH_MIN_MS,
  SPLIT_PROGRESS,
  applySplitEvent,
  resetMapRefetchThrottle,
} from "./applySplitEvent";
import { JobDetailPage } from "./JobDetailPage";
import { jobKeys } from "./jobKeys";
import type { JobDetail } from "./jobsApi";
import type { ChunkTries, SplitProgress } from "./splitApi";

const JOB = "job_01J0000000000000000000SPL8";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function summary(done: number, over: Partial<SplitProgress> = {}): SplitProgress {
  return {
    job_id: JOB,
    job_state: "running",
    queue_state: "open",
    items_total: 40,
    chunk_items: 10,
    chunks: { total: 4, pending: 4 - done, leased: 0, done, failed: 0 },
    items_done: done * 10,
    live_leases: 0,
    speculative_live: 0,
    speculated: 0,
    lanes: [],
    lanes_total: 0,
    rate_items_s: null,
    eta_s: null,
    started_at: "2026-10-21T10:00:00Z",
    ended_at: null,
    elapsed_s: 1,
    predicted_s: 5,
    combine_ms: null,
    warnings: [],
    computed_at: `2026-10-21T10:00:${String(10 + done).padStart(2, "0")}Z`,
    chunk_map: "d".repeat(done) + "p".repeat(4 - done),
    ...over,
  };
}

function event(data: SplitProgress): FabricEvent {
  const rest: Record<string, unknown> = { ...data };
  delete rest.chunk_map;
  return { seq: 1, type: SPLIT_PROGRESS, at: 1, data: rest };
}

beforeEach(() => {
  resetMapRefetchThrottle();
});

afterEach(() => {
  resetMapRefetchThrottle();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("?map=true refetch throttle (D286 carry-over)", () => {
  it("refetches the first change at once, then at most once per window with one trailing refetch", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-21T10:00:00Z"));
    const client = new QueryClient();
    client.setQueryData(jobKeys.split(JOB), summary(0));
    const spy = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    applySplitEvent(client, event(summary(1)));
    expect(spy).toHaveBeenCalledTimes(1);
    for (let done = 2; done <= 3; done++) {
      vi.advanceTimersByTime(100);
      applySplitEvent(client, event(summary(done)));
    }
    expect(spy).toHaveBeenCalledTimes(1); // inside the window: deferred
    vi.advanceTimersByTime(MAP_REFETCH_MIN_MS);
    expect(spy).toHaveBeenCalledTimes(2); // exactly one trailing refetch
    vi.advanceTimersByTime(10 * MAP_REFETCH_MIN_MS);
    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenLastCalledWith({ queryKey: jobKeys.split(JOB), exact: true });
  });

  it("refetches the job's end at once, inside the window too", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-21T10:00:00Z"));
    const client = new QueryClient();
    client.setQueryData(jobKeys.split(JOB), summary(0));
    const spy = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    applySplitEvent(client, event(summary(3)));
    vi.advanceTimersByTime(50);
    applySplitEvent(client, event(summary(4, { job_state: "succeeded", ended_at: "2026-10-21T10:00:15Z" })));
    expect(spy).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(10 * MAP_REFETCH_MIN_MS);
    expect(spy).toHaveBeenCalledTimes(2); // no stray trailing refetch
  });

  it("an unchanged count never refetches", () => {
    const client = new QueryClient();
    client.setQueryData(jobKeys.split(JOB), summary(1));
    const spy = vi.spyOn(client, "invalidateQueries");
    applySplitEvent(client, event(summary(1, { computed_at: "2026-10-21T10:00:20Z", eta_s: 3 })));
    expect(spy).not.toHaveBeenCalled();
  });
});

/* ------------------------------------------------------------ tries */

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

const TRIES: ChunkTries = {
  job_id: JOB,
  chunk_index: 2,
  count: 2,
  tries: [
    {
      lease_seq: 1,
      attempt_id: "att_A",
      node_id: "node_A",
      speculative: false,
      won: false,
      outcome: "failed",
      failure_type: "user_error",
      failure_class: "workload",
      exit_code: 4,
      result_digest: null,
      bytes_in: 120,
      bytes_out: null,
      stage_ms: 30,
      env_ms: 0,
      run_ms: 1500,
      upload_ms: null,
      leased_at: "2026-10-21T10:00:01Z",
      ended_at: "2026-10-21T10:00:03Z",
      stderr_tail: "Traceback\nValueError: <b>bad item</b> [redacted]\n",
    },
    {
      lease_seq: 2,
      attempt_id: "att_B",
      node_id: "node_B",
      speculative: false,
      won: true,
      outcome: "committed",
      failure_type: null,
      failure_class: "ok",
      exit_code: 0,
      result_digest: `sha256:${"cd".repeat(32)}`,
      bytes_in: 120,
      bytes_out: 512,
      stage_ms: 20,
      env_ms: 0,
      run_ms: 900,
      upload_ms: 40,
      leased_at: "2026-10-21T10:00:04Z",
      ended_at: "2026-10-21T10:00:05Z",
      stderr_tail: null,
    },
  ],
};

function job(): JobDetail {
  return {
    job_id: JOB,
    name: "split-g8",
    state: "running",
    placement: { mode: "auto" },
    created_at: "2026-10-21T09:59:59Z",
    updated_at: "2026-10-21T10:00:30Z",
    priority: 0,
    manifest: { name: "split-g8", entrypoint: ["python", "main.py"], split: { over: { range: { stop: 40 } } } },
    transitions: [],
    attempts: [],
    inputs: [],
    warnings: [],
  };
}

describe("chunk tries with stderr tails (D261 carry-over)", () => {
  it("lists every try with its outcome, timings and stderr as text", async () => {
    const calls: URL[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string) => {
        const url = new URL(input, "https://ctl.test");
        calls.push(url);
        if (url.pathname === `/v1/jobs/${JOB}`) return Promise.resolve(json(200, job()));
        if (url.pathname === `/v1/jobs/${JOB}/split`) return Promise.resolve(json(200, summary(2)));
        if (url.pathname === `/v1/jobs/${JOB}/chunks`) {
          return Promise.resolve(
            json(200, {
              job_id: JOB,
              count: 1,
              next_cursor: null,
              chunks: [
                {
                  chunk_index: 2,
                  state: "done",
                  first_item: 20,
                  item_count: 10,
                  attempts: 2,
                  speculations: 0,
                  node_id: "node_B",
                  attempt_id: "att_B",
                  result_digest: TRIES.tries[1]?.result_digest ?? null,
                  done_at: "2026-10-21T10:00:05Z",
                  failed_kind: null,
                  failed_nodes: ["node_A"],
                  leases: [],
                },
              ],
            }),
          );
        }
        if (url.pathname === `/v1/jobs/${JOB}/chunks/2/tries`) return Promise.resolve(json(200, TRIES));
        if (url.pathname === `/v1/jobs/${JOB}/decisions`) return Promise.resolve(json(200, { job_id: JOB, decisions: [], count: 0 }));
        return Promise.resolve(json(404, { error: { code: "not_found", message: url.pathname } }));
      }),
    );
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
    await userEvent.click(await screen.findByRole("tab", { name: "Chunks" }));
    const svg = await screen.findByRole("img", { name: "4 chunks coloured by state" });
    const cell = svg.querySelector('rect[data-chunk="2"]');
    fireEvent.click(cell as Element);
    const list = await screen.findByRole("list", { name: "Tries of chunk 2" });
    await waitFor(() => {
      expect(list.querySelectorAll('[data-field="chunk-try"]')).toHaveLength(2);
    });
    const [first, second] = Array.from(list.querySelectorAll('[data-field="chunk-try"]'));
    expect(first?.textContent).toContain("try 1 · node_A · failed");
    expect(first?.textContent).toContain("user_error");
    expect(first?.textContent).toContain("exit 4");
    expect(second?.textContent).toContain("committed (won)");
    const pre = first?.querySelector('[data-field="chunk-stderr"]');
    expect(pre?.textContent).toBe(TRIES.tries[0]?.stderr_tail);
    expect(pre?.querySelector("b")).toBeNull(); // text, never markup
    expect(second?.querySelector('[data-field="chunk-stderr"]')).toBeNull();
    expect(calls.some((u) => u.pathname.endsWith("/chunks/2/tries"))).toBe(true);
  });
});
