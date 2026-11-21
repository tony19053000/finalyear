/**
 * The few line icons the shell needs (inline SVG: no icon dependency, no
 * external file, CSP-safe - presentation attributes, not style). Decorative:
 * always next to a text label, so aria-hidden.
 */
import type { ReactNode } from "react";

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="shrink-0"
    >
      {children}
    </svg>
  );
}

export const OverviewIcon = () => (
  <Icon>
    <rect x="2" y="2" width="5" height="5" rx="1" />
    <rect x="9" y="2" width="5" height="5" rx="1" />
    <rect x="2" y="9" width="5" height="5" rx="1" />
    <rect x="9" y="9" width="5" height="5" rx="1" />
  </Icon>
);

export const NodesIcon = () => (
  <Icon>
    <rect x="2" y="2.5" width="12" height="4.5" rx="1" />
    <rect x="2" y="9" width="12" height="4.5" rx="1" />
    <path d="M4.5 4.75h.01M4.5 11.25h.01" />
  </Icon>
);

export const JobsIcon = () => (
  <Icon>
    <path d="M3 4h10M3 8h10M3 12h6" />
  </Icon>
);

export const BenchmarksIcon = () => (
  <Icon>
    <path d="M2.5 13.5h11" />
    <path d="M4 13.5V9M7 13.5V5.5M10 13.5V7.5M13 13.5V3" />
  </Icon>
);

export const SettingsIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="2.25" />
    <path d="M8 1.75v1.5M8 12.75v1.5M1.75 8h1.5M12.75 8h1.5M3.6 3.6l1.05 1.05M11.35 11.35l1.05 1.05M3.6 12.4l1.05-1.05M11.35 4.65l1.05-1.05" />
  </Icon>
);

/** V0.14 G5: the Planner page (a gauge: how good the planner's choices are). */
export const PlannerIcon = () => (
  <Icon>
    <path d="M2.5 11.5a5.5 5.5 0 0 1 11 0" />
    <path d="M8 11.5l3-4" />
    <path d="M2 13.5h12" />
  </Icon>
);

export const LogoMark = () => (
  <svg aria-hidden="true" viewBox="0 0 16 16" width="18" height="18" className="shrink-0 text-accent">
    <g fill="currentColor">
      <circle cx="4" cy="4.5" r="1.75" />
      <circle cx="12" cy="4.5" r="1.75" />
      <circle cx="8" cy="12" r="1.75" />
    </g>
    <path d="M4 4.5h8L8 12Z" fill="none" stroke="currentColor" strokeWidth="1.1" />
  </svg>
);

export const SunIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="2.75" />
    <path d="M8 1.5v1.25M8 13.25v1.25M1.5 8h1.25M13.25 8h1.25M3.4 3.4l.9.9M11.7 11.7l.9.9M3.4 12.6l.9-.9M11.7 4.3l.9-.9" />
  </Icon>
);

export const MoonIcon = () => (
  <Icon>
    <path d="M13.5 9.5A5.5 5.5 0 0 1 6.5 2.5a5.5 5.5 0 1 0 7 7Z" />
  </Icon>
);

export const BellIcon = () => (
  <Icon>
    <path d="M4 11.5V7a4 4 0 0 1 8 0v4.5l1 1H3l1-1Z" />
    <path d="M6.75 14a1.4 1.4 0 0 0 2.5 0" />
  </Icon>
);

/** Lines narrowing downwards: "filter the list". */
export const FilterIcon = () => (
  <Icon>
    <path d="M2.5 4.5h11M4.5 8h7M6.5 11.5h3" />
  </Icon>
);

const CHEVRON = { down: "M4.5 6.5 8 10l3.5-3.5", left: "M9.5 4.5 6 8l3.5 3.5", right: "M6.5 4.5 10 8l-3.5 3.5" } as const;

/** A thin chevron (dropdown, month navigation). */
export const ChevronIcon = ({ dir = "down" }: { dir?: keyof typeof CHEVRON }) => (
  <Icon>
    <path d={CHEVRON[dir]} />
  </Icon>
);

export const CheckIcon = () => (
  <Icon>
    <path d="m3.5 8.5 3 3 6-7" />
  </Icon>
);

export const SignOutIcon = () => (
  <Icon>
    <path d="M6.5 2.5h-3a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h3" />
    <path d="M10.5 11 13.5 8l-3-3M13.5 8H6.5" />
  </Icon>
);
