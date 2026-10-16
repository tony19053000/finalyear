import { cn } from "../lib/cn";

export interface MeterProps {
  /** 0-100; null draws an empty track (unknown, never 0). */
  percent: number | null;
  className?: string;
}

/**
 * A thin horizontal usage bar. SVG with a width *attribute*, not an inline
 * style: the controller's CSP (`style-src 'self'`) forbids style attributes.
 * Decorative - the number is always printed beside it.
 */
export function Meter({ percent, className }: MeterProps) {
  const clamped = percent == null || !Number.isFinite(percent) ? null : Math.min(100, Math.max(0, percent));
  const tone =
    clamped == null ? "fill-status-unknown" : clamped >= 90 ? "fill-status-failed" : clamped >= 75 ? "fill-status-degraded" : "fill-accent";
  return (
    <svg aria-hidden="true" className={cn("h-1.5 w-full overflow-hidden rounded-full", className)} preserveAspectRatio="none" viewBox="0 0 100 6">
      <rect x="0" y="0" width="100" height="6" rx="3" className="fill-surface-sunken" />
      {clamped != null && clamped > 0 && <rect x="0" y="0" width={clamped} height="6" rx="3" className={tone} />}
    </svg>
  );
}
