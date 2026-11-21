import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

import { cn } from "../lib/cn";
import { useSession } from "../api/hooks";
import { BenchmarksIcon, JobsIcon, NodesIcon, OverviewIcon, PlannerIcon, SettingsIcon } from "./icons";
import { Wordmark, type WordmarkSize } from "./Wordmark";

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
  // V0.14 G5 (D300): calibration + offline evaluation (every signed-in user may read them)
  { to: "/planner", label: "Planner", icon: <PlannerIcon /> },
  { to: "/settings", label: "Settings", icon: <SettingsIcon /> },
];

/** The brand: the "hive." wordmark (no mark), linking home. "login" is the larger sign-in size. */
export function Brand({ size = "bar" }: { size?: WordmarkSize }) {
  return (
    <Link to="/" aria-label="Hive home" className="flex shrink-0 items-center rounded-lg text-fg no-underline">
      <Wordmark size={size} />
    </Link>
  );
}

/** The main navigation: rounded tab buttons in the top bar; the active one is outlined in honey. */
export function MainNav() {
  const isAdmin = useSession().data?.user.role === "admin";
  return (
    <nav aria-label="Main" className="max-w-full min-w-0 overflow-x-auto [scrollbar-width:none]">
      <ul className="flex items-center gap-2 p-0.5">
        {NAV_ITEMS.filter((item) => isAdmin || item.adminOnly !== true).map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "flex h-11 items-center gap-2 rounded-full border px-5 text-sm whitespace-nowrap transition-colors",
                  isActive
                    ? "border-accent bg-surface font-medium text-accent"
                    : "border-border bg-surface text-fg-muted hover:border-border-strong hover:text-fg",
                )
              }
            >
              {item.icon}
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
