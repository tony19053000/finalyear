/**
 * The local console's API client (`/console/v1`, V0.17 G3).
 *
 * Same origin as the page (http://127.0.0.1:47130); the session is the
 * HttpOnly `mf_console` cookie, so nothing secret is ever readable here. The
 * browser sends `Origin` on every POST/PUT/DELETE, which is the agent's CSRF
 * check. Failures are `{"error": {code, message, fix}}` -> `ConsoleApiError`.
 */
import { ApiError, NetworkError } from "../api/errors";
import type { components } from "./schema";

type Schemas = components["schemas"];
export type StatusView = Schemas["StatusView"];
export type PolicyView = Schemas["PolicyView"];
export type PolicyValues = Schemas["PolicyValues"];
export type CheckView = Schemas["CheckView"];
export type ChecksView = Schemas["ChecksView"];
export type ControllerView = Schemas["ControllerView"];
export type DiscoverView = Schemas["DiscoverView"];
export type JoinView = Schemas["JoinView"];
export type NodeNameView = Schemas["NodeNameView"];
export type LogsView = Schemas["LogsView"];
export type LogLineView = Schemas["LogLineView"];
export type DockerAccessView = Schemas["DockerAccessView"];
export type OkView = Schemas["OkView"];

export const API = "/console/v1";

/** A console refusal: `code` is stable, `message` + `fix` are plain words. */
export class ConsoleApiError extends ApiError {
  readonly fix: string;
  constructor(status: number, code: string, message: string, fix: string) {
    super(status, code, message, { detail: { fix } });
    this.fix = fix;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? { accept: "application/json" } : { accept: "application/json", "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (err) {
    throw new NetworkError(
      `MeshForge on this laptop did not answer (${err instanceof Error ? err.message : String(err)}). It may be restarting.`,
    );
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = isRecord(data) && isRecord(data.error) ? data.error : {};
    throw new ConsoleApiError(
      response.status,
      typeof error.code === "string" ? error.code : `http_${String(response.status)}`,
      typeof error.message === "string" ? error.message : `The console answered ${String(response.status)}.`,
      typeof error.fix === "string" ? error.fix : "",
    );
  }
  return data as T;
}

export const consoleApi = {
  exchange: (credential: { code?: string; token?: string }) => request<OkView>("POST", "/session", credential),
  signOut: () => request<OkView>("DELETE", "/session"),
  status: () => request<StatusView>("GET", "/status"),
  discover: () => request<DiscoverView>("GET", "/discover"),
  join: (code: string, controllerHost: string | null) =>
    request<JoinView>("POST", "/join", { code, controller_host: controllerHost }),
  setupComplete: () => request<OkView>("POST", "/setup-complete"),
  policy: () => request<PolicyView>("GET", "/policy"),
  savePolicy: (values: PolicyValues) => request<PolicyView>("PUT", "/policy", { values }),
  nodeName: () => request<NodeNameView>("GET", "/node-name"),
  saveNodeName: (name: string) => request<NodeNameView>("PUT", "/node-name", { name }),
  checks: () => request<ChecksView>("GET", "/checks"),
  dockerAccess: () => request<DockerAccessView>("GET", "/docker-access"),
  logs: (limit: number, after = 0) => request<LogsView>("GET", `/logs?limit=${String(limit)}&after=${String(after)}`),
  restart: () => request<OkView>("POST", "/restart"),
  leave: () => request<OkView>("POST", "/leave", { confirm: "leave" }),
};

export const consoleKeys = {
  status: ["console", "status"] as const,
  policy: ["console", "policy"] as const,
  nodeName: ["console", "node-name"] as const,
  checks: ["console", "checks"] as const,
  logs: ["console", "logs"] as const,
  discover: ["console", "discover"] as const,
};

/** Plain message + fix of any error thrown by `consoleApi`. */
export function explain(error: unknown): { message: string; fix: string; code: string | undefined } {
  if (error instanceof ConsoleApiError) return { message: error.message, fix: error.fix, code: error.code };
  if (error instanceof Error) return { message: error.message, fix: "", code: undefined };
  return { message: String(error), fix: "", code: undefined };
}
