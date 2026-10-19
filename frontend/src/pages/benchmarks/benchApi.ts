/**
 * The controller routes the Benchmarks pages use (V0.9 G6, 0.9.9, D221),
 * typed from schema.d.ts. All are admin-only on the server (a campaign is job
 * submission at scale); the page never calls them for a user.
 *
 *   GET  /v1/bench/campaigns                      list (run counts per state)
 *   GET  /v1/bench/campaigns/{id}                 one, with its spec
 *   GET  /v1/bench/campaigns/{id}/status          done %, ETA, infra failure rate
 *   GET  /v1/bench/campaigns/{id}/runs            runs in seq order (keyset `after`)
 *   GET  /v1/bench/campaigns/{id}/labels          per-group labels (JSON | parquet)
 *   POST /v1/bench/campaigns/{id}/{start|pause|resume|cancel}
 *   GET  /v1/telemetry/export?campaign=           the run records (Parquet)
 *
 * The Quality report (G5c) has its own module, `qualityApi.ts`.
 */
import { apiDownload, apiRequest } from "../../api/client";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type CampaignOut = Schemas["CampaignOut"];
export type CampaignList = Schemas["CampaignList"];
export type CampaignDetail = Schemas["CampaignDetail"];
export type CampaignState = CampaignOut["state"];
export type CampaignStatus = Schemas["CampaignStatusOut"];
export type CampaignControlOut = Schemas["CampaignControlOut"];
export type RunCounts = Schemas["RunCounts"];
export type RunOut = Schemas["RunOut"];
export type RunList = Schemas["RunList"];
export type RunState = RunOut["state"];
export type LabelsOut = Schemas["LabelsOut"];
export type GroupLabel = Schemas["GroupLabelOut"];
export type ConfigLabel = Schemas["ConfigLabelOut"];
export type MetricStats = Schemas["MetricStats"];

export const CAMPAIGN_STATES: readonly CampaignState[] = ["active", "paused", "done", "cancelled"];
export const RUN_STATES: readonly RunState[] = ["planned", "submitted", "done", "failed", "skipped"];

export function isCampaignState(value: unknown): value is CampaignState {
  return typeof value === "string" && (CAMPAIGN_STATES as readonly string[]).includes(value);
}

/** Campaigns per list request (the server allows up to its MAX_LIST). */
export const CAMPAIGN_LIST_LIMIT = 100;
/** Runs per page of the Runs tab. */
export const RUN_PAGE_SIZE = 100;

const campaignPath = (campaignId: string) => `/v1/bench/campaigns/${encodeURIComponent(campaignId)}`;

export function listCampaigns(signal?: AbortSignal): Promise<CampaignList> {
  return apiRequest<CampaignList>("/v1/bench/campaigns", { signal, query: { limit: CAMPAIGN_LIST_LIMIT } });
}

export function getCampaign(campaignId: string, signal?: AbortSignal): Promise<CampaignDetail> {
  return apiRequest<CampaignDetail>(campaignPath(campaignId), { signal });
}

export function getCampaignStatus(campaignId: string, signal?: AbortSignal): Promise<CampaignStatus> {
  return apiRequest<CampaignStatus>(`${campaignPath(campaignId)}/status`, { signal });
}

/** One page of runs after `after` (the last seq seen; 0 = from the start). */
export function listRuns(
  campaignId: string,
  after: number,
  state: RunState | null,
  signal?: AbortSignal,
): Promise<RunList> {
  return apiRequest<RunList>(`${campaignPath(campaignId)}/runs`, {
    signal,
    query: { after, limit: RUN_PAGE_SIZE, state: state ?? undefined },
  });
}

export function getLabels(campaignId: string, signal?: AbortSignal): Promise<LabelsOut> {
  return apiRequest<LabelsOut>(`${campaignPath(campaignId)}/labels`, { signal });
}

export type CampaignVerb = "start" | "pause" | "resume" | "cancel";

/** `POST .../{verb}`: idempotent on the server; a refusal is a typed ApiError (409 conflict ...). */
export function controlCampaign(campaignId: string, verb: CampaignVerb): Promise<CampaignControlOut> {
  return apiRequest<CampaignControlOut>(`${campaignPath(campaignId)}/${verb}`, { method: "POST" });
}

export const PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet";

/** The campaign's run records as Parquet (`GET /v1/telemetry/export?campaign=`). */
export function downloadRecords(campaignId: string, signal?: AbortSignal): Promise<Blob> {
  return apiDownload("/v1/telemetry/export", { signal, query: { campaign: campaignId } }, PARQUET_MEDIA_TYPE);
}

/** The campaign's labels table as Parquet (`GET .../labels?format=parquet`). */
export function downloadLabels(campaignId: string, signal?: AbortSignal): Promise<Blob> {
  return apiDownload(`${campaignPath(campaignId)}/labels`, { signal, query: { format: "parquet" } }, PARQUET_MEDIA_TYPE);
}
