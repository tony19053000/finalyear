/**
 * V0.8 G5: Settings pages (0.8.6 + D176 rest). Every value shown comes from
 * the fake controller's answer (checklist 26); sensitive values - a join code,
 * a new API token, a password, a secret value - never reach the query or
 * mutation cache and are gone from the DOM once their dialog closes
 * (checklist 25); destructive actions go through a confirm dialog; admin
 * sections are hidden from a user and never called for one.
 */
import { QueryClient } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "../api/queryKeys";
import { PageLoadBoundary, PreloadPages } from "../app/App";
import { qrModules, qrPath, QR_MARGIN } from "../components/qr";
import { applySettingsEvent } from "../pages/settings/applySettingsEvent";
import { ADMIN_SESSION, USER_SESSION, event, fakeController, json, node, renderApp, type Handler } from "./fakeFabric";

const CODE = "mf1-aebag-aaaab-6c3ab-zz2qa-qaaaa-aaaaa-aaaaa-aaaab-aaaaa-aaaaa-aaaaa-aaaaa-7777a";
const TOKEN = "mft_0123456789abcdefghijklmnopqrstuvwxyzABCDEF";
const PASSWORD = "correct horse battery staple";
const SECRET_VALUE = "s3cr3t-value-never-shown";

beforeEach(() => {
  document.cookie = "mf_csrf=csrf-g5";
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function token(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    user_id: "usr_admin",
    user: "root",
    name: "laptop",
    state: "active",
    current: false,
    created_at: "2026-10-01T10:00:00+00:00",
    expires_at: "2026-12-01T10:00:00+00:00",
    last_used_at: "2026-10-15T10:00:00+00:00",
    revoked_at: null,
    kind: "api",
    idle_expires_at: null,
    ...overrides,
  };
}

function invite(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    state: "pending",
    created_by: "user:root",
    created_at: "2026-10-16T10:00:00+00:00",
    expires_at: "2026-10-16T10:15:00+00:00",
    used_at: null,
    used_by_node: null,
    revoked_at: null,
    replace_node_id: null,
    ...overrides,
  };
}

function input(name: string, version: number, overrides: Record<string, unknown> = {}) {
  return {
    name,
    version,
    ref: `${name}@${String(version)}`,
    tree_id: `sha256:${"a".repeat(64)}`,
    size: 2048,
    files: 3,
    pinned: false,
    created_at: "2026-10-10T10:00:00+00:00",
    ...overrides,
  };
}

function routes(session: typeof ADMIN_SESSION, extra: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    "GET /v1/session": () => json(200, { session }),
    "GET /v1/nodes": () => json(200, { count: 1, nodes: [node("node_A", { hostname: "lab-a" })] }),
    "GET /v1/auth/whoami": () =>
      json(200, { user: session.user, legacy: false, token_id: "tok_1", token_expires_at: session.expires_at }),
    "GET /healthz": () => json(200, { status: "ok", sessions: 2, degraded: null }),
    ...extra,
  };
}

/** Everything the app keeps in TanStack Query (queries + mutations), as text. */
function cacheDump(client: QueryClient): string {
  const queries = client.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]);
  const mutations = client.getMutationCache().getAll().map((m) => [m.state.variables, m.state.data]);
  return JSON.stringify({ queries, mutations });
}

function header(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name);
}

/* ------------------------------------------------------------ navigation */

