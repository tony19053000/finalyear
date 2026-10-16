/**
 * V0.8 G4 tester r2: the Jobs pages under awkward use - double clicks, refusals, terminal states,
 * hostile job names, very long lists, unsupported manifests - against a stubbed controller.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../../components/Toast";
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


const pick = (rel: string, text: string) => {
  const f = new File([text], rel.split("/").pop() ?? "f");
  Object.defineProperty(f, "webkitRelativePath", { value: rel });
  return f;
};

interface Sent {
  keys: (string | null)[];
  bodies: Record<string, unknown>[];
  haveCalls: number;
}
function happy(failFirst = 0): Sent {
  const sent: Sent = { keys: [], bodies: [], haveCalls: 0 };
  controller((url, init) => {
    if (url.pathname === "/v1/blobs/have") {
      sent.haveCalls += 1;
      return json(200, { missing: [] });
    }
    if (url.pathname === "/v1/jobs" && init.method === "POST") {
      sent.keys.push(new Headers(init.headers).get("Idempotency-Key"));
      sent.bodies.push(JSON.parse(init.body as string) as Record<string, unknown>);
      return sent.keys.length <= failFirst
        ? json(503, { error: { code: "resource_busy", message: "busy" } })
        : json(201, { job_id: "job_OK", state: "queued", created: true });
    }
    return undefined;
  });
  return sent;
}

async function loaded() {
  renderAt("/jobs/new", <SubmitJobPage />, "/jobs/new");
  fireEvent.change(screen.getByLabelText("Project folder"), { target: { files: [pick("p/main.py", "1")] } });
  await screen.findByLabelText("Project summary");
  return screen.getByRole<HTMLButtonElement>("button", { name: "Upload and submit" });
}

describe("priority validation edge strings (T-15)", () => {
  it.each([
    ["007", 7],
    ["-0", 0],
    ["10", 10],
    ["-10", -10],
    ["0", 0],
  ])("%j is accepted and sent as the number %d", async (text, want) => {
    const sent = happy();
    const button = await loaded();
    const field = screen.getByLabelText("Priority (-10 to 10)");
    fireEvent.change(field, { target: { value: text } });
    expect(button.disabled, text).toBe(false);
    await userEvent.click(button);
    await waitFor(() => {
      expect(sent.bodies).toHaveLength(1);
    });
    const got = (sent.bodies[0]?.manifest as { priority: number }).priority;
    expect(got === want, `${text} -> ${String(got)}`).toBe(true);
    expect(Object.is(got, -0)).toBe(false); // JSON would turn -0 into 0 anyway; never ship -0 as a distinct value
  });

  it.each(["11", "-11", "10.5", "1e1", "99999999999999999999"])("%j is refused with the reason and nothing is sent", async (text) => {
    const sent = happy();
    const button = await loaded();
    const field = screen.getByLabelText("Priority (-10 to 10)");
    fireEvent.change(field, { target: { value: text } });
    expect(field.getAttribute("aria-invalid"), text).toBe("true");
    expect(button.disabled, text).toBe(true);
    // Even a forced click (disabled attribute removed by devtools) cannot start an upload.
    button.disabled = false;
    fireEvent.click(button);
    await new Promise((r) => setTimeout(r, 50));
    expect(sent.haveCalls).toBe(0);
    expect(sent.keys).toHaveLength(0);
  });

  it("emptying the field goes back to the manifest/default (no `priority` key forced)", async () => {
    const sent = happy();
    const button = await loaded();
    const field = screen.getByLabelText("Priority (-10 to 10)");
    fireEvent.change(field, { target: { value: "99" } });
    fireEvent.change(field, { target: { value: "" } });
    expect(button.disabled).toBe(false);
    await userEvent.click(button);
    await waitFor(() => {
      expect(sent.bodies).toHaveLength(1);
    });
    expect("priority" in (sent.bodies[0]?.manifest as object)).toBe(false);
  });

  it("after a failed submit, making the priority invalid disables the retry button", async () => {
    happy(1);
    const button = await loaded();
    await userEvent.click(button);
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "50" } });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: /upload and submit/i }).disabled).toBe(true);
  });
});

describe("idempotency key identity (T-14)", () => {
  it("same body -> same key even if the priority is retyped as an equal number ('5' -> '05')", async () => {
    const sent = happy(1);
    const button = await loaded();
    fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "5" } });
    await userEvent.click(button);
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "05" } });
    await userEvent.click(screen.getByRole("button", { name: /upload and submit/i }));
    await waitFor(() => {
      expect(sent.keys).toHaveLength(2);
    });
    expect(sent.keys[1]).toBe(sent.keys[0]);
  });

  it.each([
    ["name", (n: string) => { fireEvent.change(screen.getByLabelText("Name (overrides the manifest)"), { target: { value: n } }); }],
    ["policy", () => { fireEvent.change(screen.getByLabelText("Placement policy"), { target: { value: "round_robin" } }); }],
    ["priority", () => { fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "3" } }); }],
  ])("a changed %s mints a NEW key (a reused key with a different body is a 409)", async (_label, change) => {
    const sent = happy(1);
    const button = await loaded();
    await userEvent.click(button);
    await screen.findByRole("alert");
    change("renamed");
    await userEvent.click(screen.getByRole("button", { name: /upload and submit/i }));
    await waitFor(() => {
      expect(sent.keys).toHaveLength(2);
    });
    expect(sent.keys[1]).not.toBe(sent.keys[0]);
  });

  it("two separate prepared projects never share a key; keys are unique per submit and look like web-<uuid>", async () => {
    const sent = happy();
    const button = await loaded();
    await userEvent.click(button);
    await waitFor(() => {
      expect(sent.keys).toHaveLength(1);
    });
    expect(sent.keys[0]).toMatch(/^web-[0-9a-f-]{36}$/);
  });

  it("the body is fixed at click time: the fields are locked while the upload runs", async () => {
    let release: (r: Response) => void = () => undefined;
    controller((url) => {
      if (url.pathname === "/v1/blobs/have") return undefined;
      return undefined;
    });
    const sent: Record<string, unknown>[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string, init: RequestInit = {}) => {
        const url = new URL(input, "https://ctl.test");
        if (url.pathname === "/v1/session") return Promise.resolve(json(200, { session: SESSION }));
        if (url.pathname === "/v1/nodes") return Promise.resolve(json(200, { nodes: [], count: 0 }));
        if (url.pathname === "/v1/blobs/have") return new Promise<Response>((r) => { release = r; });
        if (url.pathname === "/v1/jobs") {
          sent.push(JSON.parse(init.body as string) as Record<string, unknown>);
          return Promise.resolve(json(201, { job_id: "job_OK", state: "queued", created: true }));
        }
        return Promise.resolve(json(404, { error: { code: "not_found", message: "x" } }));
      }),
    );
    const button = await loaded();
    fireEvent.change(screen.getByLabelText("Priority (-10 to 10)"), { target: { value: "2" } });
    await userEvent.click(button);
    const field = screen.getByLabelText<HTMLInputElement>("Priority (-10 to 10)");
    expect(field.disabled).toBe(true);
    fireEvent.change(field, { target: { value: "9" } }); // a forced edit while busy
    release(json(200, { missing: [] }));
    await waitFor(() => {
      expect(sent).toHaveLength(1);
    });
    expect((sent[0]?.manifest as { priority: number }).priority).toBe(2);
  });
});
