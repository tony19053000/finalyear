/**
 * DEMO MODE: `http://localhost:5173/?demo=1` (localhost only). Replaces the
 * controller's node / job / alert answers with demo/demoData.ts so the
 * Overview is seen filled, the Benchmarks pages' campaigns (demo/benchDemoData.ts:
 * list, status, runs, labels, quality, controls, Parquet downloads) and the Planner
 * page (demo/plannerDemoData.ts: calibration, evaluation reports, ML models,
 * predictions, run evaluation / train / pin / unpin) and the Settings pages
 * (demo/settingsDemoData.ts: controller health, API tokens, secrets, data,
 * invites - read-only); everything else still goes to the real controller. It stays on until the next full reload without `?demo=1`.
 * It never fakes a session: you sign in to the real controller as usual.
 *
 * To remove: delete src/demo/ and the demo lines in src/main.tsx.
 */
import type { QueryClient } from "@tanstack/react-query";

import {
  demoChunkTries,
  demoChunks,
  demoDecisions,
  demoLog,
  demoJobDetail,
  demoJobList,
  demoNodeDetail,
  demoNodeList,
  demoNetwork,
  demoNodeMetrics,
  demoNodeResources,
  demoSplit,
} from "./demoData";
import {
  demoCampaignDetail,
  demoCampaignList,
  demoCampaignStatus,
  demoControl,
  demoLabels,
  demoMovingCampaigns,
  demoParquet,
  demoQuality,
  demoRuns,
} from "./benchDemoData";
import { demoPlannerMoving, demoPlannerRoute } from "./plannerDemoData";
import { demoSettingsRoute } from "./settingsDemoData";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/** True when the page was opened with `?demo=1` on a local host (on until the next full reload). */
export function demoRequested(): boolean {
  if (!LOCAL_HOSTS.has(window.location.hostname)) return false;
  return new URLSearchParams(window.location.search).get("demo") === "1";
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

/**
 * Data only: sign-in, session and whoami (`/v1/session`, `/v1/auth/*` except the
 * token list) always go to the real controller, so the demo needs a real login.
 */
function route(url: URL): unknown {
  const p = url.pathname;
  if (p === "/v1/nodes") return demoNodeList();
  let m = /^\/v1\/nodes\/([^/]+)$/.exec(p);
  if (m) return demoNodeDetail(decodeURIComponent(m[1] ?? ""));
  m = /^\/v1\/nodes\/([^/]+)\/metrics$/.exec(p);
  if (m) return demoNodeMetrics(decodeURIComponent(m[1] ?? ""));
  m = /^\/v1\/nodes\/([^/]+)\/resources$/.exec(p);
  if (m) return demoNodeResources(decodeURIComponent(m[1] ?? ""));
  if (p === "/v1/network") return demoNetwork();
  if (p === "/v1/jobs") return demoJobList(url.searchParams.getAll("state"));
  m = /^\/v1\/jobs\/([^/]+)$/.exec(p);
  if (m) return demoJobDetail(decodeURIComponent(m[1] ?? ""));
  m = /^\/v1\/jobs\/([^/]+)\/decisions$/.exec(p);
  if (m) return demoDecisions(decodeURIComponent(m[1] ?? ""));
  m = /^\/v1\/jobs\/([^/]+)\/split$/.exec(p);
  if (m) return demoSplit(decodeURIComponent(m[1] ?? ""));
  m = /^\/v1\/jobs\/([^/]+)\/chunks$/.exec(p);
  if (m) return demoChunks(decodeURIComponent(m[1] ?? ""), Number(url.searchParams.get("start") ?? "0"));
  m = /^\/v1\/jobs\/([^/]+)\/chunks\/(\d+)\/tries$/.exec(p);
  if (m) return demoChunkTries(decodeURIComponent(m[1] ?? ""), Number(m[2]));
  if (p === "/v1/alerts") return { count: 0, next_cursor: null, alerts: [] };
  return demoSettingsRoute(url) ?? benchRoute(url);
}

const CAMPAIGN = /^\/v1\/bench\/campaigns\/([^/]+)(?:\/([a-z]+))?$/;

/** The Benchmarks pages' GET routes (demo/benchDemoData.ts); undefined = not a demo answer. */
function benchRoute(url: URL): unknown {
  const p = url.pathname;
  if (p === "/v1/bench/campaigns") return demoCampaignList();
  const m = CAMPAIGN.exec(p);
  if (m === null) return undefined;
  const id = decodeURIComponent(m[1] ?? "");
  switch (m[2]) {
    case undefined:
      return demoCampaignDetail(id);
    case "status":
      return demoCampaignStatus(id);
    case "runs": {
      const limit = Number(url.searchParams.get("limit") ?? "100");
      return demoRuns(id, Number(url.searchParams.get("after") ?? "0"), Number.isFinite(limit) && limit > 0 ? limit : 100, url.searchParams.get("state"));
    }
    case "labels":
      return url.searchParams.get("format") === "parquet" ? undefined : demoLabels(id);
    case "quality":
      return demoQuality(id);
    default:
      return undefined;
  }
}

/** The Parquet downloads (records export and labels table) of a demo campaign. */
function binaryRoute(url: URL): string | undefined {
  if (url.pathname === "/v1/telemetry/export") {
    const id = url.searchParams.get("campaign");
    return id === null ? undefined : demoParquet(id, "records");
  }
  const m = CAMPAIGN.exec(url.pathname);
  if (m !== null && m[2] === "labels" && url.searchParams.get("format") === "parquet") return demoParquet(decodeURIComponent(m[1] ?? ""), "labels");
  return undefined;
}

/** `POST /v1/bench/campaigns/{id}/{verb}` on a demo campaign. */
function controlRoute(url: URL): Response | undefined {
  const m = CAMPAIGN.exec(url.pathname);
  if (m === null || m[2] === undefined) return undefined;
  const out = demoControl(decodeURIComponent(m[1] ?? ""), m[2]);
  if (out === undefined) return undefined;
  return new Response(JSON.stringify(out.body), { status: out.ok ? 200 : out.status, headers: { "Content-Type": "application/json" } });
}

function frame(stream: "stdout" | "stderr", offset: number, text: string): string {
  let bin = "";
  for (const b of new TextEncoder().encode(text)) bin += String.fromCharCode(b);
  return JSON.stringify({ stream, offset, data: btoa(bin) });
}

/**
 * A stand-in for the job log WebSocket (`/v1/jobs/{id}/logs/ws`): the job's
 * opening lines, then - while it runs - a new line every ~1.4 s; a finished
 * job's log ends with the end frame.
 */
class DemoLogSocket {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  private timers: number[] = [];
  private offset = 0;

  constructor(jobId: string) {
    const log = demoLog(jobId);
    this.timers.push(
      window.setTimeout(() => {
        this.readyState = 1;
        this.onopen?.(new Event("open"));
        if (log === undefined) return;
        for (const line of log.lines) this.send(line);
        if (log.end !== null) {
          this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(log.end) }));
          return;
        }
        let n = 0;
        this.timers.push(
          window.setInterval(() => {
            this.send(log.liveLine(n));
            n += 1;
          }, 1400),
        );
      }, 250),
    );
  }

  private send(line: string): void {
    const text = `${line}\n`;
    this.onmessage?.(new MessageEvent("message", { data: frame("stdout", this.offset, text) }));
    this.offset += new TextEncoder().encode(text).length;
  }

  close(): void {
    this.readyState = 3;
    for (const t of this.timers) {
      window.clearTimeout(t);
      window.clearInterval(t);
    }
  }
}

