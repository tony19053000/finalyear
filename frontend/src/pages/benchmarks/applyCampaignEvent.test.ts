/**
 * V0.9 G6: `campaign.progress` -> the query cache (pure applier, like
 * applyJobEvent): patches in place, never writes a bad field, refetches only
 * what it cannot patch.
 */
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { EVENT_APPLIERS } from "../../events/appliers";
import type { FabricEvent } from "../../events/eventStream";
import { CAMPAIGN_PROGRESS, applyCampaignEvent, parseCounts, parseProgress } from "./applyCampaignEvent";
import type { CampaignDetail, CampaignList, CampaignStatus } from "./benchApi";
import { benchKeys } from "./benchKeys";

const COUNTS = { planned: 6, submitted: 2, done: 2, failed: 0, skipped: 0 };
const ID = "camp_01J00000000000000000000AAA";
const OTHER = "camp_01J00000000000000000000BBB";

function campaign(id: string, over: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    campaign_id: id,
    name: `c-${id.slice(-3)}`,
    state: "active",
    seed: 1,
    spec_digest: "sha256:x",
    run_count: 10,
    counts: { ...COUNTS },
    summary: { groups: 1, configs: 2, dropped: 0, duplicates: 0, estimated_hours: 0.5, nodes: ["node_a"], unsupported: 0, session_label: null },
    created_by: "usr_admin",
    created_at: "2026-10-19T09:00:00Z",
    spec: "name: x\n",
    ...over,
  };
}

function status(id: string, over: Partial<CampaignStatus> = {}): CampaignStatus {
  return {
    campaign_id: id,
    state: "active",
    runner_owned: true,
    started_at: "2026-10-19T09:00:00Z",
    finished_reason: null,
    max_concurrent_per_node: 1,
    cooldown_s: 0,
    max_infra_retries: 2,
    counts: { ...COUNTS },
    cancelled_jobs: 0,
    done_percent: 20,
    median_run_s: 12,
    eta_s: 96,
    infra_failure_rate: 0,
    records: 2,
    infra_failures: 0,
    preempted: 0,
    retries: 0,
    runner_degraded: false,
    ...over,
  };
}

function ev(data: Record<string, unknown>, type = CAMPAIGN_PROGRESS): FabricEvent {
  return { seq: 1, type, at: 1_760_000_000, data };
}

function seeded() {
  const client = new QueryClient();
  const list: CampaignList = { campaigns: [campaign(ID), campaign(OTHER)], count: 2 };
  client.setQueryData(benchKeys.list, list);
  client.setQueryData(benchKeys.detail(ID), campaign(ID));
  client.setQueryData(benchKeys.status(ID), status(ID));
  return client;
}

const progress = (over: Record<string, unknown> = {}) => ({
  campaign_id: ID,
  state: "active",
  finished_reason: null,
  counts: { planned: 5, submitted: 2, done: 3, failed: 0, skipped: 0 },
  done_percent: 30,
  eta_s: 84,
  infra_failure_rate: 0.01,
  ...over,
});

