import type { ReactNode } from "react";

/** The page's one <h1> and a line of context. */
export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="mt-3 mb-7 flex items-end justify-between gap-4">
      <div>
        <h1 className="font-display text-[2.75rem] leading-[1.05] font-bold tracking-[-0.035em] text-fg">{title}</h1>
        {description !== undefined && <p className="mt-2 text-sm text-fg-muted">{description}</p>}
      </div>
      {actions !== undefined && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
