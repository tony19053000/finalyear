/**
 * Hand-written calls over the generated types (D171): one function per
 * controller route the dashboard uses. Types come from schema.d.ts, so a
 * backend contract change breaks `npm run typecheck`, not the running UI.
 */
import { apiRequest } from "./client";
import type { components } from "./schema";

type Schemas = components["schemas"];
export type SessionOut = Schemas["SessionOut"];
export type LogoutOut = Schemas["LogoutOut"];
export type WhoAmI = Schemas["WhoAmI"];
export type NodeList = Schemas["NodeList"];
export type NodeSummary = Schemas["NodeSummary"];

/** `POST /v1/session` - name + password -> the session cookie (D170). */
export function openSession(name: string, password: string): Promise<SessionOut> {
  return apiRequest<SessionOut>("/v1/session", {
    method: "POST",
    json: { name, password },
    skipUnauthorizedHandler: true,
  });
}

/** `GET /v1/session` - the cookie's session; 401 = not logged in. */
export function getSession(signal?: AbortSignal): Promise<SessionOut> {
  return apiRequest<SessionOut>("/v1/session", { signal, skipUnauthorizedHandler: true });
}

/** `DELETE /v1/session` - revoke server-side and clear both cookies. */
export function closeSession(): Promise<LogoutOut> {
  return apiRequest<LogoutOut>("/v1/session", { method: "DELETE", skipUnauthorizedHandler: true });
}

/** `GET /v1/auth/whoami`. */
export function getWhoAmI(signal?: AbortSignal): Promise<WhoAmI> {
  return apiRequest<WhoAmI>("/v1/auth/whoami", { signal });
}

/** `GET /v1/nodes` (default limit 500). */
export function listNodes(signal?: AbortSignal): Promise<NodeList> {
  return apiRequest<NodeList>("/v1/nodes", { signal });
}
