import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { HiveMark, JobsIcon, OverviewIcon, SettingsIcon } from "../app/icons";
import { ToastProvider } from "../components/Toast";
import { cn } from "../lib/cn";
import { ConsoleApiError, consoleApi, consoleKeys, explain, type StatusView } from "./api";
import { clearFragment, readCredential } from "./bootstrap";
import { HiveBackdrop } from "../app/HiveBackdrop";
import { Dot, Problem } from "./components/fields";
import { connectionSummary } from "./logic";
import { Home } from "./pages/Home";
import { Wizard } from "./pages/Wizard";

// Pages behind Home load as their own chunks (same-origin files: CSP unchanged).
const Settings = lazy(() => import("./pages/Settings").then((m) => ({ default: m.Settings })));
const Logs = lazy(() => import("./pages/Logs").then((m) => ({ default: m.Logs })));
const Leave = lazy(() => import("./pages/Leave").then((m) => ({ default: m.Leave })));

function createClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (count, error) => !(error instanceof ConsoleApiError && error.status < 500) && count < 2,
        refetchOnWindowFocus: true,
      },
      mutations: { retry: false },
    },
  });
}

type Access = "exchanging" | "ready" | "expired";

/**
 * One exchange per credential, even when React runs the effect twice
 * (StrictMode): the launcher's code is single-use, so a second POST would fail.
 */
const exchanges = new Map<string, Promise<unknown>>();

function exchangeOnce(credential: { code: string }): Promise<unknown> {
  const key = credential.code;
  let pending = exchanges.get(key);
  if (pending === undefined) {
    pending = consoleApi.exchange(credential);
    exchanges.set(key, pending);
  }
  return pending;
}

/** Trade the launcher's `#c=` for the session cookie, once (bootstrap.ts). */
function useAccess(): Access {
  const [credential] = useState(() => readCredential(window.location.hash));
  const [access, setAccess] = useState<Access>(credential === null ? "ready" : "exchanging");
  useEffect(() => {
    if (window.location.hash !== "") clearFragment();
    if (credential === null) return;
    exchangeOnce(credential).then(
      () => {
        setAccess("ready");
      },
      () => {
        setAccess("expired");
      },
    );
  }, [credential]);
  return access;
}

/** The hive lockup (the dashboard's Brand, without a link: this page is its own app). */
function Lockup() {
  return (
    <span className="flex shrink-0 items-center gap-2.5 text-fg">
      <HiveMark size={26} />
      <span className="inline-flex items-baseline font-display text-[25px] leading-none font-bold tracking-[-0.045em]">
        hive
        <span
          aria-hidden="true"
          className="ml-[0.06em] inline-block h-[0.34em] w-[0.3em] bg-accent [clip-path:polygon(50%_0,100%_25%,100%_75%,50%_100%,0_75%,0_25%)]"
        />
      </span>
    </span>
  );
}

/** A page with only the comb and one centred panel (signed out, not answering, opening). */
function Bare({ children }: { children: ReactNode }) {
  return (
    <div className="mf-app ac-app relative flex min-h-screen flex-col items-center justify-center bg-bg px-4 text-fg">
      <HiveBackdrop />
      <div className="ac-in relative z-10 flex w-full max-w-lg flex-col items-center gap-8">
        <Lockup />
        {children}
      </div>
    </div>
  );
}

function SignedOut({ expired }: { expired: boolean }) {
  return (
    <Bare>
      <main className="ov-card ov-frost nd-glass ac-panel w-full">
        <div className="ac-panel-body flex flex-col gap-5">
          <h1 className="font-display text-[1.75rem] leading-tight font-bold tracking-[-0.03em] text-fg">MeshForge on this laptop</h1>
          <Problem
            title={expired ? "This link has expired" : "You are not signed in"}
            message={
              expired
                ? "Console links work once and only for a minute."
                : "This page opens from the MeshForge entry in your app menu, which signs you in."
            }
            fix="Open MeshForge from the app menu again. (Only users allowed to manage MeshForge on this laptop can.)"
          />
        </div>
      </main>
    </Bare>
  );
}

