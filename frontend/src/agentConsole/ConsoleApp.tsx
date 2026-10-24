import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { applyTheme, saveTheme, storedTheme, type Theme } from "../app/theme";
import { Button } from "../components/Button";
import { ToastProvider } from "../components/Toast";
import { cn } from "../lib/cn";
import { ConsoleApiError, consoleApi, consoleKeys, explain, type StatusView } from "./api";
import { clearFragment, readCredential } from "./bootstrap";
import { Problem } from "./components/fields";
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

function exchangeOnce(credential: { code?: string; token?: string }): Promise<unknown> {
  const key = credential.code !== undefined ? `c:${credential.code}` : `t:${credential.token ?? ""}`;
  let pending = exchanges.get(key);
  if (pending === undefined) {
    pending = consoleApi.exchange(credential);
    exchanges.set(key, pending);
  }
  return pending;
}

/** Trade the launcher's `#c=` / `#t=` for the session cookie, once (bootstrap.ts). */
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

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(() => storedTheme());
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <Button
      variant="ghost"
      size="sm"
      aria-label={`Switch to ${next} theme`}
      onClick={() => {
        applyTheme(next);
        saveTheme(next);
        setTheme(next);
      }}
    >
      {theme === "dark" ? "Light" : "Dark"}
    </Button>
  );
}

function SignedOut({ expired }: { expired: boolean }) {
  return (
    <main className="mx-auto flex max-w-xl flex-col gap-3 p-6">
      <h1 className="text-lg font-semibold text-fg">MeshForge on this laptop</h1>
      <Problem
        title={expired ? "This link has expired" : "You are not signed in"}
        message={
          expired
            ? "Console links work once and only for a minute."
            : "This page opens from the MeshForge entry in your app menu, which signs you in."
        }
        fix="Open MeshForge from the app menu again. (Only users allowed to manage MeshForge on this laptop can.)"
      />
    </main>
  );
}

const NAV = [
  { to: "/", label: "Home", end: true },
  { to: "/settings", label: "Settings", end: false },
  { to: "/logs", label: "Logs", end: false },
  { to: "/leave", label: "Leave", end: false },
];

function Shell({ status, children }: { status: StatusView; children: ReactNode }) {
  return (
    <div className="min-h-screen bg-bg text-fg">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2">
        <div className="min-w-0">
          <span className="text-sm font-semibold">MeshForge</span>
          <span className="ml-2 truncate text-sm text-fg-muted">{status.node_name !== "" ? status.node_name : status.hostname}</span>
        </div>
        <ThemeToggle />
      </header>
      {status.joined && status.setup_complete && (
        <nav aria-label="Console" className="flex gap-1 overflow-x-auto border-b border-border px-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "border-b-2 px-3 py-2 text-sm whitespace-nowrap",
                  isActive ? "border-accent text-fg" : "border-transparent text-fg-muted hover:text-fg",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      )}
      <main className="mx-auto w-full max-w-4xl p-4">{children}</main>
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
      <p role="status" className="p-6 text-sm text-fg-muted">
        Opening MeshForge…
      </p>
    );
  }
  if (status.isError) {
    if (status.error instanceof ConsoleApiError && status.error.status === 401) return <SignedOut expired={false} />;
    const { message, fix } = explain(status.error);
    return (
      <main className="mx-auto max-w-xl p-6">
        <Problem title="MeshForge on this laptop does not answer" message={message} fix={fix || "It may be restarting; this page retries by itself."} />
      </main>
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
      <Suspense fallback={<p role="status" className="text-sm text-fg-muted">Loading…</p>}>
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

/** The laptop owner's console (V0.17 G3). */
export function ConsoleApp({ client }: { client?: QueryClient }) {
  const [queryClient] = useState(() => client ?? createClient());
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter>
          <Console />
        </BrowserRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}
