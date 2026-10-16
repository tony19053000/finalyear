import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSession } from "../api/hooks";
import { ErrorState } from "../components/States";
import type { LoginState } from "../pages/loginMessages";

/**
 * Renders `children` only with a live browser session. No session -> the
 * login page (remembering where the user was, in router state - not the
 * URL). The controller unreachable before any session is known -> say so,
 * with a retry; never a blank page and never a redirect loop. Once signed
 * in, a failed re-check never unmounts the shell.
 */
export function RequireSession({ children }: { children: ReactNode }) {
  const session = useSession();
  const location = useLocation();

  if (session.isPending) {
    return (
      <div role="status" className="flex min-h-screen items-center justify-center text-sm text-fg-muted">
        Connecting to the controller…
      </div>
    );
  }
  // A failed RE-check (controller restarting, network blip) keeps the shell:
  // its event stream recovers by itself and the header shows "Reconnecting"
  // (tester T-2). Only a first load with no session yet is a full-page error.
  if (session.data) {
    return <>{children}</>;
  }
  if (session.isError) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <ErrorState
          className="w-full max-w-md"
          title="Cannot reach the controller"
          error={session.error}
          onRetry={() => {
            void session.refetch();
          }}
        />
      </div>
    );
  }
  // Loaded, no error, no session: not signed in.
  const state: LoginState = { from: `${location.pathname}${location.search}` };
  return <Navigate to="/login" replace state={state} />;
}
