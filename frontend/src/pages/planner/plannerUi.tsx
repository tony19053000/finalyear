/**
 * Presentation pieces of the Planner page in the restyled pages' language
 * (Benchmarks / Jobs): the open headline figure, the section with a quiet
 * title row - frosted (a card) only where a main table scrolls inside it,
 * open on the page everywhere else -, the table with even gaps and the
 * sticky header that takes its fill only once rows scroll under it, and the
 * state word beside a flat light (never a badge). Explanations live in
 * tooltips. Presentation only - no data rules here.
 */
import { useRef, type ReactNode } from "react";

import type { Column } from "../../components/Table";
import { cn } from "../../lib/cn";
import { Light } from "../benchmarks/benchUi";
import { TH, useScrolled } from "../benchmarks/benchShared";

/** A big light figure: mono label over the number, a quieter unit and a mono sub line; `hint` is its tooltip. */
export function PlFigure({
  label,
  value,
  unit,
  sub,
  hint,
  honey = false,
  size = "big",
  className,
  testId,
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: ReactNode;
  hint?: string;
  honey?: boolean;
  /** `big` for the page's headline row, `mid` for the figures inside a section. */
  size?: "big" | "mid";
  className?: string;
  testId?: string;
}) {
  return (
    <div className={cn("min-w-0", hint !== undefined && "cursor-help", className)} title={hint}>
      <dt className="nd-label">{label}</dt>
      <dd className={size === "big" ? "mt-2" : "mt-2.5"}>
        <p
          className={cn("ov-figure flex items-baseline font-light", size === "big" ? "text-[3rem]" : "text-[2.25rem]", honey ? "text-[#ffc93c]" : "text-fg")}
          data-testid={testId}
        >
          <span className="truncate">{value}</span>
          {unit !== undefined && value !== "—" && (
            <span className={cn("ml-1.5 shrink-0 font-light tracking-normal", size === "big" ? "text-lg" : "text-[0.9375rem]", honey ? "text-[#ffc93c]/70" : "text-fg-muted")}>
              {unit}
            </span>
          )}
        </p>
        {sub !== undefined && <p className="mt-2 line-clamp-2 font-mono text-[0.6875rem] leading-4 tracking-[0.04em] text-fg-muted">{sub}</p>}
      </dd>
    </div>
  );
}

/**
 * A section with a quiet title row: a frosted card, or (`plain`) open on the
 * page with the very same paddings, so an open section's title and edges line
 * up with a neighbouring card's. `hint` is the title's tooltip, `meta` sits
 * at the row's right.
 */
export function PlCard({
  title,
  hint,
  meta,
  className,
  children,
  label,
  testId,
  plain = false,
  alignEnd = false,
}: {
  title?: ReactNode;
  hint?: string;
  meta?: ReactNode;
  className?: string;
  children: ReactNode;
  label?: string;
  testId?: string;
  plain?: boolean;
  /** Align the title row's items on their bottom edge (meta holds form controls). */
  alignEnd?: boolean;
}) {
  return (
    <section
      aria-label={label ?? (typeof title === "string" ? title : undefined)}
      className={cn("min-w-0", !plain && "ov-card ov-frost pb-4", className)}
      data-testid={testId}
    >
      {title !== undefined && (
        <header className={cn("pl-head flex flex-wrap justify-between gap-x-8 gap-y-3", alignEnd ? "items-end" : "items-baseline")}>
          <h2 className={cn("ov-card-title min-w-0", hint !== undefined && "cursor-help")} title={hint}>
            {title}
          </h2>
          {meta}
        </header>
      )}
      {children}
    </section>
  );
}

/** A state in words beside its flat light (never colour alone, never a badge). */
export function StatusWord({ light, children, breathe = false, className }: { light: string; children: ReactNode; breathe?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex min-w-0 items-center gap-2.5", className)}>
      <Light className={light} small breathe={breathe} />
      <span className="min-w-0 truncate">{children}</span>
    </span>
  );
}

/** A quiet mono line (counts, the in-use facts). */
export function PlNote({ children, className, testId, title }: { children: ReactNode; className?: string; testId?: string; title?: string }) {
  return (
    <p className={cn("font-mono text-[0.6875rem] tracking-[0.04em] text-fg-muted", title !== undefined && "cursor-help", className)} data-testid={testId} title={title}>
      {children}
    </p>
  );
}

/** A truncated cell whose full text is its tooltip. */
export function Clip({ text, className }: { text: string; className?: string }) {
  return (
    <span className={cn("block cursor-help truncate", className)} title={text}>
      {text}
    </span>
  );
}

/**
 * A column of a planner table: `full` is the header's full name (its
 * accessible name and tooltip) when the visible header is a short form;
 * `hint` an explanation tooltip.
 */
export type PlColumn<T> = Column<T> & { full?: string; hint?: string };

/**
 * A table with even gaps (per table in index.css via `className`: each
 * column its content's width plus one shared slack, the same space before
 * the first column and after the last), the mono head on one line, a sticky
 * header clear at the top that takes the card's tone once rows scroll under
 * it. `selected` marks a row with the honey edge; a row click calls
 * `onRowClick` (links and buttons keep theirs).
 */
export function PlTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  empty,
  className,
  scrollClassName,
  onRowClick,
  selected,
  testId,
}: {
  caption: string;
  columns: readonly PlColumn<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  empty?: ReactNode;
  className?: string;
  scrollClassName?: string;
  onRowClick?: (row: T) => void;
  selected?: (row: T) => boolean;
  testId?: string;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const scrolled = useScrolled(scroller);
  return (
    <div className={cn("ov-table-fit pl-table flex min-h-0 flex-col", className)}>
      <div ref={scroller} className={cn("jb-scroll pl-scroll min-h-0 overflow-auto", scrollClassName)} data-scrolled={scrolled ? "true" : "false"}>
        <table className="w-full border-collapse text-[0.875rem]" data-testid={testId}>
          <caption className="sr-only">{caption}</caption>
          <thead>
            <tr>
              {columns.map((c) => {
                const tip = c.hint ?? c.full;
                return (
                  <th
                    key={c.id}
                    scope="col"
                    aria-label={c.full}
                    title={tip}
                    className={cn(TH, "text-[0.6875rem]", c.align === "right" && "text-right", tip !== undefined && "cursor-help")}
                  >
                    {c.header}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={rowKey(row)}
                data-selected={selected?.(row) === true ? "true" : undefined}
                className={onRowClick !== undefined ? "cursor-pointer" : undefined}
                onClick={
                  onRowClick === undefined
                    ? undefined
                    : (event) => {
                        if ((event.target as HTMLElement).closest("a, button, summary, dialog") !== null) return;
                        onRowClick(row);
                      }
                }
              >
                {columns.map((c) => (
                  <td key={c.id} className={cn("align-middle", c.mono === true && "font-mono text-[0.8125rem] tabular-nums", c.align === "right" && "text-right", c.className)}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
            {rows.length === 0 && empty !== undefined && (
              <tr>
                <td colSpan={columns.length} className="pl-empty text-center text-fg-muted">
                  {empty}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
