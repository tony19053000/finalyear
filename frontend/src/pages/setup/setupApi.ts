/**
 * The controller's one-time setup API (V0.17 G2, D392): `/v1/setup/*`.
 *
 * Served only while a new controller has no fabric (plain HTTP on loopback);
 * afterwards - and on every set-up controller - each call is 410 Gone. The
 * setup token comes from the URL fragment (never sent to a server as part of a
 * URL) and is swapped for an HttpOnly cookie by `openSession`. No CSRF cookie
 * exists in setup mode: the server checks JSON + Origin + Host instead.
 */
import { apiRequest } from "../../api/client";
import { ApiError } from "../../api/errors";

export interface SetupStateOut {
  state: string;
  session: boolean;
}

export interface DoctorCheck {
  name: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  hint: string;
}

export interface SetupInfo {
  state: string;
  hostname: string;
  enroll_address: string | null;
  address_problem: string | null;
  dashboard_url: string;
  loopback_only: boolean;
  mdns: boolean;
  config_file: string;
  checks: { ok: boolean; checks: DoctorCheck[] } | null;
}

export interface IssuedInvite {
  code: string;
  invite_id: string;
  address: string;
  ca_pin: string;
  expires_at: string;
}

export interface SetupResult {
  admin: string;
  fabric_id: string;
  ca_fingerprint: string;
  join_fingerprint: string;
  invite: IssuedInvite | null;
  invite_problem: string | null;
  dashboard_url: string;
  restarting: boolean;
}

const quiet = { skipUnauthorizedHandler: true } as const;

export function getSetupState(): Promise<SetupStateOut> {
  return apiRequest<SetupStateOut>("/v1/setup/state", quiet);
}

export function openSession(token: string): Promise<SetupStateOut> {
  return apiRequest<SetupStateOut>("/v1/setup/session", {
    ...quiet,
    method: "POST",
    json: { token },
    csrfOptional: true,
  });
}

export function getSetupInfo(): Promise<SetupInfo> {
  return apiRequest<SetupInfo>("/v1/setup/info", quiet);
}

export function completeSetup(admin: string, password: string): Promise<SetupResult> {
  return apiRequest<SetupResult>("/v1/setup/complete", {
    ...quiet,
    method: "POST",
    json: { admin, password },
    csrfOptional: true,
  });
}

/** True for "this controller is already set up" (410 Gone). */
export function isGone(error: unknown): boolean {
  return error instanceof ApiError && error.status === 410;
}

/** The token in `#token=...` (or `#t=...`), if any. */
export function tokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const value = params.get("token") ?? params.get("t");
  return value !== null && value.trim() !== "" ? value.trim() : null;
}

/** The first-admin password rule the controller enforces (shown, pre-checked). */
export const ADMIN_MIN_CHARS = 12;
export const ADMIN_MIN_DISTINCT = 6;
/** The "not containing the user name" rule applies to names this long or longer. */
export const NAME_RULE_MIN_CHARS = 3;

/** A message for a password the controller would refuse, or null. */
export function passwordProblem(admin: string, password: string, repeat: string): string | null {
  if (password.length < ADMIN_MIN_CHARS) return `Use at least ${String(ADMIN_MIN_CHARS)} characters.`;
  if (new Set(password).size < ADMIN_MIN_DISTINCT) {
    return `Use at least ${String(ADMIN_MIN_DISTINCT)} different characters.`;
  }
  const name = admin.trim().toLowerCase();
  if (name.length >= NAME_RULE_MIN_CHARS && password.toLowerCase().includes(name)) {
    return "The password must not contain the user name.";
  }
  if (password !== repeat) return "The two passwords are not the same.";
  return null;
}
