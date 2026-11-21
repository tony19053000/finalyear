/**
 * Presentation pieces shared by the Benchmarks pages, in the Jobs / Nodes
 * language: frame-B text tabs, the open facts (mono label over a value), the
 * flat state light, the frosted card, the quiet note line. Presentation only - no data rules live here.
 */
import { useRef, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "../../lib/cn";

/** A flat status light (no glow), optionally breathing while work happens. */
export function Light({ className, breathe = false, small = false }: { className: string; breathe?: boolean; small?: boolean }) {
  return <span aria-hidden="true" className={cn("nd-light", small && "nd-light-sm", className, breathe && "nd-breathe")} />;
}

/** One open fact: the mono uppercase label over its value. */
export function Fact({
  label,
  children,
  className,
  testId,
}: {
  label: ReactNode;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="nd-label">{label}</dt>
      <dd className="mt-2.5 text-[0.9375rem] leading-6 break-words text-fg" data-testid={testId}>
        {children}
      </dd>
    </div>
  );
}

/** A big light figure (frame B): number + quieter unit, a mono label over it. */
export function Figure({
  label,
  value,
  unit,
  sub,
  honey = false,
  compact = false,
  className,
  testId,
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: ReactNode;
  honey?: boolean;
  /** A shorter figure with tighter spacing (the campaigns list, where the table gets the room). */
  compact?: boolean;
  className?: string;
  testId?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <dt className="nd-label">{label}</dt>
      <dd className={compact ? "mt-2" : "mt-3"}>
        <p
          className={cn("ov-figure flex items-baseline font-light", compact ? "text-[3rem]" : "text-[3.25rem]", honey ? "text-[#ffc93c]" : "text-fg")}
          data-testid={testId}
        >
          {value}
          {unit !== undefined && value !== "—" && (
            <span className={cn("ml-1.5 font-light tracking-normal", compact ? "text-lg" : "text-xl", honey ? "text-[#ffc93c]/70" : "text-fg-muted")}>{unit}</span>
          )}
        </p>
        {sub !== undefined && (
          <p className={cn("truncate font-mono text-[0.6875rem] tracking-[0.04em] text-fg-muted", compact ? "mt-2 leading-4" : "mt-3")}>{sub}</p>
        )}
      </dd>
    </div>
  );
}

export interface TabItem {
  value: string;
  label: string;
  count?: number;
}

/**
 * Frame-B text tabs (the job page's): plain text, the open one bold over a
 * short underline; a real ARIA tablist (arrow keys / Home / End).
 */
export function TextTabs({
  label,
  items,
  value,
  onChange,
  idPrefix,
}: {
  label: string;
  items: readonly TabItem[];
  value: string;
  onChange: (v: string) => void;
  idPrefix: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = items.findIndex((t) => t.value === value);
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % items.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    const tab = items[next];
    if (tab === undefined) return;
    e.preventDefault();
    onChange(tab.value);
    refs.current[next]?.focus();
  };
  return (
    <div role="tablist" aria-label={label} className="flex flex-wrap items-end gap-x-10 gap-y-3" onKeyDown={onKeyDown}>
      {items.map((t, k) => {
        const on = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => {
              refs.current[k] = el;
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${t.value}`}
            aria-selected={on}
            aria-controls={`${idPrefix}-panel-${t.value}`}
            aria-label={t.count === undefined ? undefined : `${t.label} ${String(t.count)}`}
            tabIndex={on ? 0 : -1}
            onClick={() => {
              onChange(t.value);
            }}
            className={`relative pb-2 text-[0.9375rem] transition-colors duration-300 ${on ? "font-semibold text-fg" : "text-fg-muted hover:text-fg"}`}
          >
            {t.label}
            {t.count !== undefined && <span className="ml-1.5 font-mono text-[0.6875rem] font-normal text-fg-subtle tabular-nums">{t.count}</span>}
            <span
              aria-hidden="true"
              className={`absolute bottom-0 left-0 h-0.5 rounded-full bg-fg transition-all duration-300 ${on ? "w-5 opacity-100" : "w-0 opacity-0"}`}
            />
          </button>
        );
      })}
    </div>
  );
}

/** A frosted section with a quiet title row (the node page's FrostTable, for any content). */
export function FrostCard({
  title,
  meta,
  className,
  children,
  label,
  testId,
}: {
  title?: string;
  meta?: ReactNode;
  className?: string;
  children: ReactNode;
  label?: string;
  testId?: string;
}) {
  return (
    <section aria-label={label ?? title} className={cn("ov-card ov-frost min-w-0", className)} data-testid={testId}>
      {title !== undefined && (
        <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2 px-10 pt-8 pb-1">
          <h2 className="ov-card-title">{title}</h2>
          {meta}
        </header>
      )}
      {children}
    </section>
  );
}

/** A quiet mono note line (counts, hints) under or beside a table. */
export function Note({ children, className, testId, caps = true }: { children: ReactNode; className?: string; testId?: string; caps?: boolean }) {
  return (
    <p className={cn("font-mono text-[0.6875rem] text-fg-muted", caps ? "tracking-[0.12em] uppercase" : "tracking-[0.04em]", className)} data-testid={testId}>
      {children}
    </p>
  );
}
