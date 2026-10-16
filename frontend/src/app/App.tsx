import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { ToastProvider } from "../components/Toast";
import { LoginPage } from "../pages/LoginPage";
import { NodesPage, NotFoundPage, OverviewPage } from "../pages/PlaceholderPages";
import { SettingsPage } from "../pages/SettingsPage";
// V0.8 G4: Jobs pages (routes in the marked block below).
import { JobDetailPage } from "../pages/jobs/JobDetailPage";
import { JobsListPage } from "../pages/jobs/JobsListPage";
import { SubmitJobPage } from "../pages/jobs/submit/SubmitJobPage";
import { AppShell, type AppShellProps } from "./AppShell";
import { RequireSession } from "./RequireSession";

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
        <Route index element={<OverviewPage />} />
        <Route path="nodes" element={<NodesPage />} />
        {/* ---- V0.8 G4: Jobs (list, submit, detail) ---- */}
        <Route path="jobs" element={<JobsListPage />} />
        <Route path="jobs/new" element={<SubmitJobPage />} />
        <Route path="jobs/:jobId" element={<JobDetailPage />} />
        {/* ---- end V0.8 G4 ---- */}
        <Route path="settings" element={<SettingsPage />} />
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
