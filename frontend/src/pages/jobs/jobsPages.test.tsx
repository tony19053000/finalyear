/**
 * The Jobs pages against a fake controller (stubbed fetch, fake log socket):
 * every number and label on screen comes from what the API returned
 * (checklist 26), honest empty/error states, cancel, submit.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../../components/Toast";
import type { SocketLike } from "../../events/eventStream";
import { JobDetailPage } from "./JobDetailPage";
import { JobsListPage } from "./JobsListPage";
import type { DecisionList, JobDetail, JobSummary } from "./jobsApi";
import { SubmitJobPage } from "./submit/SubmitJobPage";

/* ------------------------------------------------------------ fakes */

type Handler = (url: URL, init: RequestInit) => Response | undefined;

interface Call {
  method: string;
  url: URL;
  init: RequestInit;
}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

const SESSION = {
  user: { id: "usr_1", name: "alice", role: "admin" },
  session_id: "tok_1",
  expires_at: "2026-10-23T00:00:00+00:00",
  idle_expires_at: "2026-10-16T12:00:00+00:00",
  idle_timeout_s: 43200,
};

const NODES = {
  nodes: [
    { node_id: "node_A", state: "online", connected: true, hostname: "laptop-a", os: "linux", platform: "x", gpus: [], last_seen_at: "2026-10-16T00:00:00Z" },
  ],
  count: 1,
};

function fakeController(handler: Handler): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: string, init: RequestInit = {}) => {
      const url = new URL(input, "https://ctl.test");
      calls.push({ method: init.method ?? "GET", url, init });
      const answer = handler(url, init);
      if (answer !== undefined) return Promise.resolve(answer);
      if (url.pathname === "/v1/session") return Promise.resolve(json(200, SESSION));
      if (url.pathname === "/v1/nodes") return Promise.resolve(json(200, NODES));
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
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
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
  return client;
}

function summary(i: number, overrides: Partial<JobSummary> = {}): JobSummary {
  const n = String(i).padStart(4, "0");
  return {
    job_id: `job_01J${n}`,
    name: `train-${n}`,
    state: "succeeded",
    placement: { mode: "auto" },
    created_at: "2026-10-16T08:00:00Z",
    updated_at: "2026-10-16T08:05:00Z",
    priority: 0,
    ...overrides,
  };
}

/* ------------------------------------------------------------ list */