describe("Settings navigation", () => {
  it("an admin sees every section; the Account page shows whoami, session and /healthz", async () => {
    fakeController(routes(ADMIN_SESSION));
    renderApp("/settings");
    const nav = await screen.findByRole("navigation", { name: "Settings" });
    expect(within(nav).getAllByRole("link").map((l) => l.textContent)).toEqual([
      "Account",
      "API tokens",
      "Secrets",
      "Data",
      "Invites",
      "Users",
    ]);
    const account = await screen.findByTestId("account-card");
    await within(account).findByText("tok_1");
    const controller = await screen.findByTestId("controller-card");
    expect(await within(controller).findByText("ok")).toBeTruthy();
    expect(within(controller).getByText("2")).toBeTruthy(); // agent sessions = the API's number
    expect(within(controller).getByText(window.location.origin)).toBeTruthy();
    expect(screen.getByTestId("owner-policy-card").textContent).toContain("meshforge-agent policy");
  });

  it("a user sees no admin sections, and a direct visit calls no admin route", async () => {
    const fake = fakeController(routes(USER_SESSION));
    renderApp("/settings/invites");
    expect(await screen.findByText("Invites are made by admins")).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Settings" });
    expect(within(nav).queryByRole("link", { name: "Invites" })).toBeNull();
    expect(within(nav).queryByRole("link", { name: "Users" })).toBeNull();
    expect(fake.count("GET /v1/fabric/invites")).toBe(0);
    expect(screen.queryByRole("button", { name: "Create invite" })).toBeNull();
  });

  it("users page: an admin is told where users are managed (no users API exists), nothing invented", async () => {
    const fake = fakeController(routes(ADMIN_SESSION));
    renderApp("/settings/users");
    const card = await screen.findByTestId("users-card");
    expect(card.textContent).toContain("meshforge-controller user create NAME");
    expect(within(card).queryByRole("table")).toBeNull();
    expect(fake.calls.some((c) => c.key.includes("/v1/users"))).toBe(false);
  });
});

/* ------------------------------------------------------------ tokens */

describe("API tokens", () => {
  it("lists what the API returns; an admin can show every user's", async () => {
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/auth/tokens": (_init, url) =>
          url.includes("all=true")
            ? json(200, { count: 2, tokens: [token("tok_A"), token("tok_B", { user: "alice", name: "ci" })] })
            : json(200, {
                count: 2,
                tokens: [
                  token("tok_A"),
                  token("tok_1", { name: "browser", kind: "browser_session", current: true, idle_expires_at: "2026-10-16T20:00:00+00:00" }),
                ],
              }),
      }),
    );
    renderApp("/settings/tokens");
    const table = await screen.findByRole("table", { name: "Your API tokens and browser sessions" });
    expect(within(table).getByText("tok_A")).toBeTruthy();
    expect(within(table).getByText("Browser session")).toBeTruthy();
    expect(within(table).getByText("this session")).toBeTruthy();
    expect(within(table).queryByText("alice")).toBeNull();

    await userEvent.click(screen.getByRole("checkbox", { name: "Every user" }));
    const all = await screen.findByRole("table", { name: "Every user's API tokens and browser sessions" });
    expect(within(all).getByText("alice")).toBeTruthy();
    expect(fake.calls.some((c) => c.url.includes("/v1/auth/tokens?all=true"))).toBe(true);
  });

  it("revoke goes through a confirm dialog, sends the CSRF header, and refetches", async () => {
    let revoked = false;
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/auth/tokens": () => json(200, { count: 1, tokens: [token("tok_A", revoked ? { state: "revoked" } : {})] }),
        "DELETE /v1/auth/tokens/tok_A": () => {
          revoked = true;
          return json(200, { ...token("tok_A", { state: "revoked" }), already_revoked: false });
        },
      }),
    );
    renderApp("/settings/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "Revoke laptop (tok_A)" }));
    const dialog = await screen.findByRole("dialog");
    expect(fake.count("DELETE /v1/auth/tokens/tok_A")).toBe(0); // nothing before the confirm
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    expect(await screen.findByText("laptop revoked")).toBeTruthy();
    const del = fake.calls.find((c) => c.key === "DELETE /v1/auth/tokens/tok_A");
    expect(del && header(del.init, "X-MeshForge-CSRF")).toBe("csrf-g5");
    expect(await screen.findByText("revoked")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Revoke laptop (tok_A)" })).toBeNull();
  });

  it("a refused revoke is shown in the dialog, which stays open", async () => {
    fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/auth/tokens": () => json(200, { count: 1, tokens: [token("tok_A")] }),
        "DELETE /v1/auth/tokens/tok_A": () => json(404, { error: { code: "not_found", message: "no such token" } }),
      }),
    );
    renderApp("/settings/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "Revoke laptop (tok_A)" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("no such token");
  });

  it("create: asks the password again, shows the token once, keeps it out of every cache", async () => {
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/auth/tokens": () => json(200, { count: 0, tokens: [] }),
        "POST /v1/auth/login": () =>
          json(200, { token: TOKEN, token_id: "tok_NEW", expires_at: "2026-11-16T00:00:00+00:00", user: ADMIN_SESSION.user }),
      }),
    );
    const app = renderApp("/settings/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "Create API token" }));
    const dialog = await screen.findByRole("dialog");
    const name = within(dialog).getByRole("textbox", { name: "Token name" });
    await userEvent.clear(name);
    await userEvent.type(name, "ci-runner");
    await userEvent.type(within(dialog).getByLabelText(/Your password/), PASSWORD);
    await userEvent.click(within(dialog).getByRole("button", { name: "Create token" }));

    const shown = await within(dialog).findByTestId("new-token");
    expect(shown.textContent).toContain(TOKEN);
    expect(shown.textContent).toContain("tok_NEW");
    const login = fake.calls.find((c) => c.key === "POST /v1/auth/login");
    expect(JSON.parse(login?.init.body as string)).toEqual({ name: "root", password: PASSWORD, token_name: "ci-runner" });
    expect(cacheDump(app.queryClient)).not.toContain(TOKEN);
    expect(cacheDump(app.queryClient)).not.toContain(PASSWORD);

    await userEvent.click(within(shown).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(document.body.textContent).not.toContain(TOKEN);
    });
    expect(document.body.innerHTML).not.toContain(PASSWORD);
    expect(cacheDump(app.queryClient)).not.toContain(TOKEN);
    expect(fake.count("GET /v1/auth/tokens")).toBeGreaterThan(1); // the list was refetched
  });

  it("a wrong password is an error in the dialog, never a sign-out; the field is cleared", async () => {
    fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/auth/tokens": () => json(200, { count: 0, tokens: [] }),
        "POST /v1/auth/login": () => json(401, { error: { code: "unauthorized", message: "wrong name or password" } }),
      }),
    );
    renderApp("/settings/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "Create API token" }));
    const dialog = await screen.findByRole("dialog");
    const password = within(dialog).getByLabelText(/Your password/);
    await userEvent.type(password, "nope");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create token" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("wrong name or password");
    expect((password as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("heading", { name: "Sign in" })).toBeNull();
    expect(screen.getByTestId("current-user")).toBeTruthy();
  });
});

