import { useId, type HTMLAttributes, type ReactNode } from "react";

import { cn } from "../../lib/cn";

export interface OverviewCardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  title: ReactNode;
  /** Keep the title for screen readers only (the content is the hero). */
  hideTitle?: boolean;
  /** Round icon buttons / links, held in the notched top-right corner. */
  actions?: ReactNode;
  /** The one honey highlight card. */
  honey?: boolean;
}

/**
 * Frame B card: matte grey (or honey) ground with a faint grain, 28px
 * corners, title top-left; a card with actions gets the notched top-right
 * cut-out holding its round buttons.
 */
export function OverviewCard({ title, actions, honey = false, hideTitle = false, className, children, ...rest }: OverviewCardProps) {
  const titleId = useId();
  return (
    <section
      aria-labelledby={titleId}
      className={cn("ov-card flex min-w-0 flex-col p-6", honey && "ov-card-honey", className)}
      {...rest}
    >
      <header className={cn(hideTitle ? "sr-only" : "flex min-h-9 items-start", !hideTitle && actions !== undefined && "pr-24")}>
        <h2 id={titleId} className="ov-card-title pt-1">
          {title}
        </h2>
      </header>
      {actions !== undefined && <div className="ov-notch">{actions}</div>}
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </section>
  );
}

/** The ↗ glyph of frame B's corner buttons. */
export function ArrowUpRight({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" className={className}>
      <path d="M5 11L11 5M6 5h5v5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Frame B's bell (goes to the Alerts card). */
export function BellIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" className={className}>
      <path
        d="M4 11V7.5a4 4 0 0 1 8 0V11l1 1.2H3L4 11zM6.6 13.6a1.5 1.5 0 0 0 2.8 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** The bell button: opens the card the same way the arrow does, showing what the card shows. */
export function InfoBell({ open, title, onClick, dark = false }: { open: boolean; title: string; onClick?: () => void; dark?: boolean }) {
  return (
    <button
      type="button"
      className={cn("ov-icon-btn", open && (dark ? "ov-icon-btn-dark" : "ov-icon-btn-honey"))}
      aria-label={open ? `Close what ${title} shows` : `What ${title} shows`}
      aria-pressed={open}
      onClick={onClick}
    >
      <BellIcon />
    </button>
  );
}

/** What a card shows, in points: the opened card's right side when the bell opened it. */
export function InfoPoints({ title, points, testId }: { title: string; points: readonly string[]; testId?: string }) {
  return (
    <div className="ov-speed-more flex min-w-0 flex-1 flex-col pr-24" data-testid={testId}>
      <ul aria-label={`What ${title} shows`} className="ov-info-list ov-scroll ov-scroll-none mt-1 min-h-0 flex-1 overflow-y-auto">
        {points.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
    </div>
  );
}

/** The bell button: jumps to the Alerts card. */
export function AlertsBell() {
  return (
    <a href="#ov-alerts" className="ov-icon-btn" aria-label="Go to alerts">
      <BellIcon />
    </a>
  );
}