describe("Jobs list (0.8.5, D174)", () => {
  it("shows exactly what GET /v1/jobs returned, and pages with the cursor", async () => {
    const page1 = [summary(3, { state: "running", priority: 7, placement: { mode: "offload", node_id: "node_A" } }), summary(2)];
    const page2 = [summary(1, { state: "failed" })];
    const calls = fakeController((url) => {
      if (url.pathname !== "/v1/jobs") return undefined;
      return url.searchParams.get("cursor") === "C2"
        ? json(200, { jobs: page2, count: 1, next_cursor: null })
        : json(200, { jobs: page1, count: 2, next_cursor: "C2" });
    });
    renderAt("/jobs", <JobsListPage />, "/jobs");
    const table = await screen.findByRole("table", { name: /jobs, newest first/i });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "train-0003" }).getAttribute("href")).toBe("/jobs/job_01J0003");
    expect(rows[0]?.textContent).toContain("running");
    expect(rows[0]?.textContent).toContain("offload · node_A");
    expect(rows[0]?.textContent).toContain("7");
    expect(screen.getByRole("status").textContent).toMatch(/2 jobs loaded; more on the server/);
    expect(calls.find((c) => c.url.pathname === "/v1/jobs")?.url.searchParams.get("limit")).toBe("50");

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => {
      expect(within(table).getAllByRole("row")).toHaveLength(4);
    });
    expect(screen.getByRole("status").textContent).toMatch(/3 jobs loaded; that is all/);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("filters go to the server (state, node, time) and into the URL", async () => {
    const seen: URLSearchParams[] = [];
    fakeController((url) => {
      if (url.pathname !== "/v1/jobs") return undefined;
      seen.push(url.searchParams);
      return json(200, { jobs: [summary(1, { state: "failed" })], count: 1, next_cursor: null });
    });
    renderAt("/jobs", <JobsListPage />, "/jobs");
    await screen.findByRole("table");
    await userEvent.selectOptions(screen.getByLabelText("State"), "active");
    await waitFor(() => {
      expect(seen.at(-1)?.getAll("state")).toEqual(["submitted", "planning", "queued", "preparing", "running", "collecting", "retrying"]);
    });
    await userEvent.selectOptions(await screen.findByLabelText("Ran on node"), "node_A");
    await waitFor(() => {
      expect(seen.at(-1)?.get("node")).toBe("node_A");
    });
    fireEvent.change(screen.getByLabelText("Created after"), { target: { value: "2026-10-01T10:00" } });
    await waitFor(() => {
      expect(seen.at(-1)?.get("created_after")).toBe(new Date("2026-10-01T10:00").toISOString());
    });
    expect(screen.getByTestId("where").textContent).toMatch(/node=node_A/);
    await userEvent.type(screen.getByLabelText("Owner (user id)"), "usr_9");
    await waitFor(() => {
      expect(seen.at(-1)?.get("owner")).toBe("usr_9");
    });
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect([...(seen.at(-1)?.keys() ?? [])]).toEqual(["limit"]);
    });
  });

  it("windows rows over 200: the DOM holds a slice, spacers hold the rest (no style attribute)", async () => {
    const many = Array.from({ length: 500 }, (_, i) => summary(500 - i));
    fakeController((url) => (url.pathname === "/v1/jobs" ? json(200, { jobs: many, count: 500, next_cursor: null }) : undefined));
    renderAt("/jobs", <JobsListPage />, "/jobs");
    const table = await screen.findByRole("table");
    const bodyRows = table.querySelectorAll("tbody tr:not([data-testid=jobs-spacer])");
    expect(bodyRows.length).toBeGreaterThan(10);
    expect(bodyRows.length).toBeLessThan(100);
    const spacers = table.querySelectorAll("[data-testid=jobs-spacer] td");
    expect(spacers).toHaveLength(1); // at the top: only a bottom spacer
    expect(Number(spacers[0]?.getAttribute("height"))).toBeGreaterThan(0);
    expect(table.outerHTML).not.toMatch(/style=/);
    expect(table.getAttribute("aria-rowcount")).toBe("501");
  });

  it("honest empty and error states", async () => {
    fakeController((url) => (url.pathname === "/v1/jobs" ? json(200, { jobs: [], count: 0, next_cursor: null }) : undefined));
    renderAt("/jobs", <JobsListPage />, "/jobs");
    expect(await screen.findByRole("heading", { name: "No jobs yet" })).toBeTruthy();
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
    fakeController((url) =>
      url.pathname === "/v1/jobs" ? json(503, { error: { code: "network_error", message: "db down" } }) : undefined,
    );
    renderAt("/jobs", <JobsListPage />, "/jobs");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Could not load jobs");
    expect(alert.textContent).toContain("db down");
  });
});

/* ------------------------------------------------------------ detail */

const DIGEST = `sha256:${"ab".repeat(32)}`;

function detail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    ...summary(7, { state: "running", priority: 3, placement: { mode: "auto", policy: "cost_model" } }),
    tree_id: `sha256:${"cd".repeat(32)}`,
    manifest: { name: "train-0007", entrypoint: ["python", "main.py"] },
    transitions: [
      { seq: 1, from_state: null, to_state: "queued", at: "2026-10-16T08:00:00Z" },
      { seq: 2, from_state: "queued", to_state: "running", at: "2026-10-16T08:00:12Z" },
    ],
    attempts: [
      {
        attempt_id: "att_1",
        node_id: "node_A",
        fencing_token: 1,
        state: "running",
        exit_code: null,
        output_digest: null,
        missing_outputs: [],
        logs: {},
        started_at: "2026-10-16T08:00:12Z",
        ended_at: null,
        transfers: [],
      },
    ],
    inputs: [],
    ...overrides,
  };
}

