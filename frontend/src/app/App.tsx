import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { Component, Suspense, lazy, useEffect, useState, type ErrorInfo, type ReactNode } from "react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { Button } from "../components/Button";
import { ToastProvider } from "../components/Toast";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/PlaceholderPages";
import { AppShell, type AppShellProps } from "./AppShell";
import { RequireSession } from "./RequireSession";

/*
 * Route-level code splitting (V0.8 G5, D195): every page behind the shell is
 * its own chunk; the login page and the shell stay in the entry chunk so the
 * first paint never waits on a second request. The chunks are same-origin
 * files, so the CSP (`script-src 'self'`) is unchanged.
 *
 * Once signed in, every page chunk is loaded in the background
 * (`PreloadPages`): the shell keeps working through a controller restart
 * (G2 T-2), and navigating then must not need a file from the controller. If a
 * chunk still cannot be loaded, `PageLoadBoundary` says so instead of a blank
 * page.
 */
const PAGE_LOADERS = {
  overview: () => import("../pages/overview/OverviewPage"),
  nodes: () => import("../pages/nodes/NodesPage"),
  nodeDetail: () => import("../pages/nodes/NodeDetailPage"),
  // V0.8 G4: Jobs pages (routes in the marked block below).
  jobs: () => import("../pages/jobs/JobsListPage"),
  jobDetail: () => import("../pages/jobs/JobDetailPage"),
  submit: () => import("../pages/jobs/submit/SubmitJobPage"),
  // V0.8 G5: Settings (account, tokens, invites, users, secrets, data).
  settings: () => import("../pages/settings/SettingsRoutes"),
  // V0.9 G6: Benchmarks (admin only; recharts loads with these chunks).
  benchmarks: () => import("../pages/benchmarks/BenchmarksPage"),
  campaign: () => import("../pages/benchmarks/CampaignDetailPage"),
  // V0.14 G5: Planner (calibration + evaluation reports; recharts like Benchmarks).
  planner: () => import("../pages/planner/PlannerPage"),
};

const OverviewPage = lazy(() => PAGE_LOADERS.overview().then((m) => ({ default: m.OverviewPage })));
const NodesPage = lazy(() => PAGE_LOADERS.nodes().then((m) => ({ default: m.NodesPage })));
const NodeDetailPage = lazy(() => PAGE_LOADERS.nodeDetail().then((m) => ({ default: m.NodeDetailPage })));
const JobsListPage = lazy(() => PAGE_LOADERS.jobs().then((m) => ({ default: m.JobsListPage })));
const JobDetailPage = lazy(() => PAGE_LOADERS.jobDetail().then((m) => ({ default: m.JobDetailPage })));
const SubmitJobPage = lazy(() => PAGE_LOADERS.submit().then((m) => ({ default: m.SubmitJobPage })));
const BenchmarksPage = lazy(() => PAGE_LOADERS.benchmarks().then((m) => ({ default: m.BenchmarksPage })));
const CampaignDetailPage = lazy(() => PAGE_LOADERS.campaign().then((m) => ({ default: m.CampaignDetailPage })));
const PlannerPage = lazy(() => PAGE_LOADERS.planner().then((m) => ({ default: m.PlannerPage })));
const SettingsRoutes = lazy(() => PAGE_LOADERS.settings().then((m) => ({ default: m.SettingsRoutes })));

/**
 * Load every page chunk once the shell is up (errors are left to the boundary).
 * A failed preload is one console warning (D204, V0.8 G7; never `console.debug`:
 * the G2 tester bans log/debug/info in sources): it is not an
 * error - the page's own load retries when it is opened - but it is visible to
 * whoever debugs a "page could not be loaded".
 */
export function PreloadPages({
  loaders = PAGE_LOADERS,
}: {
  loaders?: Readonly<Record<string, () => Promise<unknown>>>;
}) {
  useEffect(() => {
    for (const [page, load] of Object.entries(loaders)) {
      load().catch((err: unknown) => {
        console.warn("page preload failed; it loads again when opened", page, err instanceof Error ? err.message : String(err));
      });
    }
  }, [loaders]);
  return null;
}

/** What a page shows while its chunk loads (normally a few milliseconds). */
function PageLoading() {
  return (
    <p role="status" className="text-sm text-fg-muted">
      Loading…
    </p>
  );
}

interface BoundaryState {
  failed: boolean;
}

/** A page chunk that could not be loaded (controller unreachable): say so, offer a reload. */
export class PageLoadBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  override state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.warn("page failed to load", error.message, info.componentStack?.slice(0, 200));
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="flex flex-col items-start gap-2 rounded-lg border border-status-failed bg-status-failed-soft px-4 py-3">
        <h1 className="text-sm font-semibold text-status-failed">This page could not be loaded</h1>
        <p className="text-sm text-fg">
          The dashboard could not fetch this page from the controller. Check that the controller is running, then reload.
        </p>
        <Button
          size="sm"
          onClick={() => {
            window.location.reload();
          }}
        >
          Reload
        </Button>
      </div>
    );
  }
}

function Page({ children }: { children: ReactNode }) {
  return (
    <PageLoadBoundary>
      <Suspense fallback={<PageLoading />}>{children}</Suspense>
    </PageLoadBoundary>
  );
}

export function AppRoutes({ socketFactory }: AppShellProps) {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireSession>
            <PreloadPages />
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
        {/* ---- V0.9 G6: Benchmarks (campaigns list, campaign detail) ---- */}
        <Route path="benchmarks" element={<Page><BenchmarksPage /></Page>} />
        <Route path="benchmarks/:campaignId" element={<Page><CampaignDetailPage /></Page>} />
        {/* ---- end V0.9 G6 ---- */}
        {/* ---- V0.14 G5: Planner (calibration, evaluation reports) ---- */}
        <Route path="planner" element={<Page><PlannerPage /></Page>} />
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