describe("applyCampaignEvent", () => {
  it("is in the shell's appliers", () => {
    expect(EVENT_APPLIERS).toContain(applyCampaignEvent);
  });

  it("ignores other events", () => {
    const client = seeded();
    const before = client.getQueryData(benchKeys.list);
    expect(applyCampaignEvent(client, ev(progress(), "job.state"))).toBe(false);
    expect(client.getQueryData(benchKeys.list)).toBe(before);
  });

  it("patches the list row, the detail and the status in place", () => {
    const client = seeded();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    expect(applyCampaignEvent(client, ev(progress()))).toBe(true);
    const list = client.getQueryData<CampaignList>(benchKeys.list);
    expect(list?.campaigns[0]?.counts).toEqual({ planned: 5, submitted: 2, done: 3, failed: 0, skipped: 0 });
    expect(list?.campaigns[1]?.counts).toEqual(COUNTS); // another campaign untouched
    expect(client.getQueryData<CampaignDetail>(benchKeys.detail(ID))?.counts.done).toBe(3);
    const s = client.getQueryData<CampaignStatus>(benchKeys.status(ID));
    expect(s?.done_percent).toBe(30);
    expect(s?.eta_s).toBe(84);
    expect(s?.infra_failure_rate).toBe(0.01);
    expect(s?.records).toBe(2); // not in the event: kept
    expect(invalidate).not.toHaveBeenCalled(); // all patched, nothing refetched
  });

  it("a pause moves the state everywhere", () => {
    const client = seeded();
    applyCampaignEvent(client, ev(progress({ state: "paused" })));
    expect(client.getQueryData<CampaignList>(benchKeys.list)?.campaigns[0]?.state).toBe("paused");
    expect(client.getQueryData<CampaignDetail>(benchKeys.detail(ID))?.state).toBe("paused");
    expect(client.getQueryData<CampaignStatus>(benchKeys.status(ID))?.state).toBe("paused");
  });

  it("a final state refetches the detail and the status", () => {
    const client = seeded();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyCampaignEvent(
      client,
      ev(progress({ state: "done", finished_reason: "complete", counts: { planned: 0, submitted: 0, done: 10, failed: 0, skipped: 0 }, eta_s: null })),
    );
    expect(client.getQueryData<CampaignStatus>(benchKeys.status(ID))?.finished_reason).toBe("complete");
    expect(client.getQueryData<CampaignStatus>(benchKeys.status(ID))?.eta_s).toBeNull();
    const keys = invalidate.mock.calls.map((c) => c[0]?.queryKey);
    expect(keys).toContainEqual(benchKeys.detail(ID));
    expect(keys).toContainEqual(benchKeys.status(ID));
  });

  it("an unknown campaign refetches the list (and patches nothing)", () => {
    const client = seeded();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    applyCampaignEvent(client, ev(progress({ campaign_id: "camp_01J00000000000000000000NEW" })));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: benchKeys.list, exact: true });
    expect(client.getQueryData<CampaignList>(benchKeys.list)?.campaigns[0]?.counts).toEqual(COUNTS);
  });

  it("nothing cached: nothing written, no throw", () => {
    const client = new QueryClient();
    expect(applyCampaignEvent(client, ev(progress()))).toBe(true);
    expect(client.getQueryData(benchKeys.list)).toBeUndefined();
    expect(client.getQueryData(benchKeys.status(ID))).toBeUndefined();
  });

  it("bad fields are never written; a bad event changes nothing", () => {
    const client = seeded();
    applyCampaignEvent(
      client,
      ev({
        campaign_id: ID,
        state: "exploded",
        counts: { planned: -1, submitted: 2, done: 3, failed: 0, skipped: 0 },
        done_percent: 140,
        eta_s: "soon",
        infra_failure_rate: 7,
      }),
    );
    expect(client.getQueryData<CampaignDetail>(benchKeys.detail(ID))).toEqual(campaign(ID));
    expect(client.getQueryData<CampaignStatus>(benchKeys.status(ID))).toEqual(status(ID));
    expect(applyCampaignEvent(client, ev({ state: "paused" }))).toBe(true); // no campaign_id
    expect(applyCampaignEvent(client, ev({ campaign_id: 7 }))).toBe(true);
  });

  it("an odd cache entry is left alone", () => {
    const client = new QueryClient();
    client.setQueryData(benchKeys.list, { campaigns: [null, 3, { campaign_id: ID }] });
    client.setQueryData(benchKeys.status(ID), "not a status");
    expect(() => applyCampaignEvent(client, ev(progress()))).not.toThrow();
    expect(client.getQueryData(benchKeys.status(ID))).toBe("not a status");
  });
});

describe("parseProgress / parseCounts", () => {
  it("keeps exactly the valid fields", () => {
    expect(parseProgress({ campaign_id: ID, eta_s: null, infra_failure_rate: null })).toEqual({
      campaignId: ID,
      etaS: null,
      infraFailureRate: null,
    });
    expect(parseProgress({ campaign_id: ID, finished_reason: "budget", done_percent: 100 })).toEqual({
      campaignId: ID,
      finishedReason: "budget",
      donePercent: 100,
    });
    expect(parseProgress({ campaign_id: ID, finished_reason: "nope", done_percent: Number.NaN })).toEqual({ campaignId: ID });
    expect(parseProgress({ campaign_id: "" })).toBeUndefined();
  });

  it("counts need all five non-negative integers", () => {
    expect(parseCounts(COUNTS)).toEqual(COUNTS);
    expect(parseCounts({ ...COUNTS, done: 1.5 })).toBeUndefined();
    expect(parseCounts({ planned: 1 })).toBeUndefined();
    expect(parseCounts([1, 2])).toBeUndefined();
    expect(parseCounts(null)).toBeUndefined();
  });
});
