/**
 * Live campaign updates for the Benchmarks pages (D172, D221): what a
 * `campaign.progress` event does to the TanStack Query cache. Pure - no
 * React, no second store; the shell runs it through `EVENT_APPLIERS`.
 *
 * `campaign.progress {campaign_id, state, finished_reason, counts,
 * done_percent, eta_s, infra_failure_rate}` (controller/bench/runner.py, at
 * most once a second per runner-owned campaign, admins only):
 *
 * - the campaign's row in the cached list and its cached detail get the new
 *   `state` and `counts` in place (no refetch of the list for one change);
 * - its cached status gets every field the event carries;
 * - (the campaign page refetches its runs / labels / quality tabs when the
 *   patched counts show a run ended - `useRefreshOnRunEnd`, the same path a
 *   poll takes);
 * - a campaign the list does not hold (created elsewhere) refetches the list;
 *   a final state (done / cancelled) refetches the detail and status so the
 *   authoritative row follows.
 *
 * Defensive by design: a field of the wrong type is ignored (never written
 * into the cache), and a cache entry of an odd shape is left alone.
 * Returns true when the event was a campaign event.
 */
import type { QueryClient } from "@tanstack/react-query";

import type { FabricEvent } from "../../events/eventStream";
import { isCampaignState, type CampaignDetail, type CampaignList, type CampaignStatus, type RunCounts } from "./benchApi";
import { benchKeys } from "./benchKeys";

export const CAMPAIGN_PROGRESS = "campaign.progress";

const COUNT_KEYS = ["planned", "submitted", "done", "failed", "skipped"] as const;
const FINISHED_REASONS = new Set(["complete", "budget", "cancelled"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
}

/** All five counts as non-negative integers, or undefined (a partial or bad object is not trusted). */
export function parseCounts(value: unknown): RunCounts | undefined {
  if (!isRecord(value)) return undefined;
  const out: Partial<Record<(typeof COUNT_KEYS)[number], number>> = {};
  for (const key of COUNT_KEYS) {
    const n = count(value[key]);
    if (n === undefined) return undefined;
    out[key] = n;
  }
  return out as RunCounts;
}

/** A finite number >= 0, `null` (the server's "unknown"), or undefined (bad / absent: not applied). */
function optionalRate(data: Record<string, unknown>, key: string): number | null | undefined {
  if (!(key in data)) return undefined;
  const value = data[key];
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export interface CampaignProgress {
  campaignId: string;
  state?: CampaignStatus["state"];
  finishedReason?: CampaignStatus["finished_reason"];
  counts?: RunCounts;
  donePercent?: number;
  etaS?: number | null;
  infraFailureRate?: number | null;
}

/** The event's usable fields (pure; unit-tested). Undefined when it names no campaign. */
export function parseProgress(data: Record<string, unknown>): CampaignProgress | undefined {
  const campaignId = data.campaign_id;
  if (typeof campaignId !== "string" || campaignId === "") return undefined;
  const out: CampaignProgress = { campaignId };
  if (isCampaignState(data.state)) out.state = data.state;
  const reason = data.finished_reason;
  if (reason === null || (typeof reason === "string" && FINISHED_REASONS.has(reason))) {
    out.finishedReason = reason as CampaignStatus["finished_reason"];
  }
  const counts = parseCounts(data.counts);
  if (counts !== undefined) out.counts = counts;
  const done = data.done_percent;
  if (typeof done === "number" && Number.isFinite(done) && done >= 0 && done <= 100) out.donePercent = done;
  const eta = optionalRate(data, "eta_s");
  if (eta !== undefined) out.etaS = eta;
  const rate = optionalRate(data, "infra_failure_rate");
  if (rate !== undefined && (rate === null || rate <= 1)) out.infraFailureRate = rate;
  return out;
}

const TERMINAL = new Set<CampaignStatus["state"]>(["done", "cancelled"]);

function ended(c: RunCounts): number {
  return c.done + c.failed + c.skipped;
}

/**
 * An event older than the cached row (a poll / refetch can land between two
 * events): a finished campaign never leaves its final state, and runs that
 * ended never un-end. Such an event changes nothing (tester r1 T-1, T-1b).
 */
function isStale(row: { state: CampaignStatus["state"]; counts: RunCounts }, p: CampaignProgress): boolean {
  if (TERMINAL.has(row.state) && p.state !== row.state) return true;
  return p.counts !== undefined && isRecord(row.counts) && ended(p.counts) < ended(row.counts);
}

function patchCampaign<T extends { state: CampaignStatus["state"]; counts: RunCounts }>(row: T, p: CampaignProgress): T {
  if (isStale(row, p)) return row;
  return { ...row, ...(p.state !== undefined && { state: p.state }), ...(p.counts !== undefined && { counts: p.counts }) };
}

function patchStatus(status: CampaignStatus, p: CampaignProgress): CampaignStatus {
  if (isStale(status, p)) return status;
  const next = patchCampaign(status, p);
  if (p.finishedReason !== undefined) next.finished_reason = p.finishedReason;
  if (p.donePercent !== undefined) next.done_percent = p.donePercent;
  if (p.etaS !== undefined) next.eta_s = p.etaS;
  if (p.infraFailureRate !== undefined) next.infra_failure_rate = p.infraFailureRate;
  return next;
}

function isList(value: unknown): value is CampaignList {
  return isRecord(value) && Array.isArray(value.campaigns);
}

function isCampaignShaped(value: unknown): value is CampaignDetail {
  return isRecord(value) && typeof value.campaign_id === "string" && isRecord(value.counts);
}

export function applyCampaignEvent(client: QueryClient, event: FabricEvent): boolean {
  if (event.type !== CAMPAIGN_PROGRESS) return false;
  const p = parseProgress(event.data);
  if (p === undefined) return true;
  const id = p.campaignId;

  const list = client.getQueryData(benchKeys.list);
  if (isList(list)) {
    const mine = (c: unknown): c is CampaignList["campaigns"][number] => isCampaignShaped(c) && c.campaign_id === id;
    if (list.campaigns.some(mine)) {
      client.setQueryData<CampaignList>(benchKeys.list, {
        ...list,
        campaigns: list.campaigns.map((c) => (mine(c) ? patchCampaign(c, p) : c)),
      });
    } else {
      void client.invalidateQueries({ queryKey: benchKeys.list, exact: true });
    }
  }
  const detail = client.getQueryData(benchKeys.detail(id));
  if (isCampaignShaped(detail)) client.setQueryData<CampaignDetail>(benchKeys.detail(id), patchCampaign(detail, p));
  const status = client.getQueryData(benchKeys.status(id));
  if (isCampaignShaped(status)) {
    client.setQueryData<CampaignStatus>(benchKeys.status(id), patchStatus(status as unknown as CampaignStatus, p));
  }

  if (p.state === "done" || p.state === "cancelled") {
    void client.invalidateQueries({ queryKey: benchKeys.detail(id), exact: true });
    void client.invalidateQueries({ queryKey: benchKeys.status(id), exact: true });
  }
  return true;
}
