/** The demo Settings answers: every list the pages read, consistent with the demo session (demo mode only). */
import { describe, expect, it } from "vitest";

import type { Health } from "../api/fabric";
import type { InputList, InviteList, SecretList, TokenList } from "../api/settings";
import { DEMO_NODES } from "./demoData";
import { DEMO_SESSION_TOKEN_ID, demoSettingsRoute } from "./settingsDemoData";

const BASE = "http://localhost:5173";

function get(path: string): unknown {
  const out = demoSettingsRoute(new URL(path, BASE));
  if (out === undefined) throw new Error(`no demo answer for ${path}`);
  return out;
}

describe("demo settings", () => {
  it("the controller is healthy with one session per connected laptop", () => {
    const health = get("/healthz") as Health;
    expect(health.status).toBe("ok");
    expect(health.sessions).toBe(DEMO_NODES.filter((n) => n.state !== "offline").length);
  });

  it("tokens: the admin's own include this browser session; ?all=true adds other users'", () => {
    const mine = get("/v1/auth/tokens") as TokenList;
    expect(mine.count).toBe(mine.tokens.length);
    expect(new Set(mine.tokens.map((t) => t.user))).toEqual(new Set(["admin"]));
    const current = mine.tokens.filter((t) => t.current);
    expect(current.map((t) => t.id)).toEqual([DEMO_SESSION_TOKEN_ID]);
    expect(new Set(mine.tokens.map((t) => t.state))).toEqual(new Set(["active", "expired", "revoked"]));
    const all = get("/v1/auth/tokens?all=true") as TokenList;
    expect(all.tokens.length).toBeGreaterThan(mine.tokens.length);
    expect(new Set(all.tokens.map((t) => t.user)).size).toBeGreaterThan(1);
    const created = all.tokens.map((t) => Date.parse(t.created_at));
    expect([...created].sort((a, b) => b - a)).toEqual(created); // newest first
  });

  it("secrets carry names and times only; inputs and invites are listed with every state", () => {
    const secrets = get("/v1/secrets") as SecretList;
    expect(secrets.secrets.length).toBeGreaterThan(3);
    for (const s of secrets.secrets) expect(Object.keys(s).sort()).toEqual(["created_at", "name", "updated_at"]);
    const inputs = get("/v1/data") as InputList;
    expect(inputs.count).toBe(inputs.inputs.length);
    for (const i of inputs.inputs) {
      expect(i.ref).toBe(`${i.name}@${String(i.version)}`);
      expect(i.tree_id).toMatch(/^sha256:[0-9a-f]{64}$/);
    }
    const invites = get("/v1/fabric/invites") as InviteList;
    expect(new Set(invites.invites.map((i) => i.state))).toEqual(new Set(["pending", "used", "expired", "revoked"]));
    const ids = new Set(DEMO_NODES.map((n) => n.id));
    for (const i of invites.invites.filter((x) => x.state === "used")) expect(ids.has(i.used_by_node ?? "")).toBe(true);
  });

  it("anything else is not a settings answer", () => {
    expect(demoSettingsRoute(new URL("/v1/nodes", BASE))).toBeUndefined();
  });
});
