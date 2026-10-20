/**
 * V0.10 G7 tester r1: attacks on the stale-event rule and the Chunks tab's
 * defences. Out-of-order / duplicate / hostile `split.progress` events against
 * the TanStack cache; what the page shows when the numbers are odd.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../../components/Toast";
import type { FabricEvent, SocketLike } from "../../events/eventStream";
import {
  SPLIT_PROGRESS,
  applySplitEvent,
  isStale,
  parseSplitEvent,
} from "./applySplitEvent";
import { JobDetailPage } from "./JobDetailPage";
import { jobKeys } from "./jobKeys";
import type { JobDetail } from "./jobsApi";
import type { SplitProgress } from "./splitApi";

const JOB = "job_01J0000000000000000000SPLT";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function base(over: Partial<SplitProgress> = {}): SplitProgress {
  return {
    job_id: JOB,
    job_state: "running",
    queue_state: "open",
    items_total: 100,
    chunk_items: 10,
    chunks: { total: 10, pending: 6, leased: 2, done: 2, failed: 0 },
    items_done: 20,
    live_leases: 2,
    speculative_live: 0,
    speculated: 0,
    lanes: [
      {
        attempt_id: "att_A",
        node_id: "node_A",
        state: "running",
        live: true,
        chunks_done: 2,
        items_done: 20,
        failed_tries: 0,
        current: [
          {
            chunk_index: 5,
            lease_seq: 1,
            speculative: false,
            leased_at: "2026-10-20T10:00:30Z",
          },
        ],
        rate_items_s: 2,
        started_at: "2026-10-20T10:00:01Z",
        first_lease_at: "2026-10-20T10:00:02Z",
        last_commit_at: "2026-10-20T10:00:29Z",
        ended_at: null,
      },
    ],
    lanes_total: 1,
    rate_items_s: 2,
    eta_s: 40,
    started_at: "2026-10-20T10:00:00Z",
    ended_at: null,
    elapsed_s: 30,
    predicted_s: 95,
    combine_ms: null,
    warnings: [],
    computed_at: "2026-10-20T10:00:30.000000+00:00",
    chunk_map: "ddllpppppp",
    ...over,
  };
}

function at(sec: number): string {
  return new Date(
    Date.parse("2026-10-20T10:00:00Z") + sec * 1000,
  ).toISOString();
}

function ev(data: Record<string, unknown>): FabricEvent {
  return { seq: 1, type: SPLIT_PROGRESS, at: 1, data };
}

function wire(p: SplitProgress): Record<string, unknown> {
  const rest: Record<string, unknown> = { ...p };
  delete rest.chunk_map;
  return rest;
}

/** A monotone run of summaries: every step commits one more chunk. */
function sequence(n: number): SplitProgress[] {
  return Array.from({ length: n }, (_, i) =>
    base({
      chunks: {
        total: n + 5,
        pending: 5 + n - i - 1,
        leased: 0,
        done: i + 1,
        failed: 0,
      },
      items_done: (i + 1) * 10,
      computed_at: at(i + 1),
      job_state: "running",
    }),
  );
}

function shuffled<T>(items: T[], seed: number): T[] {
  const out = [...items];
  let s = seed;
  for (let i = out.length - 1; i > 0; i--) {
    s = (s * 1664525 + 1013904223) % 4294967296;
    const j = s % (i + 1);
    [out[i], out[j]] = [out[j] as T, out[i] as T];
  }
  return out;
}

