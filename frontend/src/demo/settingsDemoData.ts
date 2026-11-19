/**
 * DEMO DATA (localhost `?demo=1` only - see demo/install.ts) for the Settings
 * pages: the controller's health (`GET /healthz`), API tokens and browser
 * sessions (`GET /v1/auth/tokens`, `?all=true` for every user's), secrets
 * (names and times only, `GET /v1/secrets`), named inputs (`GET /v1/data`)
 * and invites (`GET /v1/fabric/invites`). Read-only: create / revoke / pin /
 * delete still go to the real controller (and fail there without a session).
 * Times are relative to now, so the page always looks current.
 */
import type { Health } from "../api/fabric";
import type { InputList, InputOut, InviteList, InviteOut, SecretList, TokenList, TokenOut } from "../api/settings";
import { DEMO_NODES } from "./demoData";

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const MiB = 1024 ** 2;
const GiB = 1024 ** 3;

function at(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString();
}

/** The demo admin's own browser session: whoami's `token_id` and the list's `current` row. */
export const DEMO_SESSION_TOKEN_ID = "tok_01JD8Q4M7R2N5W9X3C6V0B1K4H";

const ADMIN = { user: "admin", user_id: "usr_demo_admin" };
const OTHERS = {
  asha: { user: "asha", user_id: "usr_01JC2V7K9P4M8Q1R5T3W6X0Y2Z" },
  ravi: { user: "ravi", user_id: "usr_01JC2VB3D6F8H1K4M7N9Q2R5T8" },
};

interface DemoToken {
  id: string;
  who: { user: string; user_id: string };
  name: string;
  kind: "api" | "browser_session";
  state: "active" | "expired" | "revoked";
  /** ms before now */
  created: number;
  /** ms before now, or null (never used) */
  used: number | null;
  /** ms from now (negative: in the past) */
  expires: number;
  /** ms from now: a browser session's idle end */
  idle?: number;
  /** ms before now */
  revoked?: number;
  current?: boolean;
}

const TOKENS: readonly DemoToken[] = [
  { id: DEMO_SESSION_TOKEN_ID, who: ADMIN, name: "dashboard", kind: "browser_session", state: "active", created: 42 * MIN, used: 4_000, expires: DAY - 42 * MIN, idle: HOUR, current: true },
  { id: "tok_01JD7ZP3K8M1Q6R9T2V5W8X0Y3", who: ADMIN, name: "dashboard", kind: "browser_session", state: "active", created: 19 * HOUR, used: 2 * HOUR + 11 * MIN, expires: 5 * HOUR, idle: 38 * MIN },
  { id: "tok_01JCZ4F7H0K3M6P9R2T5V8X1Z4", who: ADMIN, name: "asha-laptop", kind: "api", state: "active", created: 12 * DAY + 3 * HOUR, used: 9 * MIN, expires: 18 * DAY },
  { id: "tok_01JCWB8D1G4J7L0N3Q6S9U2W5Y", who: ADMIN, name: "ci-runner", kind: "api", state: "active", created: 21 * DAY, used: 47 * MIN, expires: 9 * DAY - 4 * HOUR },
  { id: "tok_01JD3M6P9S2V5Y8B1E4H7K0N3Q", who: ADMIN, name: "lab-pc-07 notebook", kind: "api", state: "active", created: 5 * DAY + 6 * HOUR, used: 3 * DAY, expires: 25 * DAY },
  { id: "tok_01JC8T1W4Z7C0F3J6M9P2S5V8Y", who: ADMIN, name: "bench-sweep", kind: "api", state: "expired", created: 34 * DAY, used: 6 * DAY, expires: -4 * DAY },
  { id: "tok_01JCQ2E5H8K1N4Q7T0W3Z6C9F2", who: ADMIN, name: "old-desktop", kind: "api", state: "revoked", created: 27 * DAY, used: 15 * DAY, expires: 3 * DAY, revoked: 14 * DAY },
  { id: "tok_01JD5R8U1X4A7D0G3J6M9Q2T5W", who: OTHERS.asha, name: "dashboard", kind: "browser_session", state: "active", created: 3 * HOUR, used: 6 * MIN, expires: 21 * HOUR, idle: 54 * MIN },
  { id: "tok_01JCX9B2E5H8L1P4S7V0Y3B6E9", who: OTHERS.asha, name: "asha-laptop", kind: "api", state: "active", created: 16 * DAY, used: 22 * MIN, expires: 14 * DAY },
  { id: "tok_01JD1F4J7N0R3U6X9A2D5G8K1N", who: OTHERS.ravi, name: "ravi-thinkpad", kind: "api", state: "active", created: 8 * DAY, used: 5 * HOUR, expires: 22 * DAY },
  { id: "tok_01JCM7Q0T3W6Z9C2F5J8M1Q4T7", who: OTHERS.ravi, name: "colab", kind: "api", state: "expired", created: 40 * DAY, used: 31 * DAY, expires: -10 * DAY },
];

