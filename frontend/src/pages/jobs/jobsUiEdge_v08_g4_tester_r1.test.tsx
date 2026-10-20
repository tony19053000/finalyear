/**
 * V0.8 G4 tester r1: the Jobs pages under awkward use - double clicks, refusals, terminal states,
 * hostile job names, very long lists, unsupported manifests - against a stubbed controller.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../../components/Toast";
import { computeWindow } from "../../components/jobs/useWindowedRows";
import type { SocketLike } from "../../events/eventStream";
import { JobDetailPage } from "./JobDetailPage";
import { JobsListPage } from "./JobsListPage";
import type { JobDetail, JobSummary } from "./jobsApi";
import { SubmitJobPage } from "./submit/SubmitJobPage";

// V0.8 G5 (D198 follow-up): uploads and submit now go through api/client.ts,
// which refuses a mutation without the mf_csrf cookie (D191 c). A signed-in
// browser always has it; tests that model one set it here (setup.ts clears it).
beforeEach(() => {
  document.cookie = "mf_csrf=test-csrf";
});

type Handler = (url: URL, init: RequestInit) => Response | undefined;
interface Call {
  method: string;
  url: URL;
  init: RequestInit;
}
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

const SESSION = {
  user: { id: "usr_1", name: "alice", role: "user" },
  session_id: "tok_1",
  expires_at: "2026-10-23T00:00:00+00:00",
  idle_expires_at: "2026-10-16T12:00:00+00:00",
  idle_timeout_s: 43200,
};

function controller(handler: Handler): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const url = new URL(input, "https://ctl.test");
      calls.push({ method: init.method ?? "GET", url, init });
      const own = handler(url, init);
      if (own !== undefined) return Promise.resolve(own);
      if (url.pathname === "/v1/session") return Promise.resolve(json(200, { session: SESSION }));
      if (url.pathname === "/v1/nodes") return Promise.resolve(json(200, { nodes: [], count: 0 }));
      return Promise.resolve(json(404, { error: { code: "not_found", message: `no route ${url.pathname}` } }));
    }),
  );
  return calls;
}
afterEach(() => {
  vi.unstubAllGlobals();
});

function Where() {
  const loc = useLocation();
  return <span data-testid="where">{`${loc.pathname}${loc.search}`}</span>;
}
function renderAt(path: string, element: ReactNode, routePath: string) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={routePath} element={element} />
            <Route path="*" element={<p>elsewhere</p>} />
          </Routes>
          <Where />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function summary(i: number, o: Partial<JobSummary> = {}): JobSummary {
  const n = String(i).padStart(5, "0");
  return { job_id: `job_01J${n}`, name: `job-${n}`, state: "succeeded", placement: { mode: "auto" }, created_at: "2026-10-16T08:00:00Z", updated_at: "2026-10-16T08:05:00Z", priority: 0, ...o };
}
function detail(state: JobSummary["state"]): JobDetail {
  return {
    ...summary(7, { state }),
    tree_id: `sha256:${"cd".repeat(32)}`,
    manifest: { name: "job-00007", entrypoint: ["python", "main.py"] },
    transitions: [{ seq: 1, from_state: null, to_state: "queued", at: "2026-10-16T08:00:00Z" }],
    attempts: [],
    inputs: [],
    warnings: [],
  };
}
class Sock implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    /* nothing */
  }
}
function renderDetail(job: JobDetail, extra: Handler = () => undefined) {
  const calls = controller((url, init) => {
    const own = extra(url, init);
    if (own !== undefined) return own;
    if (url.pathname === `/v1/jobs/${job.job_id}`) return json(200, job);
    if (url.pathname === `/v1/jobs/${job.job_id}/decisions`) return json(200, { job_id: job.job_id, count: 0, decisions: [] });
    return undefined;
  });
  renderAt(`/jobs/${job.job_id}`, <JobDetailPage logSocketFactory={(u) => new Sock(u)} />, "/jobs/:jobId");
  return calls;
}

