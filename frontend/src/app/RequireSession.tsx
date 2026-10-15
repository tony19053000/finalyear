import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSession } from "../api/hooks";
import { ErrorState } from "../components/States";
import type { LoginState } from "../pages/loginMessages";

/**
 * Renders `children` only with a live browser session. No session -> the
 * login page (remembering where the user was, in router state - not the
 * URL). The controller is unreachable -> say so, with a retry; never a
 * blank page and never a redirect loop.
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
  if (session.data === null) {
    const state: LoginState = { from: `${location.pathname}${location.search}` };
    return <Navigate to="/login" replace state={state} />;
  }
  return <>{children}</>;
}