function tokenOut(t: DemoToken): TokenOut {
  return {
    id: t.id,
    user_id: t.who.user_id,
    user: t.who.user,
    name: t.name,
    kind: t.kind,
    state: t.state,
    current: t.current === true,
    created_at: at(-t.created),
    last_used_at: t.used === null ? null : at(-t.used),
    expires_at: at(t.expires),
    idle_expires_at: t.idle === undefined ? null : at(t.idle),
    revoked_at: t.revoked === undefined ? null : at(-t.revoked),
  };
}

/** `GET /v1/auth/tokens`: the admin's own, or (`all`) every user's - newest first, as the controller lists them. */
export function demoTokens(all: boolean): TokenList {
  const tokens = TOKENS.filter((t) => all || t.who === ADMIN)
    .map(tokenOut)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  return { count: tokens.length, tokens };
}

/** `GET /v1/secrets`: names and times, never a value. */
export function demoSecrets(): SecretList {
  const rows: [string, number, number][] = [
    ["AWS_ACCESS_KEY_ID", 30 * DAY, 30 * DAY],
    ["AWS_SECRET_ACCESS_KEY", 30 * DAY, 2 * DAY + 5 * HOUR],
    ["DB_PASS", 18 * DAY, 18 * DAY],
    ["GITHUB_DEPLOY_KEY", 9 * DAY + 2 * HOUR, 9 * DAY + 2 * HOUR],
    ["HF_TOKEN", 26 * DAY, 4 * HOUR + 12 * MIN],
    ["WANDB_API_KEY", 21 * DAY, 11 * DAY],
  ];
  const secrets = rows.map(([name, created, updated]) => ({ name, created_at: at(-created), updated_at: at(-updated) }));
  return { count: secrets.length, secrets };
}

