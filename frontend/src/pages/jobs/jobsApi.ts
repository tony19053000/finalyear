/**
 * The controller routes the Jobs pages use (V0.8 G4), typed from schema.d.ts.
 *
 * JSON calls go through the shared `apiRequest` (cookie auth, CSRF header,
 * typed errors, 401 -> login). Two kinds of call need what `apiRequest` does
 * not offer (G3 owns client.ts; see the G4 progress file): a custom header
 * (`Idempotency-Key` on submit, the `X-MeshForge-*` part headers) and a
 * binary body (blob uploads). `rawRequest` below does exactly that with the
 * same rules: same-origin cookie, CSRF header from `mf_csrf`, `/v1/` paths
 * only, the error envelope -> `ApiError`, no answer -> `NetworkError`.
 */
import { CSRF_COOKIE, CSRF_HEADER, apiRequest, readCookie, type Method } from "../../api/client";
import { ApiError, NetworkError, errorFromResponse } from "../../api/errors";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type JobSummary = Schemas["JobSummary"];
export type JobList = Schemas["JobList"];
export type JobDetail = Schemas["JobDetail"];
export type JobState = Schemas["JobState"];
export type AttemptSummary = Schemas["AttemptSummary"];
export type TransitionOut = Schemas["TransitionOut"];
export type JobCancelled = Schemas["JobCancelled"];
export type JobCreated = Schemas["JobCreated"];
export type DecisionList = Schemas["DecisionList"];
export type DecisionOut = Schemas["DecisionOut"];
export type CandidateOut = Schemas["CandidateOut"];
export type PolicyName = Schemas["PolicyName"];
export type BlobMissing = Schemas["BlobMissing"];
export type BlobParts = Schemas["BlobParts"];
export type JobInput = JobDetail["inputs"][number];

/** Every job state, in lifecycle order (filters, validation of events). */
export const JOB_STATES: readonly JobState[] = [
  "submitted",
  "planning",
  "queued",
  "preparing",
  "running",
  "collecting",
  "retrying",
  "succeeded",
  "failed",
  "cancelled",
];

/** common/states.py JOB_TERMINAL_STATES. */
export const TERMINAL_JOB_STATES: ReadonlySet<string> = new Set(["succeeded", "failed", "cancelled"]);

export const POLICIES: readonly PolicyName[] = [
  "cost_model",
  "least_loaded",
  "strongest_suitable",
  "resource_score",
  "cache_affinity",
  "round_robin",
  "random",
];

export function isJobState(value: unknown): value is JobState {
  return typeof value === "string" && (JOB_STATES as readonly string[]).includes(value);
}

/** The list filters of D174 (all optional; empty = no filter). */
export interface JobFilters {
  states: JobState[];
  owner: string;
  node: string;
  /** ISO 8601 with a zone, or "". */
  createdAfter: string;
  createdBefore: string;
}

export const NO_FILTERS: JobFilters = { states: [], owner: "", node: "", createdAfter: "", createdBefore: "" };

/** Jobs per page (the server's default page size, D174). */
export const JOB_PAGE_SIZE = 50;

/** `GET /v1/jobs` - one keyset page, newest first. */
export function listJobs(filters: JobFilters, cursor: string | null, signal?: AbortSignal): Promise<JobList> {
  return apiRequest<JobList>("/v1/jobs", {
    signal,
    query: {
      limit: JOB_PAGE_SIZE,
      state: filters.states.length > 0 ? filters.states : undefined,
      owner: filters.owner || undefined,
      node: filters.node || undefined,
      created_after: filters.createdAfter || undefined,
      created_before: filters.createdBefore || undefined,
      cursor: cursor ?? undefined,
    },
  });
}

/** `GET /v1/jobs/{id}`. */
export function getJob(jobId: string, signal?: AbortSignal): Promise<JobDetail> {
  return apiRequest<JobDetail>(`/v1/jobs/${encodeURIComponent(jobId)}`, { signal });
}

/** `GET /v1/jobs/{id}/decisions` - what the planner saw and chose. */
export function getDecisions(jobId: string, signal?: AbortSignal): Promise<DecisionList> {
  return apiRequest<DecisionList>(`/v1/jobs/${encodeURIComponent(jobId)}/decisions`, { signal });
}

/** `POST /v1/jobs/{id}/cancel` (idempotent: 202 accepted, 200 already ended). */
export function cancelJob(jobId: string): Promise<JobCancelled> {
  return apiRequest<JobCancelled>(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

/** Where the browser downloads a stored blob (a job's output archive). */
export function blobUrl(digest: string): string {
  return `/v1/blobs/${digest.replace(/^sha256:/, "")}`;
}

/** Where the browser downloads a job's persisted log (plain text). */
export function logDownloadUrl(jobId: string, attemptId?: string): string {
  const query = attemptId === undefined ? "" : `&attempt=${encodeURIComponent(attemptId)}`;
  return `/v1/jobs/${encodeURIComponent(jobId)}/logs?stream=both${query}`;
}

/* ------------------------------------------------------------- raw calls */

export interface RawRequestOptions {
  method: Method;
  headers?: Record<string, string>;
  body?: BodyInit;
  signal?: AbortSignal;
}

/**
 * A call with custom headers / a binary body (see the module comment).
 * Resolves with the parsed JSON body (undefined when empty).
 */
export async function rawRequest<T>(path: string, options: RawRequestOptions): Promise<T> {
  if (!path.startsWith("/v1/")) throw new Error(`API paths start with /v1/ (got ${path})`);
  const headers = new Headers({ Accept: "application/json", ...options.headers });
  if (options.method !== "GET" && options.method !== "HEAD") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf !== undefined) headers.set(CSRF_HEADER, csrf);
  }
  let response: Response;
  try {
    response = await fetch(path, {
      method: options.method,
      headers,
      body: options.body,
      credentials: "same-origin",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new NetworkError("The controller did not answer.");
  }
  const text = await response.text();
  let parsed: unknown = undefined;
  if (text !== "") {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
  }
  if (!response.ok) throw errorFromResponse(response.status, response.headers, parsed);
  if (text !== "" && parsed === undefined) {
    throw new ApiError(response.status, "invalid_response", "The controller sent a body that is not JSON.");
  }
  return parsed as T;
}

/** The body of `POST /v1/jobs` (job_models.JobSubmission). */
export interface JobSubmissionBody {
  manifest: Record<string, unknown>;
  tree_id: string;
  placement: { node_id?: string; policy?: PolicyName };
}

/** `POST /v1/jobs` with an `Idempotency-Key` (a retry returns the same job). */
export function submitJob(body: JobSubmissionBody, idempotencyKey: string, signal?: AbortSignal): Promise<JobCreated> {
  return rawRequest<JobCreated>("/v1/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify(body),
    signal,
  });
}