/* ------------------------------------------------------------ invites */

describe("Invites", () => {
  it("create: TTL + re-key choice reach the API; the code is shown once with a QR of exactly that string", async () => {
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/fabric/invites": () => json(200, { invites: [invite("inv_1")] }),
        "POST /v1/fabric/invites": () =>
          json(201, {
            code: CODE,
            invite_id: "inv_NEW",
            address: "10.0.0.1:47100",
            ca_pin: "c0ffee",
            expires_at: "2026-10-16T11:00:00+00:00",
            replace_node_id: "node_A",
          }),
      }),
    );
    const app = renderApp("/settings/invites");
    expect(await screen.findByText("inv_1")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Create invite" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "Code expires after" }), "1 hour");
    await userEvent.selectOptions(within(dialog).getByRole("combobox", { name: "For" }), "node_A");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create invite" }));

    const issued = await within(dialog).findByTestId("issued-invite");
    expect(issued.textContent).toContain(CODE);
    expect(issued.textContent).toContain("10.0.0.1:47100");
    const post = fake.calls.find((c) => c.key === "POST /v1/fabric/invites");
    expect(JSON.parse(post?.init.body as string)).toEqual({ ttl_s: 3600, replace_node_id: "node_A" });
    expect(post && header(post.init, "X-MeshForge-CSRF")).toBe("csrf-g5");

    // The QR is one path drawn from the matrix of exactly the code string.
    const qr = within(issued).getByRole("img", { name: "QR code of the join code" });
    const modules = qrModules(CODE);
    expect(qr.getAttribute("data-modules")).toBe(String(modules.length));
    expect(qr.querySelector("path")?.getAttribute("d")).toBe(qrPath(modules));
    expect(qr.outerHTML).not.toMatch(/style=|<image|data:/);

    expect(cacheDump(app.queryClient)).not.toContain(CODE);
    await userEvent.click(within(issued).getByRole("button", { name: "Done" }));
    await waitFor(() => {
      expect(document.body.textContent).not.toContain(CODE);
    });
    expect(screen.queryByRole("img", { name: "QR code of the join code" })).toBeNull();
    expect(cacheDump(app.queryClient)).not.toContain(CODE);
  });

  it("only a pending invite can be revoked, after a confirm", async () => {
    let state = "pending";
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/fabric/invites": () =>
          json(200, { invites: [invite("inv_1", { state }), invite("inv_2", { state: "used", used_by_node: "node_A" })] }),
        "POST /v1/fabric/invites/inv_1/revoke": () => {
          state = "revoked";
          return json(200, invite("inv_1", { state: "revoked" }));
        },
      }),
    );
    renderApp("/settings/invites");
    expect(await screen.findByText("inv_2")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Revoke invite inv_2" })).toBeNull();
    expect(screen.getByRole("link", { name: "node_A" }).getAttribute("href")).toBe("/nodes/node_A");
    await userEvent.click(screen.getByRole("button", { name: "Revoke invite inv_1" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke invite" }));
    expect(await screen.findByText("Invite inv_1 revoked")).toBeTruthy();
    expect(fake.count("POST /v1/fabric/invites/inv_1/revoke")).toBe(1);
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Revoke invite inv_1" })).toBeNull();
    });
  });

  it("an API refusal (no fabric CA) is shown, nothing invented", async () => {
    fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/fabric/invites": () => json(200, { invites: [] }),
        "POST /v1/fabric/invites": () =>
          json(409, { error: { code: "conflict", message: "this controller has no fabric CA: run `meshforge-controller fabric create`" } }),
      }),
    );
    renderApp("/settings/invites");
    expect(await screen.findByText("No invites yet. Create one to add a laptop to the fabric.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Create invite" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create invite" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("no fabric CA");
    expect(within(dialog).queryByTestId("issued-invite")).toBeNull();
  });

  it("node.status events refetch a mounted invite list (an invite was used)", async () => {
    let used = false;
    const fake = fakeController(
      routes(ADMIN_SESSION, {
        "GET /v1/fabric/invites": () =>
          json(200, { invites: [invite("inv_1", used ? { state: "used", used_by_node: "node_B" } : {})] }),
      }),
    );
    const app = renderApp("/settings/invites");
    expect(await screen.findByText("pending")).toBeTruthy();
    app.socket().greet();
    used = true;
    act(() => {
      app.socket().send(event("node.status", { node_id: "node_B", state: "online", connected: true }));
    });
    expect(await screen.findByText("used")).toBeTruthy();
    expect(fake.count("GET /v1/fabric/invites")).toBe(2);
  });
});

