import type { ReactNode } from "react";

import { useSession } from "../../api/hooks";
import { EmptyState } from "../../components/States";

/**
 * The Benchmarks pages are admin-only on the server (every /v1/bench route is
 * 403 for a user: a campaign is job submission at scale). A user who opens one
 * gets the dashboard's standard "admins only" state instead of a call that
 * can only be refused. `title`: the page's big title, shown above that state
 * (an admin's page draws its own).
 */
export function AdminOnly({ children, title }: { children: ReactNode; title?: string }) {
  const session = useSession();
  const heading =
    title === undefined ? null : (
      <h1 className="mt-3 mb-10 font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">{title}</h1>
    );
  if (session.data === undefined) {
    return (
      <>
        {heading}
        <p role="status" className="text-sm text-fg-muted">
          Loading…
        </p>
      </>
    );
  }
  if (session.data?.user.role !== "admin") {
    return (
      <>
        {heading}
        <EmptyState title="Benchmarks are run by admins">
          Benchmark campaigns submit jobs to every laptop of the fabric, so only a fabric admin can see and control them.
          Ask an admin, or use <code className="font-mono">compute bench</code> with an admin account.
        </EmptyState>
      </>
    );
  }
  return <>{children}</>;
}
