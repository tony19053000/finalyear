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
