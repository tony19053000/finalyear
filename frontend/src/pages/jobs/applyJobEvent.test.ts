import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { FabricEvent } from "../../events/eventStream";
import { applyJobEvent, type JobListData } from "./applyJobEvent";
import { jobKeys } from "./jobKeys";
import { NO_FILTERS, type JobDetail, type JobSummary } from "./jobsApi";

function job(id: string, state: JobSummary["state"] = "queued"): JobSummary {
  return {
    job_id: id,
    name: id,
    state,
    placement: { mode: "auto" },
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    priority: 0,
  };
}

function list(...pages: JobSummary[][]): JobListData {
  return {
    pages: pages.map((jobs) => ({ jobs, count: jobs.length, next_cursor: null })),
    pageParams: pages.map((_, i) => (i === 0 ? null : `c${String(i)}`)),
  };
}

function event(type: string, data: Record<string, unknown>): FabricEvent {
  return { seq: 1, type, at: 0, data };
}

describe("applyJobEvent (D172: patch/invalidate the cache, no second store)", () => {
  it("patches the job's row in every cached list page, without refetching the list", () => {
    const client = new QueryClient();
    const key = jobKeys.list(NO_FILTERS);
    client.setQueryData(key, list([job("job_03"), job("job_02")], [job("job_01")]));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    expect(applyJobEvent(client, event("job.state", { job_id: "job_01", state: "running" }))).toBe(true);
    const data = client.getQueryData<JobListData>(key);
    expect(data?.pages[1]?.jobs[0]?.state).toBe("running");
    expect(data?.pages[0]?.jobs.map((j) => j.state)).toEqual(["queued", "queued"]);
    // Only the job's own detail is invalidated, never the list.
    expect(invalidate.mock.calls.map((c) => c[0]?.queryKey)).toEqual([jobKeys.detail("job_01")]);
  });

  it("patches the cached detail's state at once", () => {
    const client = new QueryClient();
    client.setQueryData<Partial<JobDetail>>(jobKeys.detail("job_01"), { job_id: "job_01", state: "queued" });
    applyJobEvent(client, event("job.state", { job_id: "job_01", state: "succeeded" }));
    expect(client.getQueryData<JobDetail>(jobKeys.detail("job_01"))?.state).toBe("succeeded");
  });

  it("a NEW job (newer than every listed one) refetches the lists it belongs to", () => {
    const client = new QueryClient();
    const all = jobKeys.list(NO_FILTERS);
    const failedOnly = jobKeys.list({ ...NO_FILTERS, states: ["failed"] });
    client.setQueryData(all, list([job("job_05")]));
    client.setQueryData(failedOnly, list([job("job_04", "failed")]));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyJobEvent(client, event("job.state", { job_id: "job_09", state: "queued" }));
    const keys = invalidate.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(all);
    expect(keys).not.toContainEqual(failedOnly); // a queued job is not in a failed-only list
  });

  it("an OLD job not loaded (a later page, or filtered out) changes nothing in the lists", () => {
    const client = new QueryClient();
    const key = jobKeys.list(NO_FILTERS);
    client.setQueryData(key, list([job("job_05")]));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyJobEvent(client, event("job.state", { job_id: "job_01", state: "failed" }));
    expect(invalidate.mock.calls.map((c) => c[0]?.queryKey)).toEqual([jobKeys.detail("job_01")]);
  });

  it("attempt.state refetches the job's detail and decisions (prefix), not the lists", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    expect(applyJobEvent(client, event("attempt.state", { attempt_id: "att_1", job_id: "job_01", state: "running" }))).toBe(true);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: jobKeys.detail("job_01") });
    expect(jobKeys.decisions("job_01").slice(0, 2)).toEqual(jobKeys.detail("job_01"));
  });

  it("ignores other event types and malformed job events safely", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    expect(applyJobEvent(client, event("node.status", { node_id: "n" }))).toBe(false);
    expect(applyJobEvent(client, event("job.state", { state: "running" }))).toBe(true);
    expect(applyJobEvent(client, event("job.state", { job_id: "job_01", state: "exploded" }))).toBe(true);
    // An unknown state patches nothing; the detail is still refetched (truth from the API).
    expect(invalidate.mock.calls.map((c) => c[0]?.queryKey)).toEqual([jobKeys.detail("job_01")]);
  });
});
