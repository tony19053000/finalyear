import type { ReactNode } from "react";

import { cn } from "../lib/cn";

export interface Column<T> {
  /** Stable id of the column (React key, test hook). */
  id: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  align?: "left" | "right";
  /** Monospace + tabular numbers (ids, digests, sizes). */
  mono?: boolean;
  className?: string;
}

export interface TableProps<T> {
  columns: Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  /** Read by screen readers; visually hidden. */
  caption: string;
  /** Shown in place of the body when `rows` is empty. */
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  className?: string;
}

/**
 * A dense data table (D175). Rows over 200 are virtualised by the jobs list
 * in G4; this component stays a plain semantic <table>.
 */
export function Table<T>({ columns, rows, rowKey, caption, empty, onRowClick, className }: TableProps<T>) {
  return (
    <div className={cn("overflow-x-auto", className)}>
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border">
            {columns.map((col) => (
              <th
                key={col.id}
                scope="col"
                className={cn(
                  "h-8 px-3 text-2xs font-medium tracking-wide text-fg-subtle uppercase",
                  col.align === "right" ? "text-right" : "text-left",
                )}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-3 py-6 text-center text-fg-muted">
                {empty ?? "Nothing to show."}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr
                key={rowKey(row)}
                onClick={onRowClick ? () => { onRowClick(row); } : undefined}
                className={cn(
                  "border-b border-border last:border-b-0",
                  onRowClick && "cursor-pointer hover:bg-surface-raised",
                )}
              >
                {columns.map((col) => (
                  <td
                    key={col.id}
                    className={cn(
                      "h-9 px-3 align-middle",
                      col.align === "right" ? "text-right" : "text-left",
                      col.mono && "font-mono text-xs tabular-nums",
                      col.className,
                    )}
                  >
                    {col.cell(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
