import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { setUnauthorizedHandler } from "../api/client";
import { LOGOUT_MUTATION_KEY } from "../api/hooks";
import { queryKeys } from "../api/queryKeys";
import { useEventStream, type UseEventStreamOptions } from "../events/useEventStream";
import type { LoginState } from "../pages/loginMessages";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

export interface AppShellProps {
  /** Tests inject a fake socket; production uses the browser WebSocket. */
  socketFactory?: UseEventStreamOptions["socketFactory"];
}

/**
 * The signed-in frame: skip link, sidebar navigation, header (fabric +
 * connection status, user, theme, logout) and the page. Owns the app's one
 * event stream and what a dead session does (-> login page, reason shown).
 */
export function AppShell({ socketFactory }: AppShellProps) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  // A ref, so the callback (and with it the event stream) stays the same
  // across navigation - the stream must not reconnect on every page change.
  // (react-router's `navigate` can change identity with the location, too.)
  const here = useRef("/");
  const navigateRef = useRef(navigate);
  useEffect(() => {
    here.current = `${location.pathname}${location.search}`;
    navigateRef.current = navigate;
  }, [location.pathname, location.search, navigate]);

  const sessionEnded = useCallback(() => {
    // Our own logout closes the event stream with 4401 too: not an expiry.
    if (client.isMutating({ mutationKey: LOGOUT_MUTATION_KEY }) > 0) return;
    const state: LoginState = { from: here.current, reason: "expired" };
    void navigateRef.current("/login", { replace: true, state });
    client.clear();
    client.setQueryData(queryKeys.session, null);
  }, [client]);

  useEffect(() => {
    setUnauthorizedHandler(sessionEnded);
    return () => {
      setUnauthorizedHandler(null);
    };
  }, [sessionEnded]);

  const streamStatus = useEventStream({ enabled: true, onUnauthorized: sessionEnded, socketFactory });

  return (
    <div className="flex min-h-screen bg-bg text-fg">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-surface-raised focus:px-3 focus:py-2"
      >
        Skip to content
      </a>
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header streamStatus={streamStatus} />
        <main id="main" tabIndex={-1} className="flex-1 px-6 py-5 outline-none">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
