// V0.17 G3: the agent's local console - pure logic and the wizard against a fake agent.
import { QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PolicyView, StatusView } from "../agentConsole/api";
import { clearFragment, readCredential } from "../agentConsole/bootstrap";
import { ConsoleApp } from "../agentConsole/ConsoleApp";
import { connectionSummary, firstStep, joinCodeIn, nodeNameProblem } from "../agentConsole/logic";
import { SAFE_DEFAULTS, initialState, toValues } from "../agentConsole/policyLogic";

const POLICY: PolicyView = {
  policy: {
    accept_jobs: true,
    accept_gpu_sessions: true,
    max_ram_bytes: 8 * 1024 ** 3,
    max_cpu_percent: 80,
    max_vram_bytes: null,
    allow_unsandboxed: false,
    allow_network: false,
    allowed_users: [],
    pause_benchmarks: false,
  },
  max_cpu_jobs: 2,
  max_gpu_jobs: null,
  live_keys: [],
  invalid: null,
  deprecated_keys: [],
  restart_needed: [],
  owner_activity_guard: true,
  cpu_threads: 8,
};

function status(over: Partial<StatusView> = {}): StatusView {
  return {
    version: "0.17.0",
    install_mode: "system",
    phase: "waiting_for_join",
    phase_since: 0,
    stop_reason: null,
    joined: false,
    setup_complete: false,
    node_id: null,
    node_name: "",
    hostname: "lab-a",
    fabric_id: null,
    cert_not_after: null,
    identity_problem: null,
    connection: null,
    controller: null,
    session_id: null,
    dashboard_url: null,
    ...over,
  };
}

describe("bootstrap: the launch fragment", () => {
  it("reads #c= only (never a token), refuses junk", () => {
    expect(readCredential("#c=abc_DEF-1")).toEqual({ code: "abc_DEF-1" });
    expect(readCredential("#t=tok")).toBeNull();
    expect(readCredential("")).toBeNull();
    expect(readCredential("#c=<script>")).toBeNull();
    expect(readCredential(`#c=${"a".repeat(300)}`)).toBeNull();
  });

  it("clears the fragment without a new history entry", () => {
    const replaceState = vi.fn();
    clearFragment({
      history: { replaceState } as unknown as History,
      location: { pathname: "/", search: "?x=1" } as Location,
    });
    expect(replaceState).toHaveBeenCalledWith(null, "", "/?x=1");
  });
});

describe("logic", () => {
  it("the wizard resumes at the policy step once joined (Q26)", () => {
    expect(firstStep({ joined: false })).toBe("welcome");
    expect(firstStep({ joined: true })).toBe("policy");
  });

  it("finds the join code in a scanned QR text", () => {
    expect(joinCodeIn("  mf1-abcd-efgh-1234 ")).toBe("mf1-abcd-efgh-1234");
    expect(joinCodeIn("join with mf1-xyz please")).toBe("mf1-xyz");
    expect(joinCodeIn("https://example.org")).toBeNull();
  });

  it("counts the node name in code points, like the server", () => {
    expect(nodeNameProblem("😀".repeat(64))).toBeNull(); // 128 UTF-16 units, 64 code points
    expect(nodeNameProblem("😀".repeat(65))).toMatch(/65 characters/);
    expect(nodeNameProblem(`  ${"a".repeat(64)}  `)).toBeNull(); // trimmed first
  });

  it("says plainly whether the laptop is connected", () => {
    expect(connectionSummary(status({ phase: "running", connection: "ONLINE" })).tone).toBe("ok");
    expect(connectionSummary(status({ phase: "running", connection: "BACKOFF" })).label).toMatch(/Looking/);
    expect(connectionSummary(status({ phase: "stopped" })).tone).toBe("failed");
    expect(connectionSummary(status()).label).toBe("Not in a fabric");
  });
});

describe("policy form logic", () => {
  it("the wizard starts from the safe defaults (Q7): no jobs, no GPU, 50 % CPU", () => {
    const form = initialState(POLICY, true);
    expect(form.acceptJobs).toBe(false);
    expect(form.gpuJobs).toBe(false);
    expect(form.gpuSessions).toBe(false);
    expect(form.cpuPercent).toBe("50");
    const out = toValues(form);
    expect("values" in out && out.values).toMatchObject({
      accept_jobs: SAFE_DEFAULTS.accept_jobs,
      max_cpu_percent: SAFE_DEFAULTS.max_cpu_percent,
      max_gpu_jobs: 0,
      accept_gpu_sessions: false,
    });
  });

  it("Settings start from the current policy and round-trip it", () => {
    const form = initialState(POLICY, false);
    expect(form).toMatchObject({ acceptJobs: true, cpuPercent: "80", ramGib: "8", cpuJobs: "2", gpuJobs: true });
    const out = toValues(form);
    expect("values" in out && out.values).toMatchObject({
      accept_jobs: true,
      max_cpu_percent: 80,
      max_ram_bytes: 8 * 1024 ** 3,
      max_cpu_jobs: 2,
      max_gpu_jobs: null,
      max_vram_bytes: null,
    });
  });

  it("refuses out-of-range numbers with a sentence per field", () => {
    const form = { ...initialState(POLICY, false), cpuPercent: "150", ramGib: "-1", cpuJobs: "" };
    const out = toValues(form);
    expect("errors" in out && Object.keys(out.errors).sort()).toEqual(["cpuJobs", "cpuPercent", "ramGib"]);
  });
});