describe("applySettingsEvent", () => {
  it("invalidates only the invite list, only on node.status", () => {
    const client = new QueryClient();
    client.setQueryData(queryKeys.invites, { invites: [] });
    client.setQueryData(queryKeys.secrets, { secrets: [], count: 0 });
    expect(applySettingsEvent(client, event("job.state", { job_id: "j" }))).toBe(false);
    expect(client.getQueryState(queryKeys.invites)?.isInvalidated).toBe(false);
    expect(applySettingsEvent(client, event("node.status", { node_id: "n" }))).toBe(true);
    expect(client.getQueryState(queryKeys.invites)?.isInvalidated).toBe(true);
    expect(client.getQueryState(queryKeys.secrets)?.isInvalidated).toBe(false);
  });
});

/* ------------------------------------------------------------ secrets */

describe("Secrets", () => {
  it("set: the name is normalised like the controller, the value goes raw in the body and is then gone", async () => {
    let names: string[] = [];
    const fake = fakeController(
      routes(USER_SESSION, {
        "GET /v1/secrets": () =>
          json(200, {
            count: names.length,
            secrets: names.map((n) => ({ name: n, created_at: "2026-10-16T10:00:00+00:00", updated_at: "2026-10-16T10:00:00+00:00" })),
          }),
        "PUT /v1/secrets/API_KEY": () => {
          names = ["API_KEY"];
          return json(200, { name: "API_KEY", created: true });
        },
      }),
    );
    const app = renderApp("/settings/secrets");
    expect(await screen.findByText("No secrets yet.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "api_key");
    expect(within(dialog).getByText("Stored as API_KEY.")).toBeTruthy();
    const value = within(dialog).getByLabelText("Value");
    expect(value.getAttribute("type")).toBe("password");
    await userEvent.type(value, SECRET_VALUE);
    await userEvent.click(within(dialog).getByRole("button", { name: "Store secret" }));

    expect(await screen.findByText("Secret API_KEY stored")).toBeTruthy();
    const put = fake.calls.find((c) => c.key === "PUT /v1/secrets/API_KEY");
    expect(new TextDecoder().decode(put?.init.body as Uint8Array)).toBe(SECRET_VALUE);
    expect(put && header(put.init, "Content-Type")).toBe("application/octet-stream");
    expect(put && header(put.init, "X-MeshForge-CSRF")).toBe("csrf-g5");
    expect(await screen.findByRole("cell", { name: "API_KEY" })).toBeTruthy();
    expect(document.body.innerHTML).not.toContain(SECRET_VALUE);
    expect(cacheDump(app.queryClient)).not.toContain(SECRET_VALUE);
  });

  it("a value read from a file is sent byte-for-byte (BOM, CRLF, NUL, invalid UTF-8 kept) - fix r1 T-1", async () => {
    const payload = new Uint8Array([0xef, 0xbb, 0xbf, 0x41, 0x0d, 0x0a, 0x00, 0xff, 0xfe, 0x80, 0x42]);
    const fake = fakeController(
      routes(USER_SESSION, {
        "GET /v1/secrets": () => json(200, { count: 0, secrets: [] }),
        "PUT /v1/secrets/BLOB": () => json(200, { name: "BLOB", created: true }),
      }),
    );
    const app = renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "BLOB");
    await userEvent.upload(within(dialog).getByLabelText(/read the value from a file/), new File([payload], "blob.bin"));
    expect(await within(dialog).findByText(/11 bytes, sent exactly as in the file/)).toBeTruthy();
    await userEvent.click(within(dialog).getByRole("button", { name: "Store secret" }));
    expect(await screen.findByText("Secret BLOB stored")).toBeTruthy();
    const put = fake.calls.find((c) => c.key === "PUT /v1/secrets/BLOB");
    expect(Array.from(put?.init.body as Uint8Array)).toEqual(Array.from(payload));
    expect(cacheDump(app.queryClient)).not.toContain("BOM");
  });

  it("a slow file read that ends after a newer choice is dropped (D204, V0.8 G7)", async () => {
    const fake = fakeController(
      routes(USER_SESSION, {
        "GET /v1/secrets": () => json(200, { count: 0, secrets: [] }),
        "PUT /v1/secrets/SLOW": () => json(200, { name: "SLOW", created: true }),
      }),
    );
    renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "SLOW");
    const picker = within(dialog).getByLabelText(/read the value from a file/);
    /** A file whose bytes arrive only when the test says so. */
    const slowFile = (name: string, text: string) => {
      const file = new File([text], name);
      let release: () => void = () => undefined;
      const gate = new Promise<void>((resolve) => {
        release = resolve;
      });
      Object.defineProperty(file, "arrayBuffer", {
        value: async () => {
          await gate;
          return new TextEncoder().encode(text).buffer;
        },
      });
      return { file, release };
    };

    // 1. old.bin is slow; new.bin is chosen after it and read first.
    const old = slowFile("old.bin", "OLD-VALUE");
    await userEvent.upload(picker, old.file);
    await userEvent.upload(picker, new File(["NEW-VALUE"], "new.bin"));
    expect(await within(dialog).findByText(/Value read from new\.bin/)).toBeTruthy();
    await act(async () => {
      old.release();
      await Promise.resolve();
    });
    expect(within(dialog).queryByText(/old\.bin/)).toBeNull();

    // 2. typing beats a read still in flight.
    const late = slowFile("late.bin", "LATE-VALUE");
    await userEvent.upload(picker, late.file);
    await userEvent.type(within(dialog).getByLabelText("Value"), "TYPED-VALUE");
    await act(async () => {
      late.release();
      await Promise.resolve();
    });
    expect(within(dialog).queryByText(/late\.bin/)).toBeNull();

    await userEvent.click(within(dialog).getByRole("button", { name: "Store secret" }));
    expect(await screen.findByText("Secret SLOW stored")).toBeTruthy();
    const puts = fake.calls.filter((c) => c.key === "PUT /v1/secrets/SLOW");
    expect(puts.length).toBe(1);
    expect(new TextDecoder().decode(puts[0]?.init.body as Uint8Array)).toBe("TYPED-VALUE");
  });

  it("an empty file says why Store is disabled (D204)", async () => {
    fakeController(routes(USER_SESSION, { "GET /v1/secrets": () => json(200, { count: 0, secrets: [] }) }));
    renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "EMPTY");
    await userEvent.upload(within(dialog).getByLabelText(/read the value from a file/), new File([], "empty.bin"));
    expect(await within(dialog).findByText(/empty\.bin is empty - a secret needs at least 4 bytes/)).toBeTruthy();
    expect(within(dialog).getByRole<HTMLButtonElement>("button", { name: "Store secret" }).disabled).toBe(true);
  });

  it("refuses what the controller would refuse before sending: bad name, value under 4 bytes", async () => {
    const fake = fakeController(routes(USER_SESSION, { "GET /v1/secrets": () => json(200, { count: 0, secrets: [] }) }));
    renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    const name = within(dialog).getByRole("textbox", { name: "Name" });
    await userEvent.type(name, "9bad");
    expect(name.getAttribute("aria-invalid")).toBe("true");
    await userEvent.clear(name);
    await userEvent.type(name, "OK_NAME");
    await userEvent.type(within(dialog).getByLabelText("Value"), "abc");
    expect(within(dialog).getByText(/At least 4 bytes/)).toBeTruthy();
    expect(within(dialog).getByRole<HTMLButtonElement>("button", { name: "Store secret" }).disabled).toBe(true);
    expect(fake.calls.filter((c) => c.key.startsWith("PUT")).length).toBe(0);
  });

  it("replacing says so; a failed store clears the value and shows the reason", async () => {
    fakeController(
      routes(USER_SESSION, {
        "GET /v1/secrets": () =>
          json(200, { count: 1, secrets: [{ name: "DB_PASS", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" }] }),
        "PUT /v1/secrets/DB_PASS": () => json(503, { error: { code: "network_error", message: "the secret store is unavailable; retry" } }),
      }),
    );
    renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Set a secret" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: "Name" }), "DB_PASS");
    expect(within(dialog).getByText("DB_PASS already exists: its value will be replaced.")).toBeTruthy();
    const value = within(dialog).getByLabelText<HTMLInputElement>("Value");
    await userEvent.type(value, SECRET_VALUE);
    await userEvent.click(within(dialog).getByRole("button", { name: "Replace secret" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("unavailable");
    expect(value.value).toBe("");
  });

  it("delete needs a confirm", async () => {
    const fake = fakeController(
      routes(USER_SESSION, {
        "GET /v1/secrets": () =>
          json(200, { count: 1, secrets: [{ name: "DB_PASS", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" }] }),
        "DELETE /v1/secrets/DB_PASS": () => json(200, { name: "DB_PASS", deleted: true }),
      }),
    );
    renderApp("/settings/secrets");
    await userEvent.click(await screen.findByRole("button", { name: "Delete secret DB_PASS" }));
    expect(fake.count("DELETE /v1/secrets/DB_PASS")).toBe(0);
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete secret" }));
    expect(await screen.findByText("Secret DB_PASS deleted")).toBeTruthy();
    expect(fake.count("DELETE /v1/secrets/DB_PASS")).toBe(1);
  });
});

/* ------------------------------------------------------------ data */

describe("Data", () => {
  it("lists every version as the API returns it; pin sends {pinned:true}", async () => {
    let pinned = false;
    const fake = fakeController(
      routes(USER_SESSION, {
        "GET /v1/data": () => json(200, { count: 2, inputs: [input("mnist", 1, { pinned }), input("mnist", 2, { size: 5 * 1024 ** 2, files: 7 })] }),
        "PATCH /v1/data/mnist/1": () => {
          pinned = true;
          return json(200, input("mnist", 1, { pinned: true }));
        },
      }),
    );
    renderApp("/settings/data");
    const table = await screen.findByRole("table", { name: "Named inputs (datasets, models), every version" });
    const row = within(table).getByText("mnist@2").closest("tr");
    expect(row && within(row).getByText("5.0 MiB")).toBeTruthy();
    expect(row && within(row).getByText("7")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Pin mnist@1" }));
    expect(await screen.findByText("mnist@1 pinned")).toBeTruthy();
    const patch = fake.calls.find((c) => c.key === "PATCH /v1/data/mnist/1");
    expect(JSON.parse(patch?.init.body as string)).toEqual({ pinned: true });
    expect(await screen.findByRole("button", { name: "Unpin mnist@1" })).toBeTruthy();
  });

  it("delete: a version in use (409) is refused in the dialog with the API's reason", async () => {
    fakeController(
      routes(USER_SESSION, {
        "GET /v1/data": () => json(200, { count: 1, inputs: [input("mnist", 1)] }),
        "DELETE /v1/data/mnist/1": () => json(409, { error: { code: "conflict", message: "mnist@1 is used by unfinished job job_X" } }),
      }),
    );
    renderApp("/settings/data");
    await userEvent.click(await screen.findByRole("button", { name: "Delete mnist@1" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete version" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("job_X");
  });
});

/* ------------------------------------------------------------ code splitting */

describe("a page chunk that cannot be loaded (item 0f)", () => {
  it("shows what failed and a reload, never a blank page", () => {
    const failing = (): never => {
      throw new TypeError("Failed to fetch dynamically imported module");
    };
    const Failing = () => <>{failing()}</>;
    vi.spyOn(console, "error").mockImplementation(() => undefined); // React reports the caught error
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    render(
      <PageLoadBoundary>
        <Failing />
      </PageLoadBoundary>,
    );
    expect(screen.getByRole("alert").textContent).toContain("This page could not be loaded");
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  });
});

describe("PreloadPages (D204, V0.8 G7)", () => {
  it("a failed preload is one warning naming the page, never an error", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const error = vi.spyOn(console, "error");
    const loaded = vi.fn(() => Promise.resolve({}));
    render(
      <PreloadPages
        loaders={{
          jobs: () => Promise.reject(new TypeError("Failed to fetch dynamically imported module")),
          nodes: loaded,
        }}
      />,
    );
    await waitFor(() => {
      expect(warn).toHaveBeenCalledTimes(1);
    });
    expect(warn.mock.calls[0]).toEqual([
      "page preload failed; it loads again when opened",
      "jobs",
      "Failed to fetch dynamically imported module",
    ]);
    expect(loaded).toHaveBeenCalledTimes(1);
    expect(error).not.toHaveBeenCalled();
  });
});

/* ------------------------------------------------------------ QR */

describe("QR matrix", () => {
  it("draws one square per dark module, offset by the quiet zone", () => {
    const modules = qrModules(CODE);
    expect(modules.length).toBeGreaterThanOrEqual(21);
    expect((modules.length - 17) % 4).toBe(0); // a real QR size: 17 + 4 * version
    const dark = modules.flat().filter(Boolean).length;
    const path = qrPath(modules);
    expect(path.match(/M/g)?.length).toBe(dark);
    expect(path.startsWith(`M${String(QR_MARGIN)} ${String(QR_MARGIN)}h1v1h-1z`)).toBe(true); // finder corner is dark
  });

  it("refuses non-ASCII rather than mis-encoding it", () => {
    expect(() => qrModules("mf1-ä")).toThrow(/ASCII/);
  });
});