describe("stale-event rule: out-of-order, duplicate, equal-time", () => {
  it("any delivery order of a monotone run ends on the newest summary", () => {
    const run = sequence(40);
    for (let seed = 1; seed <= 40; seed++) {
      const client = new QueryClient();
      client.setQueryData(
        jobKeys.split(JOB),
        base({
          chunks: { total: 45, pending: 45, leased: 0, done: 0, failed: 0 },
          items_done: 0,
          computed_at: at(0),
        }),
      );
      for (const p of shuffled(run, seed)) applySplitEvent(client, ev(wire(p)));
      const got = client.getQueryData<SplitProgress>(jobKeys.split(JOB));
      expect(got?.chunks.done, `seed ${String(seed)}`).toBe(40);
      expect(got?.computed_at).toBe(at(40));
    }
  });

  it("never goes backwards while events arrive, whatever the order", () => {
    const run = sequence(30);
    const client = new QueryClient();
    client.setQueryData(
      jobKeys.split(JOB),
      base({
        computed_at: at(0),
        chunks: { total: 35, pending: 35, leased: 0, done: 0, failed: 0 },
        items_done: 0,
      }),
    );
    let high = 0;
    for (const p of shuffled(run, 99)) {
      applySplitEvent(client, ev(wire(p)));
      const done =
        client.getQueryData<SplitProgress>(jobKeys.split(JOB))?.chunks.done ??
        -1;
      expect(done).toBeGreaterThanOrEqual(high);
      high = done;
    }
  });

  it("a duplicate event changes nothing and does not trigger a refetch", () => {
    const client = new QueryClient();
    const cached = base();
    client.setQueryData(jobKeys.split(JOB), cached);
    const spy = vi.spyOn(client, "invalidateQueries");
    for (let i = 0; i < 20; i++) applySplitEvent(client, ev(wire(cached)));
    expect(spy).not.toHaveBeenCalled();
    expect(
      client.getQueryData<SplitProgress>(jobKeys.split(JOB))?.chunk_map,
    ).toBe(cached.chunk_map);
  });

  it("an event with the same instant as the cache but the job ended wins", () => {
    const cached = base();
    const ended = base({
      job_state: "succeeded",
      ended_at: at(31),
      eta_s: 0,
      computed_at: cached.computed_at,
    });
    expect(isStale(cached, ended)).toBe(false);
  });

  it("microsecond timestamps from the server compare correctly", () => {
    const a = base({ computed_at: "2026-10-20T10:00:30.123456+00:00" });
    const b = base({ computed_at: "2026-10-20T10:00:30.123999+00:00" });
    // JS parses to the millisecond: the same instant is "not older" (fine)
    expect(isStale(a, b)).toBe(false);
    expect(isStale(b, a)).toBe(false);
    const older = base({ computed_at: "2026-10-20T10:00:29.999999+00:00" });
    expect(isStale(a, older)).toBe(true);
  });

  it("a cached terminal summary is never overwritten by a live state, nor flipped to another end", () => {
    for (const end of ["succeeded", "failed", "cancelled"] as const) {
      const client = new QueryClient();
      const cached = base({
        job_state: end,
        ended_at: at(50),
        computed_at: at(50),
      });
      client.setQueryData(jobKeys.split(JOB), cached);
      for (const state of [
        "running",
        "collecting",
        "queued",
        "retrying",
      ] as const) {
        applySplitEvent(
          client,
          ev(wire(base({ job_state: state, computed_at: at(99) }))),
        );
        expect(client.getQueryData(jobKeys.split(JOB))).toBe(cached);
      }
    }
  });

  it("events for a different job never touch this job's cache", () => {
    const client = new QueryClient();
    const cached = base();
    client.setQueryData(jobKeys.split(JOB), cached);
    applySplitEvent(
      client,
      ev(
        wire(
          base({
            job_id: "job_01J0000000000000000000OTHR",
            computed_at: at(99),
          }),
        ),
      ),
    );
    expect(client.getQueryData(jobKeys.split(JOB))).toBe(cached);
  });

  it("rejects non-finite / fractional / negative counts and wrong types", () => {
    const client = new QueryClient();
    const cached = base();
    client.setQueryData(jobKeys.split(JOB), cached);
    const bad: Record<string, unknown>[] = [
      { ...wire(base()), items_done: 1.5 },
      { ...wire(base()), items_total: "100" },
      {
        ...wire(base()),
        chunks: { total: 10, pending: 6, leased: 2, done: -2, failed: 0 },
      },
      {
        ...wire(base()),
        chunks: {
          total: 10,
          pending: 6,
          leased: 2,
          done: Number.POSITIVE_INFINITY,
          failed: 0,
        },
      },
      { ...wire(base()), chunks: null },
      { ...wire(base()), computed_at: null },
      { ...wire(base()), warnings: null },
      {},
    ];
    for (const d of bad) {
      applySplitEvent(
        client,
        ev({
          ...d,
          computed_at: d.computed_at === undefined ? at(99) : d.computed_at,
        }),
      );
      expect(client.getQueryData(jobKeys.split(JOB))).toBe(cached);
    }
  });
});

