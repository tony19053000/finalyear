import { useId, type HTMLAttributes, type ReactNode } from "react";

import { cn } from "../lib/cn";

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  /** Heading text; also names the card region for screen readers. */
  title?: ReactNode;
  /** Right-aligned header content (buttons, a badge, a timestamp). */
  actions?: ReactNode;
  /** No inner padding (tables, logs fill the card edge to edge). */
  flush?: boolean;
}

/** A bordered panel with an optional header row. */
export function Card({ title, actions, flush = false, className, children, ...rest }: CardProps) {
  const titleId = useId();
  return (
    <section
      aria-labelledby={title !== undefined ? titleId : undefined}
      className={cn("rounded-lg border border-border bg-surface", className)}
      {...rest}
    >
      {(title !== undefined || actions !== undefined) && (
        <header className="flex min-h-10 items-center justify-between gap-3 border-b border-border px-4 py-2">
          {title !== undefined && <h2 id={titleId} className="text-sm font-semibold text-fg">{title}</h2>}
          {actions !== undefined && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={flush ? undefined : "p-4"}>{children}</div>
    </section>
  );
}