/** A stable fake content address for a demo input version. */
function treeId(seed: string): string {
  let h = 2166136261;
  let out = "";
  while (out.length < 64) {
    for (const ch of `${seed}:${String(out.length)}`) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return `sha256:${out.slice(0, 64)}`;
}

/** `GET /v1/data`: every version of every named input. */
export function demoInputs(): InputList {
  const rows: [string, number, number, number, boolean, number][] = [
    // name, version, files, bytes, pinned, created (ms ago)
    ["cifar10", 1, 6, 163 * MiB, false, 41 * DAY],
    ["cifar10", 2, 6, 178 * MiB, true, 12 * DAY],
    ["imagenet-subset", 1, 12_814, 6.4 * GiB, true, 33 * DAY],
    ["llama-3.2-1b", 1, 9, 2.47 * GiB, true, 19 * DAY],
    ["mnist", 1, 4, 11.6 * MiB, false, 47 * DAY],
    ["mnist", 2, 4, 11.6 * MiB, false, 28 * DAY],
    ["mnist", 3, 5, 52.4 * MiB, true, 6 * DAY],
    ["sentiment-reviews", 1, 3, 412 * MiB, false, 15 * DAY],
    ["whisper-small", 1, 7, 967 * MiB, false, 3 * DAY + 4 * HOUR],
  ];
  const inputs: InputOut[] = rows.map(([name, version, files, size, pinned, created]) => ({
    name,
    version,
    ref: `${name}@${String(version)}`,
    files,
    size: Math.round(size),
    pinned,
    tree_id: treeId(`${name}@${String(version)}`),
    created_at: at(-created),
  }));
  return { count: inputs.length, inputs };
}

/** `GET /v1/fabric/invites`: newest first, every state; codes are never listed. */
export function demoInvites(): InviteList {
  const node = (k: number) => DEMO_NODES[k]?.id ?? null;
  const rows: { id: string; state: string; created: number; ttl: number; used?: string | null; replaces?: string | null; ago?: number }[] = [
    { id: "inv_01JD8R2T5W8Z1C4F7J0M3P6S9V", state: "pending", created: 6 * MIN, ttl: 15 * MIN },
    { id: "inv_01JD8N7Q0T3W6Z9C2F5H8K1N4Q", state: "pending", created: 25 * MIN, ttl: HOUR, replaces: node(4) },
    { id: "inv_01JD6H3K6N9Q2T5W8Z1C4F7J0M", state: "expired", created: 2 * DAY + 3 * HOUR, ttl: 15 * MIN },
    { id: "inv_01JD4C8F1J4M7P0S3V6Y9B2E5H", state: "used", created: 4 * DAY + 2 * HOUR, ttl: 15 * MIN, used: node(2), ago: 4 * DAY + 2 * HOUR - 4 * MIN },
    { id: "inv_01JD2X5A8D1G4K7N0Q3T6W9Z2C", state: "revoked", created: 6 * DAY, ttl: HOUR, ago: 6 * DAY - 20 * MIN },
    { id: "inv_01JCZS1V4Y7B0E3H6L9P2S5V8Y", state: "used", created: 9 * DAY, ttl: 24 * HOUR, used: node(3), ago: 9 * DAY - 2 * HOUR },
    { id: "inv_01JCVN6R9U2X5A8D1G4J7M0Q3T", state: "used", created: 12 * DAY, ttl: 15 * MIN, used: node(1), ago: 12 * DAY - 7 * MIN },
    { id: "inv_01JCRJ2M5P8S1V4Y7B0E3H6K9N", state: "used", created: 12 * DAY + HOUR, ttl: 15 * MIN, used: node(0), ago: 12 * DAY + HOUR - 9 * MIN },
  ];
  const invites: InviteOut[] = rows.map((r) => ({
    id: r.id,
    state: r.state,
    created_by: "user:admin",
    created_at: at(-r.created),
    expires_at: at(r.ttl - r.created),
    used_at: r.state === "used" && r.ago !== undefined ? at(-r.ago) : null,
    used_by_node: r.state === "used" ? (r.used ?? null) : null,
    revoked_at: r.state === "revoked" && r.ago !== undefined ? at(-r.ago) : null,
    replace_node_id: r.replaces ?? null,
  }));
  return { invites };
}

/** `GET /healthz`: every loop healthy; one agent session per connected demo laptop. */
export function demoHealth(): Health {
  return { status: "ok", sessions: DEMO_NODES.filter((n) => n.state !== "offline").length, degraded: null };
}

/** The Settings pages' GET routes; undefined = not a demo answer. */
export function demoSettingsRoute(url: URL): unknown {
  switch (url.pathname) {
    case "/healthz":
      return demoHealth();
    case "/v1/auth/tokens":
      return demoTokens(url.searchParams.get("all") === "true");
    case "/v1/secrets":
      return demoSecrets();
    case "/v1/data":
      return demoInputs();
    case "/v1/fabric/invites":
      return demoInvites();
    default:
      return undefined;
  }
}