const NAV = [
  { to: "/", label: "Home", end: true, icon: <OverviewIcon /> },
  { to: "/settings", label: "Settings", end: false, icon: <SettingsIcon /> },
  { to: "/logs", label: "Logs", end: false, icon: <JobsIcon /> },
  { to: "/leave", label: "Leave", end: false, icon: null },
];

function Shell({ status, children }: { status: StatusView; children: ReactNode }) {
  const summary = connectionSummary(status);
  const tone = summary.tone === "ok" ? "ok" : summary.tone === "failed" ? "failed" : summary.tone === "degraded" ? "warn" : summary.tone === "busy" ? "honey" : "off";
  return (
    <div className="mf-app ac-app relative flex min-h-screen flex-col bg-bg text-fg">
      <HiveBackdrop />
      <header className="relative z-10 flex shrink-0 flex-wrap items-center gap-x-6 gap-y-4 px-6 pt-6 pb-3 lg:grid lg:grid-cols-[1fr_auto_1fr]">
        <div className="flex min-w-0 items-center gap-4 lg:justify-self-start">
          <Lockup />
          <span className="ac-host hidden min-w-0 truncate sm:inline">{status.node_name !== "" ? status.node_name : status.hostname}</span>
        </div>
        {status.joined && status.setup_complete ? (
          <nav aria-label="Console" className="order-last w-full max-w-full min-w-0 overflow-x-auto [scrollbar-width:none] lg:order-none lg:w-auto lg:justify-self-center">
            <ul className="flex items-center gap-2 p-0.5">
              {NAV.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      cn(
                        "flex h-11 items-center gap-2 rounded-full border px-5 text-sm whitespace-nowrap transition-colors",
                        isActive
                          ? "border-accent bg-surface font-medium text-accent"
                          : "border-border bg-surface text-fg-muted hover:border-border-strong hover:text-fg",
                      )
                    }
                  >
                    {item.icon}
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        ) : (
          <span className="hidden lg:block" />
        )}
        <div className="ml-auto flex items-center gap-5 lg:ml-0 lg:justify-self-end">
          {status.joined && (
            <span className="ac-conn hidden sm:inline-flex">
              <Dot tone={tone}>{summary.label}</Dot>
            </span>
          )}
        </div>
      </header>
      <main className="ac-in relative z-10 mx-auto w-full max-w-6xl flex-1 px-6 pt-6 pb-16">{children}</main>
    </div>
  );
}

function Console() {
  const access = useAccess();
  const status = useQuery({
    queryKey: consoleKeys.status,
    queryFn: consoleApi.status,
    enabled: access === "ready",
    refetchInterval: 3000,
  });
  if (access === "expired") return <SignedOut expired />;
  if (access === "exchanging" || status.isPending) {
    return (
      <Bare>
        <p role="status" className="ac-loading">
          Opening MeshForge…
        </p>
      </Bare>
    );
  }
  if (status.isError) {
    if (status.error instanceof ConsoleApiError && status.error.status === 401) return <SignedOut expired={false} />;
    const { message, fix } = explain(status.error);
    return (
      <Bare>
        <main className="w-full">
          <Problem title="MeshForge on this laptop does not answer" message={message} fix={fix || "It may be restarting; this page retries by itself."} />
        </main>
      </Bare>
    );
  }
  const s = status.data;
  if (!s.joined || !s.setup_complete) {
    return (
      <Shell status={s}>
        <Wizard status={s} />
      </Shell>
    );
  }
  return (
    <Shell status={s}>
      <Suspense fallback={<p role="status" className="ac-loading">Loading…</p>}>
        <Routes>
          <Route path="/settings" element={<Settings />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/leave" element={<Leave status={s} />} />
          <Route path="*" element={<Home status={s} />} />
        </Routes>
      </Suspense>
    </Shell>
  );
}

/** Under the vite dev server the console is opened at its html file; links stay under it. */
const BASENAME = import.meta.env.DEV && window.location.pathname.startsWith("/src/agentConsole/index.html") ? "/src/agentConsole/index.html" : undefined;

/** The laptop owner's console (V0.17 G3). */
export function ConsoleApp({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? createClient());
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter basename={BASENAME}>
          <Console />
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}
