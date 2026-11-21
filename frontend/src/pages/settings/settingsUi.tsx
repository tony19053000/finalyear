/**
 * Presentation pieces of the Settings pages - a calm settings layout, unlike
 * the dashboard pages: no frosted cards, one centred column open on the page.
 * Each section starts with a plain intro sentence and its primary action (a
 * honey button with words); lists are simple hairline rows with a small sans
 * header and a "⋯" menu per row; facts are label/value rows under a left
 * label column. Presentation only - no data rules live here.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { formatTime } from "../../components/jobs/format";
import { cn } from "../../lib/cn";
import { dash } from "../benchmarks/benchShared";

/* ------------------------------------------------------------ intro + action */

/** A section's primary action: a solid honey button with a plus and its words. */
export function AddButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" className="st-primary" onClick={onClick}>
      <svg aria-hidden="true" viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
        <path d="M12 5v14M5 12h14" />
      </svg>
      {label}
    </button>
  );
}

/** The sub-page's opening line (muted, plain words) with its action(s) on the right. */
export function Intro({ children, actions }: { children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="st-intro">
      <p className="st-intro-text">{children}</p>
      {actions !== undefined && <div className="flex shrink-0 items-center gap-6">{actions}</div>}
    </div>
  );
}

/** A quiet line under a list (counts, the one explanation that matters); longer words go to its tooltip. */
export function Footnote({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <p className={cn("st-foot", title !== undefined && "cursor-help")} title={title}>
      {children}
    </p>
  );
}

/** A loading line in the page's own voice. */
export function Loading() {
  return (
    <p role="status" className="py-6 text-sm text-fg-muted">
      Loading…
    </p>
  );
}

/* ------------------------------------------------------------ sections */

/**
 * A classic settings section: the title and one line of description on the
 * left, the content on the right; sections are separated by a hairline.
 */
