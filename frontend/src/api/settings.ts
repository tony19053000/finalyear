/**
 * Calls and query hooks of the Settings pages (V0.8 G5, D176): invites, API
 * tokens, secrets, named inputs (data). Types come from schema.d.ts (D171).
 *
 * Sensitive answers - an invite's join code, a new API token - and sensitive
 * inputs - a password, a secret value - are NEVER put in the TanStack Query
 * cache: these calls are plain functions, not `useMutation` (whose cache keeps
 * `variables` and `data`). The Settings components hold them in component
 * state that is dropped when the dialog closes.
 */
import { useQuery } from "@tanstack/react-query";

import { apiRequest } from "./client";
import { queryKeys } from "./queryKeys";
import type { components } from "./schema";

type Schemas = components["schemas"];
export type InviteCreated = Schemas["InviteCreated"];
export type InviteList = Schemas["InviteList"];
export type InviteOut = Schemas["InviteOut"];
export type TokenList = Schemas["TokenList"];
export type TokenOut = Schemas["TokenOut"];
export type TokenRevoked = Schemas["TokenRevoked"];
export type LoginOut = Schemas["LoginOut"];
export type SecretList = Schemas["SecretList"];
export type SecretOut = Schemas["SecretOut"];
export type SecretStored = Schemas["SecretStored"];
export type SecretDeleted = Schemas["SecretDeleted"];
export type InputList = Schemas["InputList"];
export type InputOut = Schemas["meshforge__controller__api__response_models__InputOut"];
export type InputRemoved = Schemas["InputRemoved"];

/* ------------------------------------------------------------ the API's rules */

/** controller/enrollment/invites.py MIN_TTL_S / MAX_TTL_S. */
export const INVITE_MIN_TTL_S = 60;
export const INVITE_MAX_TTL_S = 24 * 3600;
/** The choices the invite dialog offers (the controller default is 15 min). */
export const INVITE_TTL_CHOICES: readonly { label: string; seconds: number | null }[] = [
  { label: "Controller default", seconds: null },
  { label: "5 minutes", seconds: 5 * 60 },
  { label: "15 minutes", seconds: 15 * 60 },
  { label: "1 hour", seconds: 3600 },
  { label: "24 hours", seconds: INVITE_MAX_TTL_S },
];

/** common/secret_names.py: `^[A-Z_][A-Z0-9_]{0,63}$` after strip + upper-case (ASCII only). */
export const SECRET_NAME_RE = /^[A-Z_][A-Z0-9_]{0,63}$/;
export const SECRET_MIN_BYTES = 4;
export const SECRET_MAX_BYTES = 64 * 1024;
/** store/models/auth.py MAX_TOKEN_NAME_CHARS. */
export const TOKEN_NAME_MAX_CHARS = 64;

/** The controller's own normalisation of a secret name, or null if it would refuse it. */
export function normaliseSecretName(raw: string): string | null {
  const name = raw.trim();
  // ASCII before case-folding, like the controller: no Unicode alias of a name.
  if (!/^[\x20-\x7e]*$/.test(name)) return null;
  const upper = name.toUpperCase();
  return SECRET_NAME_RE.test(upper) ? upper : null;
}

/** Bytes of a value as the controller will store it (UTF-8). */
export function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

/* ------------------------------------------------------------------ calls */

export function listInvites(signal?: AbortSignal): Promise<InviteList> {
  return apiRequest<InviteList>("/v1/fabric/invites", { signal });
}

/** `POST /v1/fabric/invites` - the code is in the answer, once. Never cache it. */
export function createInvite(options: { ttlS: number | null; replaceNodeId: string | null }): Promise<InviteCreated> {
  const body: Record<string, unknown> = {};
  if (options.ttlS !== null) body.ttl_s = options.ttlS;
  if (options.replaceNodeId !== null) body.replace_node_id = options.replaceNodeId;
  return apiRequest<InviteCreated>("/v1/fabric/invites", { method: "POST", json: body });
}

export function revokeInvite(inviteId: string): Promise<InviteOut> {
  return apiRequest<InviteOut>(`/v1/fabric/invites/${encodeURIComponent(inviteId)}/revoke`, { method: "POST" });
}

/** `GET /v1/auth/tokens` (`all`: every user's - admin only). */
export function listTokens(all: boolean, signal?: AbortSignal): Promise<TokenList> {
  return apiRequest<TokenList>("/v1/auth/tokens", { signal, query: { all: all ? "true" : undefined } });
}

export function revokeToken(tokenId: string): Promise<TokenRevoked> {
  return apiRequest<TokenRevoked>(`/v1/auth/tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
}

/**
 * A new API token for the signed-in user: the same `POST /v1/auth/login` that
 * `compute login` uses (there is no other way to mint one), so the password
 * is asked again. A wrong password is a 401 that must NOT end the browser
 * session (`skipUnauthorizedHandler`). The token is in the answer, once.
 */
export function createApiToken(name: string, password: string, tokenName: string): Promise<LoginOut> {
  return apiRequest<LoginOut>("/v1/auth/login", {
    method: "POST",
    json: { name, password, token_name: tokenName },
    skipUnauthorizedHandler: true,
  });
}

export function listSecrets(signal?: AbortSignal): Promise<SecretList> {
  return apiRequest<SecretList>("/v1/secrets", { signal });
}

/**
 * `PUT /v1/secrets/{name}` - the raw value as the body; never echoed back.
 * Typed text is sent as UTF-8; a file's bytes are sent exactly as read (no
 * decoding: a BOM, CRLF, NUL or non-UTF-8 byte reaches the job unchanged -
 * the same raw body `compute secret set` sends).
 */
export function putSecret(name: string, value: string | Uint8Array<ArrayBuffer>): Promise<SecretStored> {
  return apiRequest<SecretStored>(`/v1/secrets/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: typeof value === "string" ? new TextEncoder().encode(value) : value,
  });
}

export function deleteSecret(name: string): Promise<SecretDeleted> {
  return apiRequest<SecretDeleted>(`/v1/secrets/${encodeURIComponent(name)}`, { method: "DELETE" });
}

export function listInputs(signal?: AbortSignal): Promise<InputList> {
  return apiRequest<InputList>("/v1/data", { signal });
}

export function setInputPinned(name: string, version: number, pinned: boolean): Promise<InputOut> {
  return apiRequest<InputOut>(`/v1/data/${encodeURIComponent(name)}/${String(version)}`, {
    method: "PATCH",
    json: { pinned },
  });
}

export function removeInput(name: string, version: number): Promise<InputRemoved> {
  return apiRequest<InputRemoved>(`/v1/data/${encodeURIComponent(name)}/${String(version)}`, { method: "DELETE" });
}

/* ------------------------------------------------------------------ hooks */

/** Admin only: `enabled` false for a user (the call would be a 403). */
export function useInvites(enabled: boolean) {
  return useQuery({ queryKey: queryKeys.invites, queryFn: ({ signal }) => listInvites(signal), enabled });
}

export function useTokens(all: boolean) {
  return useQuery({ queryKey: queryKeys.tokens(all), queryFn: ({ signal }) => listTokens(all, signal) });
}

export function useSecrets() {
  return useQuery({ queryKey: queryKeys.secrets, queryFn: ({ signal }) => listSecrets(signal) });
}

export function useInputs() {
  return useQuery({ queryKey: queryKeys.inputs, queryFn: ({ signal }) => listInputs(signal) });
}