describe("cancel (D176) under awkward use", () => {
  // A signed-in browser holds the CSRF cookie; without it the client refuses to send (D191 c, G3).
  beforeEach(() => {
    document.cookie = "mf_csrf=csrf-edge; path=/";
  });
  afterEach(() => {
    document.cookie = "mf_csrf=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("cancel is enabled exactly in the non-terminal states", async () => {
    const live = ["submitted", "planning", "queued", "preparing", "running", "collecting", "retrying"] as const;
    const dead = ["succeeded", "failed", "cancelled"] as const;
    for (const state of [...live, ...dead]) {
      vi.unstubAllGlobals();
      document.body.innerHTML = "";
      renderDetail(detail(state));
      const button = await screen.findByRole<HTMLButtonElement>("button", { name: "Cancel job" });
      expect(button.disabled, state).toBe(dead.includes(state as (typeof dead)[number]));
    }
  });

  it("a double-click on the confirm button sends ONE cancel request", async () => {
    let n = 0;
    const calls = renderDetail(detail("running"), (url) => {
      if (url.pathname.endsWith("/cancel")) {
        n += 1;
        return json(202, { job_id: "job_01J00007", state: "running", cancel_requested: true });
      }
      return undefined;
    });
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancel this job?" });
    await userEvent.dblClick(within(dialog).getByRole("button", { name: "Cancel job" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(n).toBe(1);
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("403 / 404 / no-network answers stay in the dialog with a reason; 'already ended' (200) is an info toast", async () => {
    for (const [status, code] of [[403, "unauthorized"], [404, "not_found"]] as const) {
      vi.unstubAllGlobals();
      document.body.innerHTML = "";
      renderDetail(detail("running"), (url) => (url.pathname.endsWith("/cancel") ? json(status, { error: { code, message: `refused ${String(status)}` } }) : undefined));
      await screen.findByRole("heading", { level: 1 });
      await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
      const dialog = await screen.findByRole("dialog");
      await userEvent.click(within(dialog).getByRole("button", { name: "Cancel job" }));
      expect((await within(dialog).findByRole("alert")).textContent).toContain(`refused ${String(status)}`);
    }
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    renderDetail(detail("running"), (url) => {
      if (url.pathname.endsWith("/cancel")) throw new TypeError("Failed to fetch");
      return undefined;
    });
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel job" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/did not answer|Could not cancel/);

    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    renderDetail(detail("running"), (url) => (url.pathname.endsWith("/cancel") ? json(200, { job_id: "job_01J00007", state: "succeeded", cancel_requested: false }) : undefined));
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel job" }));
    expect(await screen.findByText("Job already ended")).toBeTruthy();
  });

  it("'Keep running' closes the dialog without any request (Escape is covered in the e2e, real Chromium)", async () => {
    const calls = renderDetail(detail("running"));
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Keep running" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });
});

describe("jobs list under load and hostile data", () => {
  it("2 500 loaded rows keep the DOM small; a hostile job name is text, never markup", async () => {
    const hostile = summary(99999, { name: "<img src=x onerror=alert(1)><script>boom()</script>" });
    const many = [hostile, ...Array.from({ length: 2499 }, (_, i) => summary(2499 - i))];
    controller((url) => (url.pathname === "/v1/jobs" ? json(200, { jobs: many, count: 2500, next_cursor: null }) : undefined));
    renderAt("/jobs", <JobsListPage />, "/jobs");
    const table = await screen.findByRole("table");
    expect(table.querySelectorAll("tbody tr").length).toBeLessThan(120);
    expect(table.querySelector("img")).toBeNull();
    expect(table.querySelector("script")).toBeNull();
    expect(table.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(screen.getByRole("status").textContent).toMatch(/2500 jobs loaded; that is all/);
  });

  it("a next_cursor that never ends (server loop) cannot be walked past the loaded pages without a click", async () => {
    let requests = 0;
    controller((url) => {
      if (url.pathname !== "/v1/jobs") return undefined;
      requests += 1;
      return json(200, { jobs: [summary(requests)], count: 1, next_cursor: "SAME" });
    });
    renderAt("/jobs", <JobsListPage />, "/jobs");
    await screen.findByRole("table");
    await new Promise((r) => setTimeout(r, 300));
    expect(requests).toBeLessThanOrEqual(3); // no runaway auto-paging
  });
});

describe("computeWindow (row virtualisation maths)", () => {
  it("covers the viewport, stays in range and spacers + rows add up, for random inputs", () => {
    let seed = 7;
    const rnd = (n: number) => {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      return seed % n;
    };
    for (let i = 0; i < 2000; i += 1) {
      const count = rnd(5000);
      const rowHeight = 20 + rnd(40);
      const scrollTop = rnd(count * rowHeight + 500);
      const viewport = rnd(1500);
      const w = computeWindow(count, scrollTop, viewport, { rowHeight, overscan: rnd(20), offset: rnd(80) });
      expect(w.start).toBeGreaterThanOrEqual(0);
      expect(w.end).toBeLessThanOrEqual(count);
      expect(w.start).toBeLessThanOrEqual(w.end);
      expect(w.padTop + (w.end - w.start) * rowHeight + w.padBottom).toBe(count * rowHeight);
      if (w.windowed) {
        // the last row (scrolled to the bottom) is always reachable
        const bottom = computeWindow(count, count * rowHeight, viewport, { rowHeight });
        expect(bottom.end).toBe(count);
      }
    }
  });
});

describe("submit page edge cases", () => {
  const pick = (rel: string, text: string) => {
    const f = new File([text], rel.split("/").pop() ?? "f");
    Object.defineProperty(f, "webkitRelativePath", { value: rel });
    return f;
  };

  it("a compute.yaml with YAML the browser will not read says 'use the CLI' and cannot be submitted", async () => {
    controller(() => undefined);
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), {
      target: { files: [pick("p/main.py", "1"), pick("p/compute.yaml", "entrypoint: python main.py\nenv: &base\n  base: python-cpu\n")] },
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/compute run|CLI/);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Upload and submit" }).disabled).toBe(true);
  });

  it("double-clicking 'Upload and submit' submits ONE job", async () => {
    let posts = 0;
    controller((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") {
        posts += 1;
        return json(201, { job_id: "job_ONE", state: "queued", created: true });
      }
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
    await screen.findByLabelText("Project summary");
    await userEvent.dblClick(screen.getByRole("button", { name: "Upload and submit" }));
    await waitFor(() => {
      expect(screen.getByTestId("where").textContent).toBe("/jobs/job_ONE");
    });
    expect(posts).toBe(1);
  });

  it("T-14 changing the priority after a failed submit does not retry with the OLD idempotency key", async () => {
    const keys: (string | null)[] = [];
    controller((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key"));
        return keys.length === 1 ? json(503, { error: { code: "resource_busy", message: "busy" } }) : json(201, { job_id: "job_2", state: "queued", created: true });
      }
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
    await screen.findByLabelText("Project summary");
    await userEvent.click(screen.getByRole("button", { name: "Upload and submit" }));
    await screen.findByRole("alert");
    await userEvent.type(screen.getByLabelText("Priority (-10 to 10)"), "9");
    await userEvent.click(screen.getByRole("button", { name: /Retry upload and submit|Upload and submit/ }));
    await waitFor(() => {
      expect(keys).toHaveLength(2);
    });
    // The server answers 409 (same key, different request) if the key is reused with a changed body.
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("T-15 an out-of-range priority is refused BEFORE the upload (not after sending the whole project)", async () => {
    const calls = controller((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") return json(400, { error: { code: "invalid_request", message: "priority must be -10..10" } });
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
    await screen.findByLabelText("Project summary");
    fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "99" } });
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Upload and submit" });
    if (!button.disabled) await userEvent.click(button);
    await new Promise((r) => setTimeout(r, 200));
    expect(calls.some((c) => c.url.pathname.startsWith("/v1/blobs/"))).toBe(false);
  });
});

describe("submit request identity and priority (fix round 1)", () => {
  const pick = (rel: string, text: string) => {
    const f = new File([text], rel.split("/").pop() ?? "f");
    Object.defineProperty(f, "webkitRelativePath", { value: rel });
    return f;
  };

  it("an IDENTICAL retry after a failed submit reuses the idempotency key", async () => {
    const keys: (string | null)[] = [];
    controller((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") {
        keys.push(new Headers(init.headers).get("Idempotency-Key"));
        return keys.length === 1 ? json(503, { error: { code: "resource_busy", message: "busy" } }) : json(201, { job_id: "job_2", state: "queued", created: true });
      }
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
    await screen.findByLabelText("Project summary");
    await userEvent.click(screen.getByRole("button", { name: "Upload and submit" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Retry upload and submit" }));
    await waitFor(() => {
      expect(keys).toHaveLength(2);
    });
    expect(keys[0]).toMatch(/^web-/);
    expect(keys[1]).toBe(keys[0]);
  });

  it("a bad priority shows the reason, disables submit, and a valid one is sent as a number", async () => {
    let body: unknown = null;
    controller((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") {
        body = JSON.parse(init.body as string);
        return json(201, { job_id: "job_P", state: "queued", created: true });
      }
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
    await screen.findByLabelText("Project summary");
    const field = screen.getByLabelText("Priority (-10 to 10)");
    const button = screen.getByRole<HTMLButtonElement>("button", { name: "Upload and submit" });
    for (const bad of ["11", "-11", "2.5", "1e1"]) {
      fireEvent.change(field, { target: { value: bad } });
      expect(field.getAttribute("aria-invalid"), bad).toBe("true");
      expect(screen.getByText("Priority must be a whole number from -10 to 10.")).toBeTruthy();
      expect(button.disabled, bad).toBe(true);
    }
    fireEvent.change(field, { target: { value: "-10" } });
    expect(field.getAttribute("aria-invalid")).toBe("false");
    expect(button.disabled).toBe(false);
    await userEvent.click(button);
    await waitFor(() => {
      expect(screen.getByTestId("where").textContent).toBe("/jobs/job_P");
    });
    expect((body as { manifest: { priority: unknown } }).manifest.priority).toBe(-10);
  });
});
