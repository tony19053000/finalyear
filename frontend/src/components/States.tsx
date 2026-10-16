import type { ReactNode } from "react";

import { describeError } from "../api/errors";
import { cn } from "../lib/cn";
import { Button } from "./Button";

export interface EmptyStateProps {
  title: string;
  /** What this place will show and how to get there - no fake data. */
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}

/** Shown where there is honestly nothing (yet) to show. */
export function EmptyState({ title, children, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-12 text-center",
        className,
      )}
    >
      <h2 className="text-base font-medium text-fg">{title}</h2>
      {children !== undefined && <div className="max-w-md text-sm text-fg-muted">{children}</div>}
      {action !== undefined && <div className="mt-2">{action}</div>}
    </div>
  );
}

export interface ErrorStateProps {
  /** What failed, e.g. "Could not load nodes". */
  title: string;
  error: unknown;
  onRetry?: () => void;
  className?: string;
}

/** A failed load: what / why (the API's error code + message) / retry. */
export function ErrorState({ title, error, onRetry, className }: ErrorStateProps) {
  const { message, code } = describeError(error);
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-start gap-2 rounded-lg border border-status-failed bg-status-failed-soft px-4 py-3",
        className,
      )}
    >
      <h2 className="text-sm font-semibold text-status-failed">{title}</h2>
      <p className="text-sm text-fg">{message}</p>
      {code !== undefined && <p className="font-mono text-2xs text-fg-muted">code: {code}</p>}
      {onRetry !== undefined && (
        <Button size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
