import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { Suspense, lazy, useState, type ReactNode } from "react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { ToastProvider } from "../components/Toast";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/PlaceholderPages";
import { AppShell, type AppShellProps } from "./AppShell";
import { RequireSession } from "./RequireSession";

/*
 * Route-level code splitting (V0.8 G5, D195): every page behind the shell is
 * its own chunk, loaded on first visit; the login page and the shell stay in
 * the entry chunk so the first paint never waits on a second request. The
 * chunks are same-origin files, so the CSP (`script-src 'self'`) is unchanged.
 */
const OverviewPage = lazy(() => import("../pages/overview/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const NodesPage = lazy(() => import("../pages/nodes/NodesPage").then((m) => ({ default: m.NodesPage })));
const NodeDetailPage = lazy(() => import("../pages/nodes/NodeDetailPage").then((m) => ({ default: m.NodeDetailPage })));
// V0.8 G4: Jobs pages (routes in the marked block below).
const JobsListPage = lazy(() => import("../pages/jobs/JobsListPage").then((m) => ({ default: m.JobsListPage })));
const JobDetailPage = lazy(() => import("../pages/jobs/JobDetailPage").then((m) => ({ default: m.JobDetailPage })));
const SubmitJobPage = lazy(() => import("../pages/jobs/submit/SubmitJobPage").then((m) => ({ default: m.SubmitJobPage })));
// V0.8 G5: Settings (account, tokens, invites, users, secrets, data, fabric).
const SettingsRoutes = lazy(() => import("../pages/settings/SettingsRoutes").then((m) => ({ default: m.SettingsRoutes })));

/** What a page shows while its chunk loads (normally a few milliseconds). */
function PageLoading() {
  return (
    <p role="status" className="text-sm text-fg-muted">
      Loading…
    </p>
  );
}

function Page({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PageLoading />}>{children}</Suspense>;
}

export function AppRoutes({ socketFactory }: AppShellProps) {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireSession>
            <AppShell socketFactory={socketFactory} />
          </RequireSession>
        }
      >
        <Route index element={<Page><OverviewPage /></Page>} />
        <Route path="nodes" element={<Page><NodesPage /></Page>} />
        <Route path="nodes/:nodeId" element={<Page><NodeDetailPage /></Page>} />
        {/* ---- V0.8 G4: Jobs (list, submit, detail) ---- */}
        <Route path="jobs" element={<Page><JobsListPage /></Page>} />
        <Route path="jobs/new" element={<Page><SubmitJobPage /></Page>} />
        <Route path="jobs/:jobId" element={<Page><JobDetailPage /></Page>} />
        {/* ---- end V0.8 G4 ---- */}
        {/* ---- V0.8 G5: Settings (nested routes in pages/settings) ---- */}
        <Route path="settings/*" element={<Page><SettingsRoutes /></Page>} />
        {/* ---- end V0.8 G5 ---- */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export interface AppProps extends AppShellProps {
  /** Tests: a fresh client and an in-memory router at `initialPath`. */
  queryClient?: QueryClient;
  initialPath?: string;
}

/** Providers + routes. Production renders it with no props. */
export function App({ queryClient, initialPath, socketFactory }: AppProps) {
  const [client] = useState(() => queryClient ?? createQueryClient());
  const routes = <AppRoutes socketFactory={socketFactory} />;
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        {initialPath !== undefined ? (
          <MemoryRouter initialEntries={[initialPath]}>{routes}</MemoryRouter>
        ) : (
          <BrowserRouter>{routes}</BrowserRouter>
        )}
      </ToastProvider>
    </QueryClientProvider>
  );
}