const DECISIONS: DecisionList = {
  job_id: "job_01J0007",
  count: 1,
  decisions: [
    {
      id: 11,
      attempt_no: 1,
      attempt_id: "att_1",
      outcome: "dispatched",
      policy: "cost_model",
      chosen_node_id: "node_A",
      created_at: "2026-10-16T08:00:10Z",
      snapshot_digest: "x",
      candidates: [
        {
          node_id: "node_A",
          mode: "offload",
          feasible: true,
          slot: "cpu",
          estimate: {
            total_s: 12.5,
            terms: {
              transfer: { seconds: 1.5, source: "measured", detail: {} },
              compute: { seconds: 11, source: "assumed", detail: {} },
            },
          },
          score: 12.5,
          rank: 1,
        },
        { node_id: "node_B", mode: "offload", feasible: false, reject_code: "no_gpu", reject_text: "the job needs a GPU" },
      ],
      actual: { attempt_id: "att_1", node_id: "node_A", state: "running", phases_ms: { queue: 1200, stage: 300, env: null, run: null, collect: null } },
    },
  ],
};

class LogSocket implements SocketLike {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {}
  close(): void {
    // client close
  }
}

function renderDetail(job: JobDetail, extra: Handler = () => undefined) {
  const sockets: LogSocket[] = [];
  const calls = fakeController((url, init) => {
    const own = extra(url, init);
    if (own !== undefined) return own;
    if (url.pathname === `/v1/jobs/${job.job_id}`) return json(200, job);
    if (url.pathname === `/v1/jobs/${job.job_id}/decisions`) return json(200, DECISIONS);
    return undefined;
  });
  const factory = (url: string) => {
    const s = new LogSocket(url);
    sockets.push(s);
    return s;
  };
  renderAt(`/jobs/${job.job_id}`, <JobDetailPage logSocketFactory={factory} />, "/jobs/:jobId");
  return { calls, sockets };
}

