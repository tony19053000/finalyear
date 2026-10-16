import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, MemoryRouter, Route, Routes } from "react-router-dom";

import { createQueryClient } from "../api/queryClient";
import { ToastProvider } from "../components/Toast";
import { LoginPage } from "../pages/LoginPage";
import { NodeDetailPage } from "../pages/nodes/NodeDetailPage";
import { NodesPage } from "../pages/nodes/NodesPage";
import { OverviewPage } from "../pages/overview/OverviewPage";
import { JobsPage, NotFoundPage } from "../pages/PlaceholderPages";
import { SettingsPage } from "../pages/SettingsPage";
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
        <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
        <Route path="jobs" element={<JobsPage />} />
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