/* ------------------------------------------------------------ page */

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

function detail(): JobDetail {
  return {
    job_id: JOB,
    name: "split-demo",
    state: "running",
    placement: { mode: "auto" },
    created_at: "2026-10-20T09:59:59Z",
    updated_at: "2026-10-20T10:00:30Z",
    priority: 0,
    manifest: {
      name: "split-demo",
      entrypoint: ["python", "main.py"],
      split: { over: { range: { stop: 100 } } },
    },
    transitions: [],
    attempts: [],
    inputs: [],
    warnings: [],
  };
}

function renderPage(summary: SplitProgress) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string) => {
      const url = new URL(input, "https://ctl.test");
      if (url.pathname === `/v1/jobs/${JOB}`)
        return Promise.resolve(json(200, detail()));
      if (url.pathname === `/v1/jobs/${JOB}/split`)
        return Promise.resolve(json(200, summary));
      if (url.pathname === `/v1/jobs/${JOB}/decisions`)
        return Promise.resolve(
          json(200, { job_id: JOB, decisions: [], count: 0 }),
        );
      return Promise.resolve(
        json(404, { error: { code: "not_found", message: "no" } }),
      );
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/jobs/${JOB}`]}>
          <Routes>
            <Route
              path="/jobs/:jobId"
              element={
                <JobDetailPage logSocketFactory={(u) => new LogSocket(u)} />
              }
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  return client;
}

function field(name: string): string {
  return document.querySelector(`[data-field="${name}"]`)?.textContent ?? "";
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Chunks tab with odd numbers", () => {
  it("shows zero and huge (contract-valid) numbers without NaN or Infinity", async () => {
    renderPage(
      base({ eta_s: 0, elapsed_s: 0, predicted_s: 1e12, rate_items_s: 1e-9 }),
    );
    await userEvent.click(await screen.findByRole("tab", { name: "Chunks" }));
    await screen.findByRole("region", { name: "Progress" });
    const shown = ["eta", "makespan", "rate"].map(field).join(" | ");
    expect(shown).not.toMatch(/NaN|Infinity|undefined/);
  });

  it("eta is 'not measured yet' while nothing is measured (null), not 0 or a guess", async () => {
    renderPage(base({ eta_s: null, rate_items_s: null, lanes: [] }));
    await userEvent.click(await screen.findByRole("tab", { name: "Chunks" }));
    await screen.findByRole("region", { name: "Progress" });
    expect(field("eta")).toBe("not measured yet");
    expect(field("rate")).toBe("—");
  });

  // DEFECT T-1 (minor): parseSplitEvent checks only Array.isArray(lanes), so an
  // event with a null / non-object lane entry is written to the cache and
  // `Lanes` throws (TypeError reading node_id), unmounting the page. Verified by
  // rendering it by hand (tester r1). Remove `.fails` when the parser validates
  // the lane entries (and `current`).
  it("T-1 (fixed): an event with a malformed lane entry is rejected by the parser", () => {
    const hostile = wire(base({ computed_at: at(500) }));
    hostile.lanes = [null, 7, { node_id: 3 }];
    expect(parseSplitEvent(hostile)).toBeUndefined();
  });
});
