/** The demo campaigns agree with themselves across every Benchmarks endpoint (demo mode only). */
import { describe, expect, it } from "vitest";

import { bestSpeedUp, formatFactor } from "../pages/benchmarks/campaignView";
import { parseQuality, recordBuckets } from "../pages/benchmarks/qualityApi";
import {
  demoCampaignDetail,
  demoCampaignList,
  demoCampaignStatus,
  demoControl,
  demoLabels,
  demoMovingCampaigns,
  demoParquet,
  demoQuality,
  demoRuns,
} from "./benchDemoData";

const list = demoCampaignList();
const byName = (name: string) => {
  const c = list.campaigns.find((x) => x.name === name);
  if (c === undefined) throw new Error(`no demo campaign ${name}`);
  return c;
};

describe("demo benchmarks", () => {
  it("lists finished, running, queued and cancelled campaigns, newest first", () => {
    expect(list.count).toBe(list.campaigns.length);
    const created = list.campaigns.map((c) => Date.parse(c.created_at));
    expect([...created].sort((a, b) => b - a)).toEqual(created);
    const states = new Set(list.campaigns.map((c) => c.state));
    expect(states).toEqual(new Set(["active", "done", "cancelled"]));
    expect(byName("gpu-memory-sweep").counts.planned).toBe(byName("gpu-memory-sweep").run_count);
    expect(demoMovingCampaigns()).toEqual([byName("overnight-mixed-sweep").campaign_id]);
  });

  it("carries the measured headline numbers", () => {
    const factor = (name: string) => {
      const labels = demoLabels(byName(name).campaign_id);
      return labels === undefined ? null : formatFactor(bestSpeedUp(labels)?.factor);
    };
    expect(factor("cpu-split-2-laptops")).toBe("1.97");
    expect(factor("cpu-split-4-laptops")).toBe("3.21");
    expect(factor("text-ai-3-gpus")).toBe("3.19");
    expect(factor("real-data-ai-2-gpus")).toBe("2.10");
    expect(factor("windows-build-split")).toBe("3.42");
    const plan = byName("planner-check-432");
    expect(plan.run_count).toBe(432);
    expect(plan.counts).toMatchObject({ done: 432, failed: 0 });
  });

  it("every campaign answers every endpoint, and the numbers add up", () => {
    for (const c of list.campaigns) {
      const id = c.campaign_id;
      expect(demoCampaignDetail(id)?.spec).toContain(`name: ${c.name}`);
      const status = demoCampaignStatus(id);
      expect(status?.counts).toEqual(c.counts);
      // runs: keyset pages cover every run once, in seq order
      const seqs: number[] = [];
      let after = 0;
      for (;;) {
        const page = demoRuns(id, after, 100, null);
        if (page === undefined) throw new Error("no runs");
        seqs.push(...page.runs.map((r) => r.seq));
        if (page.next_after == null) break;
        after = page.next_after;
      }
      expect(seqs).toEqual(Array.from({ length: c.run_count }, (_, i) => i + 1));
      expect(demoRuns(id, 0, 1000, "failed")?.count).toBe(c.counts.failed);
      // quality: a valid v1 report whose buckets add up to its records
      const quality = parseQuality(demoQuality(id));
      expect(quality.kind).toBe("report");
      if (quality.kind === "report") {
        const sum = recordBuckets(quality.report.summary).reduce((a, b) => a + b.count, 0);
        expect(sum).toBe(quality.report.summary.records);
        expect(quality.report.summary.runs_by_state).toEqual(c.counts);
      }
      // labels: the counts match the groups
      const labels = demoLabels(id);
      const counted = labels === undefined ? 0 : Object.values(labels.counts).reduce((a, b) => a + b, 0);
      expect(counted).toBe(labels?.groups.length);
      // downloads pass the page's completeness check (PAR1 ... PAR1)
      for (const what of ["records", "labels"] as const) {
        const text = demoParquet(id, what) ?? "";
        expect(text.startsWith("PAR1") && text.endsWith("PAR1")).toBe(true);
      }
    }
  });

  it("controls move a campaign's state and refuse what the server would refuse", () => {
    const done = byName("planner-check-432").campaign_id;
    expect(demoControl(done, "cancel")).toMatchObject({ ok: false, status: 409 });
    const queued = byName("gpu-memory-sweep").campaign_id;
    expect(demoControl(queued, "start")).toMatchObject({ ok: true, body: { state: "active", runner_owned: true } });
    expect(demoControl(queued, "pause")).toMatchObject({ ok: true, body: { state: "paused" } });
    expect(demoControl(queued, "resume")).toMatchObject({ ok: true, body: { state: "active" } });
    expect(demoControl(queued, "cancel")).toMatchObject({ ok: true, body: { state: "cancelled" } });
    expect(demoControl("camp_unknown", "start")).toBeUndefined();
  });
});
