import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { PRODUCT_NAME } from "../config";
import { cn } from "../lib/cn";
import { useSession } from "../api/hooks";
import { BenchmarksIcon, JobsIcon, LogoMark, NodesIcon, OverviewIcon, SettingsIcon } from "./icons";

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  end?: boolean;
  /** Shown to admins only (the API refuses everyone else). */
  adminOnly?: boolean;
}

/** The dashboard's sections. Mode 3/4 pages join in later versions. */
const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Overview", icon: <OverviewIcon />, end: true },
  { to: "/nodes", label: "Nodes", icon: <NodesIcon /> },
  { to: "/jobs", label: "Jobs", icon: <JobsIcon /> },
  { to: "/benchmarks", label: "Benchmarks", icon: <BenchmarksIcon />, adminOnly: true },
  { to: "/settings", label: "Settings", icon: <SettingsIcon /> },
];

export function Sidebar() {
  const isAdmin = useSession().data?.user.role === "admin";
  return (
    <aside className="flex w-52 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex h-12 items-center gap-2 border-b border-border px-4">
        <LogoMark />
        <span className="text-base font-semibold tracking-tight">{PRODUCT_NAME}</span>
      </div>
      <nav aria-label="Main" className="flex flex-col gap-0.5 p-2">
        {NAV_ITEMS.filter((item) => isAdmin || item.adminOnly !== true).map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              cn(
                "flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm",
                isActive ? "bg-accent-soft font-medium text-fg" : "text-fg-muted hover:bg-surface-raised hover:text-fg",
              )
            }
          >
            {({ isActive }) => (
              <>
                <span className={isActive ? "text-accent" : undefined}>{item.icon}</span>
                {item.label}
              </>
            )}
          </NavLink>
        ))}
      </nav>
    </aside>
  );
}