/* ------------------------------------------------------------------ fake agent */

type Handler = (body: unknown) => { status?: number; json: unknown };

function fakeAgent(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://127.0.0.1:47130");
    const method = init?.method ?? "GET";
    const path = url.pathname.replace("/console/v1", "");
    const body: unknown = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    const handler = routes[`${method} ${path}`];
    if (handler === undefined) {
      return Promise.resolve(new Response(JSON.stringify({ error: { code: "not_found", message: "no", fix: "" } }), { status: 404 }));
    }
    const out = handler(body);
    return Promise.resolve(new Response(JSON.stringify(out.json), { status: out.status ?? 200 }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false }, mutations: { retry: false } } });
}

describe("ConsoleApp", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("exchanges the launch code once, drops it from the URL, then shows the wizard", async () => {
    window.history.replaceState(null, "", "/#c=launch_code_1");
    const calls = fakeAgent({
      "POST /session": () => ({ json: { ok: true, message: "" } }),
      "GET /status": () => ({ json: status() }),
    });
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByRole("heading", { name: "Welcome" })).toBeTruthy();
    expect(window.location.hash).toBe("");
    const exchanges = calls.filter((c) => c.path === "/session");
    expect(exchanges).toEqual([{ method: "POST", path: "/session", body: { code: "launch_code_1" } }]);
  });

  it("an expired link says to open MeshForge from the menu again", async () => {
    window.history.replaceState(null, "", "/#c=old");
    fakeAgent({
      "POST /session": () => ({ status: 401, json: { error: { code: "link_expired", message: "expired", fix: "" } } }),
    });
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByText("This link has expired")).toBeTruthy();
  });

  it("wizard: join error is said in plain words; a good code moves on to the policy step, which saves the safe defaults", async () => {
    let joined = false;
    const calls = fakeAgent({
      "GET /status": () => ({ json: status(joined ? { joined: true, phase: "running", fabric_id: "fab_1" } : {}) }),
      "GET /discover": () => ({ json: { controllers: [{ name: "ctl", host: "192.168.1.5", port: 47100 }], problem: null } }),
      "POST /join": (body) => {
        const code = (body as { code: string }).code;
        if (code === "mf1-old") {
          return { status: 400, json: { error: { code: "expired", message: "This code has expired.", fix: "Ask for a new code." } } };
        }
        joined = true;
        return { json: { fabric_id: "fab_1", node_id: "node_1", controller: "192.168.1.5:47101", cert_not_after: "2027-01-01" } };
      },
      "GET /policy": () => ({ json: POLICY }),
      "PUT /policy": () => ({ json: POLICY }),
      "PUT /node-name": (body) => ({ json: { name: (body as { name: string }).name, source: "console", hostname: "lab-a", applies_after_restart: true } }),
      "GET /checks": () => ({ json: { checks: [{ id: "docker", title: "Docker", status: "missing", why: "Docker is not installed.", fix: "Install it.", action: null }] } }),
      "GET /docker-access": () => ({ json: { supported: true, menu_action: "x", command: "y", explanation: "z" } }),
      "POST /setup-complete": () => ({ json: { ok: true, message: "" } }),
    });
    const user = userEvent.setup();
    render(<ConsoleApp client={client()} />);
    await user.click(await screen.findByRole("button", { name: "Start" }));
    const codeBox = await screen.findByLabelText("Join code");
    await user.type(codeBox, "mf1-old");
    await user.click(screen.getByRole("button", { name: "Join" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("This code has expired.")).toBeTruthy();
    expect(within(alert).getByText("Ask for a new code.")).toBeTruthy();

    await user.clear(codeBox);
    await user.type(codeBox, "mf1-good");
    await user.selectOptions(screen.getByLabelText("Controller"), "192.168.1.5");
    await user.click(screen.getByRole("button", { name: "Join" }));
    expect(await screen.findByRole("heading", { name: "What this laptop shares" })).toBeTruthy();
    expect(calls.find((c) => c.method === "POST" && c.path === "/join" && (c.body as { code: string }).code === "mf1-good")?.body).toEqual({
      code: "mf1-good",
      controller_host: "192.168.1.5",
    });
    // no "skip" on the policy step (Q26)
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    const accept = screen.getByRole("checkbox", { name: /Run jobs from the fabric/ });
    expect((accept as HTMLInputElement).checked).toBe(false);
    await user.type(screen.getByLabelText("Name of this laptop"), "Lab A");
    await user.click(screen.getByRole("button", { name: "Save and continue" }));
    expect(await screen.findByText("Docker is not installed.")).toBeTruthy();
    const put = calls.find((c) => c.method === "PUT" && c.path === "/policy");
    expect(put?.body).toMatchObject({ values: { accept_jobs: false, max_cpu_percent: 50, max_gpu_jobs: 0, accept_gpu_sessions: false } });
    expect(calls.find((c) => c.path === "/node-name" && c.method === "PUT")?.body).toEqual({ name: "Lab A" });
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Finish" }));
    await waitFor(() => {
      expect(calls.some((c) => c.path === "/setup-complete")).toBe(true);
    });
  });

  it("a joined, set-up laptop shows Home with a stop reason and its fix", async () => {
    fakeAgent({
      "GET /status": () => ({
        json: status({
          joined: true,
          setup_complete: true,
          phase: "stopped",
          stop_reason: { code: "revoked", message: "this node was revoked", failure_type: "unauthorized" },
        }),
      }),
      "GET /checks": () => ({ json: { checks: [] } }),
      "GET /docker-access": () => ({ json: { supported: true, menu_action: "x", command: "y", explanation: "z" } }),
    });
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByText("MeshForge is stopped on this laptop")).toBeTruthy();
    expect(screen.getByText(/leave the fabric here and join again/)).toBeTruthy();
    expect(screen.getByRole("navigation", { name: "Console" })).toBeTruthy();
  });

  it("V0.17 G5: a version_unsupported stop says which side to upgrade", async () => {
    const reason =
      "version_unsupported: agent 0.15.0 is too old for controller 0.17.0 (it accepts agents 0.16 or newer); upgrade the MeshForge agent on lab-a";
    fakeAgent({
      "GET /status": () => ({
        json: status({
          joined: true,
          setup_complete: true,
          phase: "stopped",
          stop_reason: { code: "version_unsupported", message: reason, failure_type: "config_error" },
        }),
      }),
      "GET /checks": () => ({ json: { checks: [] } }),
      "GET /docker-access": () => ({ json: { supported: true, menu_action: "x", command: "y", explanation: "z" } }),
    });
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByText(reason)).toBeTruthy();
    expect(screen.getByText(/upgrade the controller first/)).toBeTruthy();
    expect(screen.getByText(/install the newer MeshForge on this laptop/)).toBeTruthy();
  });

  it("V0.17 G5: a rolled-back upgrade is shown with what to do; 'in progress' is not alarming", async () => {
    const message = "running the previous version 0.17.0: the upgrade to 0.17.1 failed (forced)";
    let kind: "rolled_back" | "in_progress" = "rolled_back";
    fakeAgent({
      "GET /status": () => ({
        json: status({ joined: true, setup_complete: true, phase: "running", upgrade: { kind, message, fix: "" } }),
      }),
      "GET /checks": () => ({ json: { checks: [] } }),
      "GET /docker-access": () => ({ json: { supported: true, menu_action: "x", command: "y", explanation: "z" } }),
    });
    const { unmount } = render(<ConsoleApp client={client()} />);
    expect(await screen.findByText("The last upgrade was rolled back")).toBeTruthy();
    expect(screen.getByText(message)).toBeTruthy();
    expect(screen.getByText(/install the new version again/)).toBeTruthy();
    unmount();
    kind = "in_progress";
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByText("This laptop")).toBeTruthy();
    expect(screen.queryByText("The last upgrade was rolled back")).toBeNull();
  });
});

describe("V0.17 G5 fix round 1", () => {
  it("an interrupted upgrade shows the fix the agent names for where it stopped", async () => {
    const fix = "finish it by installing the new package again: sudo dpkg -i ./<the 0.17.1 .deb>";
    fakeAgent({
      "GET /status": () => ({
        json: status({ joined: true, setup_complete: true, phase: "running", upgrade: { kind: "interrupted", message: "cut off", fix } }),
      }),
      "GET /checks": () => ({ json: { checks: [] } }),
      "GET /docker-access": () => ({ json: { supported: true, menu_action: "x", command: "y", explanation: "z" } }),
    });
    render(<ConsoleApp client={client()} />);
    expect(await screen.findByText("An upgrade was interrupted")).toBeTruthy();
    expect(screen.getByText(fix)).toBeTruthy();
    expect(screen.queryByText(/dpkg --configure -a/)).toBeNull();
  });
});
