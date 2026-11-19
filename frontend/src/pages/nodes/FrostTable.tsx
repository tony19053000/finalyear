import type { ReactNode } from "react";

import { cn } from "../../lib/cn";

/**
 * The node detail's table card: the Nodes list's frosted glass card, even
 * columns, tall rows and honey hover edge (`.ov-table-fit`), with a quiet
 * title row aligned to the first column. `className` picks the column
 * widths (`nd-net`, `nd-cache`, `nd-policy`, `nd-scores`).
 */
export function FrostTable({
  title,
  meta,
  className,
  children,
  testId,
}: {
  title?: string;
  meta?: ReactNode;
  className?: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section
      aria-label={title}
      className={cn("ov-card ov-frost ov-table-fit nd-table min-w-0 overflow-hidden", className)}
      data-testid={testId}
    >
      {title !== undefined && (
        <header className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-9 pt-7 pb-3">
          <h2 className="ov-card-title">{title}</h2>
          {meta}
        </header>
      )}
      <div className={title === undefined ? "pt-2 pb-3" : "pb-3"}>{children}</div>
    </section>
  );
}
