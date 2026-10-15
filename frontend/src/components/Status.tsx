import type { ReactNode } from "react";

import { cn } from "../lib/cn";
import type { StatusTone } from "../lib/status";

const DOT: Record<StatusTone, string> = {
  ok: "bg-status-ok",
  busy: "bg-status-busy",
  degraded: "bg-status-degraded",
  failed: "bg-status-failed",
  unknown: "bg-status-unknown",
};

const BADGE: Record<StatusTone, string> = {
  ok: "text-status-ok bg-status-ok-soft",
  busy: "text-status-busy bg-status-busy-soft",
  degraded: "text-status-degraded bg-status-degraded-soft",
  failed: "text-status-failed bg-status-failed-soft",
  unknown: "text-status-unknown bg-status-unknown-soft",
};

export interface StatusDotProps {
  tone: StatusTone;
  /** Printed beside the dot - status is never colour alone. */
  label: ReactNode;
  className?: string;
}

/** A coloured dot and its state text, e.g. "● online". */
export function StatusDot({ tone, label, className }: StatusDotProps) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)} data-tone={tone}>
      <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", DOT[tone])} />
      <span>{label}</span>
    </span>
  );
}

export interface BadgeProps {
  tone?: StatusTone | "neutral" | "accent";
  children: ReactNode;
  className?: string;
}

/** A small pill label: a state, a kind, a count. */
export function Badge({ tone = "neutral", children, className }: BadgeProps) {
  const colours =
    tone === "neutral"
      ? "text-fg-muted bg-surface-sunken border border-border"
      : tone === "accent"
        ? "text-accent bg-accent-soft"
        : BADGE[tone];
  return (
    <span
      data-tone={tone}
      className={cn(
        "inline-flex h-5 items-center rounded-sm px-1.5 text-2xs font-medium whitespace-nowrap",
        colours,
        className,
      )}
    >
      {children}
    </span>
  );
}
