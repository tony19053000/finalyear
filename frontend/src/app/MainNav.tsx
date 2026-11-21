import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";

import { cn } from "../lib/cn";
import { useSession } from "../api/hooks";
import { BenchmarksIcon, HiveMark, JobsIcon, NodesIcon, OverviewIcon, PlannerIcon, SettingsIcon } from "./icons";

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

/** The landing page's lockup: hive mark + "hive" wordmark with the honey hex dot. */
export function Brand() {
  return (
    <Link to="/" aria-label="hive home" className="flex shrink-0 items-center gap-2.5 rounded-lg text-fg no-underline">
      <HiveMark size={26} />
      <span className="inline-flex items-baseline font-display text-[25px] leading-none font-bold tracking-[-0.045em]">
        hive
        <span
          aria-hidden="true"
          className="ml-[0.06em] inline-block h-[0.34em] w-[0.3em] bg-accent [clip-path:polygon(50%_0,100%_25%,100%_75%,50%_100%,0_75%,0_25%)]"
        />
      </span>
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