/** Patch fetch for the demo routes; refresh the drifting data every 3 s. */
export function installDemo(client: QueryClient): void {
  const realFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, window.location.origin);
    if (url.origin === window.location.origin) {
      const planner = demoPlannerRoute(method, url, init?.body);
      if (planner !== undefined) {
        return Promise.resolve(new Response(JSON.stringify(planner.body), { status: planner.status, headers: { "Content-Type": "application/json" } }));
      }
    }
    if (method === "GET" && url.origin === window.location.origin) {
      const bytes = binaryRoute(url);
      if (bytes !== undefined) {
        return Promise.resolve(new Response(bytes, { status: 200, headers: { "Content-Type": "application/vnd.apache.parquet" } }));
      }
      const body = route(url);
      if (body !== undefined) return Promise.resolve(json(body));
    }
    if (method === "POST" && url.origin === window.location.origin) {
      const answer = controlRoute(url);
      if (answer !== undefined) return Promise.resolve(answer);
    }
    return realFetch(input, init);
  };
  // job logs: the demo jobs' logs come from DemoLogSocket, every other socket is real
  const RealSocket = window.WebSocket;
  const DemoAwareSocket = function (url: string | URL, protocols?: string | string[]) {
    const m = /\/v1\/jobs\/([^/]+)\/logs\/ws/.exec(String(url));
    const id = m === null ? null : decodeURIComponent(m[1] ?? "");
    if (id !== null && id.startsWith("job_demo_")) return new DemoLogSocket(id);
    return new RealSocket(url, protocols);
  } as unknown as typeof WebSocket;
  Object.assign(DemoAwareSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  window.WebSocket = DemoAwareSocket;
  let plannerWasMoving = demoPlannerMoving();
  setInterval(() => {
    // the list and, on a node page, its detail, live samples and disks (only active queries refetch)
    void client.invalidateQueries({ queryKey: ["nodes"] });
    void client.invalidateQueries({ queryKey: ["network"], exact: true });
    void client.invalidateQueries({ queryKey: ["jobs", "job_demo_split", "split"], exact: true });
    // benchmarks: no campaign.progress events for the demo campaigns, so the
    // running ones are refreshed here (the list, every status, the open detail);
    // their runs / labels / quality follow through useRefreshOnRunEnd
    const moving = demoMovingCampaigns();
    if (moving.length > 0) {
      void client.invalidateQueries({ queryKey: ["bench", "list"], exact: true });
      void client.invalidateQueries({ queryKey: ["bench", "status"] });
      for (const id of moving) void client.invalidateQueries({ queryKey: ["bench", "campaign", id, "detail"], exact: true });
    }
    // planner: a running evaluation / retrain is polled by the page itself; once it
    // ends, the headline figures and lists are read again
    if (plannerWasMoving && !demoPlannerMoving()) void client.invalidateQueries({ queryKey: ["planner"] });
    plannerWasMoving = demoPlannerMoving();
  }, 3_000);
  document.documentElement.dataset.demo = "1";
}
