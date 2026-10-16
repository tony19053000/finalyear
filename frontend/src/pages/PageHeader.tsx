import type { ReactNode } from "react";

/** The page's one <h1> and a line of context. */
export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold text-fg">{title}</h1>
        {description !== undefined && <p className="mt-0.5 text-sm text-fg-muted">{description}</p>}
      </div>
      {actions !== undefined && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