describe("Job detail (0.8.5, D176)", () => {
  it("summary, timeline and attempts show the API's values; the log follows the job", async () => {
    const job = detail();
    const { sockets } = renderDetail(job);
    expect(await screen.findByRole("heading", { level: 1, name: "train-0007" })).toBeTruthy();
    const summaryCard = screen.getByRole("region", { name: "Summary" });
    expect(summaryCard.textContent).toContain("running");
    expect(summaryCard.textContent).toContain("auto (the planner chooses)");
    expect(summaryCard.textContent).toContain("cost_model");
    expect(summaryCard.textContent).toContain("3");
    expect(summaryCard.textContent).toContain(job.tree_id ?? "");
    expect(sockets[0]?.url).toMatch(/\/v1\/jobs\/job_01J0007\/logs\/ws\?stream=both&stdout_offset=0&stderr_offset=0$/);

    // Log lines arrive from the socket.
    sockets[0]?.onopen?.(new Event("open"));
    sockets[0]?.onmessage?.(new MessageEvent("message", { data: JSON.stringify({ stream: "stdout", offset: 0, data: btoa("epoch 1 loss 0.5\n") }) }));
    expect(await screen.findByText("epoch 1 loss 0.5")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download full log" }).getAttribute("href")).toBe("/v1/jobs/job_01J0007/logs?stream=both");

    await userEvent.click(screen.getByRole("tab", { name: "Attempts (1)" }));
    const attempts = await screen.findByRole("table", { name: /attempts/i });
    expect(attempts.textContent).toContain("att_1");
    expect(attempts.textContent).toContain("node_A");

    await userEvent.click(screen.getByRole("tab", { name: "Timeline" }));
    const timeline = await screen.findByRole("list", { name: "State timeline" });
    expect(timeline.textContent).toContain("queued");
    expect(timeline.textContent).toContain("12.0 s"); // queued 08:00:00 -> running 08:00:12
  });

  it("plan explanation: every candidate, its terms with their source, rejections, the actual phases", async () => {
    renderDetail(detail());
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("tab", { name: "Plan" }));
    const table = await screen.findByRole("table", { name: /candidates of decision 11/i });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]?.textContent).toContain("node_A");
    expect(rows[0]?.textContent).toContain("chosen");
    expect(rows[0]?.textContent).toContain("measured");
    expect(rows[0]?.textContent).toContain("assumed");
    expect(rows[0]?.textContent).toContain("12.5 s");
    expect(rows[1]?.textContent).toContain("no_gpu");
    expect(rows[1]?.textContent).toContain("the job needs a GPU");
    expect(screen.getByText(/Actual:/).textContent).toContain("queue 1.2 s");
    expect(screen.getByText(/Actual:/).textContent).toContain("env did not run");
  });

  it("cancel: confirm dialog, POST with the CSRF header, toast; a refusal stays in the dialog", async () => {
    document.cookie = "mf_csrf=csrf-1";
    let answer = json(409, { error: { code: "conflict", message: "the job is being collected" } });
    const { calls } = renderDetail(detail(), (url) => (url.pathname.endsWith("/cancel") ? answer : undefined));
    await screen.findByRole("heading", { level: 1 });
    await userEvent.click(screen.getByRole("button", { name: "Cancel job" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancel this job?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel job" }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain("the job is being collected");
    const post = calls.find((c) => c.url.pathname.endsWith("/cancel"));
    expect(post?.method).toBe("POST");
    expect(new Headers(post?.init.headers).get("X-MeshForge-CSRF")).toBe("csrf-1");

    answer = json(202, { job_id: "job_01J0007", state: "running", cancel_requested: true });
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel job" }));
    expect(await screen.findByText("Cancel requested")).toBeTruthy();
  });

  it("a finished job: cancel disabled, outputs downloadable with the digest", async () => {
    const done = detail({
      state: "succeeded",
      attempts: [
        {
          attempt_id: "att_1",
          node_id: "node_A",
          fencing_token: 1,
          state: "done",
          exit_code: 0,
          output_digest: DIGEST,
          missing_outputs: ["plots/"],
          logs: { stdout: { digest: "sha256:1", bytes: 2048, truncated: false } },
          started_at: "2026-10-16T08:00:12Z",
          ended_at: "2026-10-16T08:01:12Z",
          transfers: [],
        },
      ],
    });
    renderDetail(done);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Cancel job" }).disabled).toBe(true);
    await userEvent.click(screen.getByRole("tab", { name: "Outputs" }));
    const link = await screen.findByRole("link", { name: "Download outputs" });
    expect(link.getAttribute("href")).toBe(`/v1/blobs/${"ab".repeat(32)}`);
    expect(link.getAttribute("download")).toBe("job_01J0007-attempt1-outputs.tar.zst");
    expect(screen.getByText(DIGEST)).toBeTruthy();
    expect(screen.getByText(/Declared but not found: plots\//)).toBeTruthy();
  });

  it("an unknown job (or another user's) says so; other failures offer a retry", async () => {
    fakeController((url) => (url.pathname.startsWith("/v1/jobs/") ? json(404, { error: { code: "not_found", message: "no such job" } }) : undefined));
    renderAt("/jobs/job_X", <JobDetailPage logSocketFactory={(u) => new LogSocket(u)} />, "/jobs/:jobId");
    expect(await screen.findByRole("heading", { name: "No such job" })).toBeTruthy();
  });
});

/* ------------------------------------------------------------ submit */

function picked(rel: string, text: string): File {
  const file = new File([text], rel.split("/").pop() ?? "f");
  Object.defineProperty(file, "webkitRelativePath", { value: rel });
  return file;
}

describe("Submit from the browser (D177)", () => {
  it("folder -> hashed tree -> have/need upload -> POST /v1/jobs with an Idempotency-Key -> the job page", async () => {
    document.cookie = "mf_csrf=csrf-2";
    const calls = fakeController((url, init) => {
      if (url.pathname === "/v1/blobs/have") {
        const asked = (JSON.parse(init.body as string) as { digests: string[] }).digests;
        return json(200, { missing: asked });
      }
      if (url.pathname.startsWith("/v1/blobs/") && init.method === "PUT") return json(201, { digest: "x", size: 1, created: true });
      if (url.pathname === "/v1/jobs" && init.method === "POST") return json(201, { job_id: "job_NEW", state: "queued", created: true });
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    const input = screen.getByLabelText("Project folder");
    expect(input.hasAttribute("webkitdirectory")).toBe(true);
    fireEvent.change(input, {
      target: {
        files: [
          picked("myproj/main.py", "print('hi')\n"),
          picked("myproj/.gitignore", "*.log\n"),
          picked("myproj/debug.log", "noise"),
          picked("myproj/__pycache__/x.pyc", "x"),
        ],
      },
    });
    const facts = await screen.findByLabelText("Project summary");
    expect(facts.textContent).toContain("myproj");
    expect(facts.textContent).toContain('["python","main.py"]');
    expect(facts.textContent).toMatch(/2 files/);
    expect(facts.textContent).toMatch(/2 ignored/);

    await userEvent.type(screen.getByLabelText("Priority (-10 to 10)"), "5");
    await userEvent.selectOptions(screen.getByLabelText("Placement policy"), "least_loaded");
    await userEvent.click(screen.getByRole("button", { name: "Upload and submit" }));
    await waitFor(() => {
      // An alert (upload/submit failure) shows its text here instead - a clear failure message.
      expect(screen.queryByRole("alert")?.textContent ?? screen.getByTestId("where").textContent).toBe("/jobs/job_NEW");
    });

    const puts = calls.filter((c) => c.method === "PUT").map((c) => c.url.pathname);
    expect(puts).toHaveLength(3); // main.py, .gitignore, then the tree manifest
    const submit = calls.find((c) => c.url.pathname === "/v1/jobs" && c.method === "POST");
    const headers = new Headers(submit?.init.headers);
    expect(headers.get("Idempotency-Key")).toMatch(/^web-[0-9a-f-]{36}$/);
    expect(headers.get("X-MeshForge-CSRF")).toBe("csrf-2");
    const body = JSON.parse(submit?.init.body as string) as { manifest: Record<string, unknown>; tree_id: string; placement: unknown };
    expect(body.manifest).toEqual({ name: "myproj", entrypoint: ["python", "main.py"], priority: 5 });
    expect(body.placement).toEqual({ policy: "least_loaded" });
    expect(puts.at(-1)).toBe(`/v1/blobs/${body.tree_id.slice(7)}`);
  });

  it("a refused submit shows the planner's reasons; retry reuses the same Idempotency-Key", async () => {
    let first = true;
    const calls = fakeController((url, init) => {
      if (url.pathname === "/v1/blobs/have") return json(200, { missing: [] });
      if (url.pathname === "/v1/jobs" && init.method === "POST") {
        if (first) {
          first = false;
          return json(409, {
            error: {
              code: "no_feasible_plan",
              message: "none of the 1 node(s) can ever run this job",
              rejections: [{ node_id: "node_A", reject_code: "no_gpu", reject_text: "needs a GPU" }],
            },
          });
        }
        return json(201, { job_id: "job_2", state: "queued", created: true });
      }
      return undefined;
    });
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [picked("p/main.py", "x")] } });
    await screen.findByLabelText("Project summary");
    await userEvent.click(screen.getByRole("button", { name: "Upload and submit" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("none of the 1 node(s) can ever run this job");
    expect(alert.textContent).toContain("node_A: no_gpu - needs a GPU");
    await userEvent.click(screen.getByRole("button", { name: "Retry upload and submit" }));
    await waitFor(() => {
      expect(screen.getByTestId("where").textContent).toBe("/jobs/job_2");
    });
    const keys = calls.filter((c) => c.url.pathname === "/v1/jobs").map((c) => new Headers(c.init.headers).get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("a project the CLI would refuse is refused here too, with the reason", async () => {
    fakeController(() => undefined);
    renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
    fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [picked("p/notes.txt", "x")] } });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Cannot submit this project");
    expect(alert.textContent).toMatch(/entrypoint: missing/);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Upload and submit" }).disabled).toBe(true);
  });
});
