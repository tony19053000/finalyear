import type { ReactNode } from "react";

import { useSession } from "../../api/hooks";
import { EmptyState } from "../../components/States";

/**
 * The Benchmarks pages are admin-only on the server (every /v1/bench route is
 * 403 for a user: a campaign is job submission at scale). A user who opens one
 * gets the dashboard's standard "admins only" state instead of a call that
 * can only be refused.
 */
export function AdminOnly({ children }: { children: ReactNode }) {
  const session = useSession();
  if (session.data === undefined) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading…
      </p>
    );
  }
  if (session.data?.user.role !== "admin") {
    return (
      <EmptyState title="Benchmarks are run by admins">
        Benchmark campaigns submit jobs to every laptop of the fabric, so only a fabric admin can see and control them.
        Ask an admin, or use <code className="font-mono">compute bench</code> with an admin account.
      </EmptyState>
    );
  }
  return <>{children}</>;
}