export function Section({
  title,
  description,
  hint,
  children,
  testId,
}: {
  title: string;
  description?: ReactNode;
  hint?: string;
  children: ReactNode;
  testId?: string;
}) {
  const id = useId();
  return (
    <section className="st-section" aria-labelledby={id} data-testid={testId}>
      <div className="st-section-head">
        <h2 id={id} className={cn("st-section-title", hint !== undefined && "cursor-help")} title={hint}>
          {title}
        </h2>
        {description !== undefined && <p className="st-section-desc">{description}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

/** Label/value rows (a settings form's read-only fields), hairlines between them. */
export function Facts({ children }: { children: ReactNode }) {
  return <dl className="st-facts">{children}</dl>;
}

export function FactRow({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="st-fact">
      <dt className={cn(hint !== undefined && "cursor-help")} title={hint}>
        {label}
      </dt>
      <dd>{children}</dd>
    </div>
  );
}

/* ------------------------------------------------------------ list */

export interface StColumn<T> {
  id: string;
  header: ReactNode;
  /** Tooltip on the header (an explanation of the column). */
  hint?: string;
  cell: (row: T) => ReactNode;
  align?: "right";
  /** The column that takes the free width and may wrap. */
  grow?: boolean;
  mono?: boolean;
}

/**
 * The settings list: a real table (caption for screen readers) drawn as plain
 * rows - a small sans header, a hairline under it and between rows.
 */
export function SettingsList<T>({
  caption,
  columns,
  rows,
  rowKey,
  empty,
}: {
  caption: string;
  columns: readonly StColumn<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  empty: ReactNode;
}) {
  return (
    <table className="st-list">
      <caption className="sr-only">{caption}</caption>
      <thead>
        <tr>
          {columns.map((c) => (
            <th
              key={c.id}
              scope="col"
              title={c.hint}
              className={cn(c.align === "right" && "text-right", c.hint !== undefined && "cursor-help", c.grow === true && "st-grow")}
            >
              {c.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={rowKey(row)}>
            {columns.map((c) => (
              <td
                key={c.id}
                className={cn(c.align === "right" && "text-right", c.grow === true && "st-grow", c.mono === true && "st-mono")}
              >
                {c.cell(row)}
              </td>
            ))}
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={columns.length} className="st-empty">
              {empty}
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

/* ------------------------------------------------------------ row menu */

export interface MenuItem {
  key: string;
  label: string;
  /** Accessible name when the visible word is not unique on the page (e.g. "Revoke laptop (tok_A)"). */
  name?: string;
  danger?: boolean;
  onSelect: () => void;
}

/**
 * A row's actions behind a "⋯" button: a small popover of plain buttons (a
 * disclosure, not an ARIA menu). Choosing one closes it and puts the focus
 * back on "⋯" first, so a dialog the action opens returns the focus there.
 * Escape, a click outside or tabbing away closes it.
 */
export function RowMenu({ label, items, busy = false }: { label: string; items: readonly MenuItem[]; busy?: boolean }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const popId = useId();

  useEffect(() => {
    if (!open) return;
    box.current?.querySelector<HTMLButtonElement>("[data-st-item]")?.focus();
    const onDown = (event: MouseEvent) => {
      if (box.current !== null && !box.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  if (items.length === 0) return null;
  return (
    <div
      ref={box}
      className="relative inline-flex"
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
      }}
      onBlur={(event) => {
        if (open && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <button
        ref={trigger}
        type="button"
        className="st-dots"
        aria-label={label}
        title="Actions"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        aria-busy={busy || undefined}
        disabled={busy}
        onClick={() => {
          setOpen((o) => !o);
        }}
      >
        {busy ? (
          <span aria-hidden="true" className="size-3 animate-spin rounded-full border border-current border-t-transparent" />
        ) : (
          <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
            <circle cx="5" cy="12" r="1.7" />
            <circle cx="12" cy="12" r="1.7" />
            <circle cx="19" cy="12" r="1.7" />
          </svg>
        )}
      </button>
      {open && (
        <div id={popId} className="st-pop">
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              data-st-item=""
              className={cn("st-pop-item", item.danger === true && "st-pop-danger")}
              aria-label={item.name}
              onClick={() => {
                setOpen(false);
                trigger.current?.focus();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ cells */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/** "Today, 14:05" for today; otherwise "Apr 5" (with the year when it is not this year). */
function shortDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  if (date.toDateString() === now.toDateString()) return `Today, ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const day = `${MONTHS[date.getMonth()] ?? ""} ${String(date.getDate())}`;
  return date.getFullYear() === now.getFullYear() ? day : `${day}, ${String(date.getFullYear())}`;
}

/** A time in a list: short, the full stamp as its tooltip; a dash when there is none. */
export function When({ iso, suffix }: { iso: string | null | undefined; suffix?: string }) {
  if (iso == null || iso === "") return dash;
  return (
    <time className="cursor-help whitespace-nowrap" dateTime={iso} title={formatTime(iso)}>
      {shortDate(iso)}
      {suffix !== undefined && <span className="ml-1.5 text-fg-subtle">{suffix}</span>}
    </time>
  );
}

/**
 * A long id shown masked (`tok_••••1K4H`): the full id is its tooltip and is
 * what screen readers (and text search) get. Short ids are shown whole.
 */
export function MaskedId({ id }: { id: string }) {
  if (id.length <= 12) return <span title={id}>{id}</span>;
  const cut = id.indexOf("_");
  const head = cut > 0 && cut < 8 ? id.slice(0, cut + 1) : "";
  return (
    <span className="cursor-help whitespace-nowrap" title={id}>
      <span className="sr-only">{id}</span>
      <span aria-hidden="true">
        {head}
        <span className="st-mask">••••••</span>
        {id.slice(-4)}
      </span>
    </span>
  );
}

const LIGHTS: Record<string, string> = {
  ok: "bg-status-ok",
  busy: "bg-status-busy",
  degraded: "bg-status-degraded",
  failed: "bg-status-failed",
  accent: "bg-[#ffc93c]",
};

/** A state in words beside a small flat light; `tone` picks the light (unknown tones are quiet). */
export function StateWord({ tone, children }: { tone: string; children: ReactNode }) {
  return (
    <span className="st-state">
      <span aria-hidden="true" className={cn("st-light", LIGHTS[tone] ?? "bg-fg-subtle")} />
      <span>{children}</span>
    </span>
  );
}
