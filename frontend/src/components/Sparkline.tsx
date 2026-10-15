import { Line, LineChart, YAxis } from "recharts";

import { cn } from "../lib/cn";

export interface SparkPoint {
  /** Sample time, ms since the epoch. */
  t: number;
  v: number;
}

export interface SparklineProps {
  points: readonly SparkPoint[];
  /** What is plotted, e.g. "CPU %" - part of the accessible summary. */
  label: string;
  /** Fixed y range, e.g. [0, 100] for a percentage. Default: the data's. */
  domain?: [number, number];
  width?: number;
  height?: number;
  /** Formats the last value in the summary (default: 1 decimal). */
  format?: (value: number) => string;
  className?: string;
}

const defaultFormat = (v: number): string => v.toFixed(1);

/**
 * A tiny trend line (Recharts) with no axes or tooltip. Fixed pixel size:
 * Recharts' ResponsiveContainer needs ResizeObserver and inline sizing, and
 * a sparkline has a known slot anyway. Screen readers get a text summary.
 */
export function Sparkline({
  points,
  label,
  domain,
  width = 120,
  height = 28,
  format = defaultFormat,
  className,
}: SparklineProps) {
  const last = points.at(-1);
  const values = points.map((p) => p.v);
  const summary =
    last === undefined
      ? `${label}: no samples`
      : `${label}: now ${format(last.v)}, min ${format(Math.min(...values))}, max ${format(Math.max(...values))} over ${points.length} samples`;

  return (
    <div role="img" aria-label={summary} className={cn("inline-block text-accent", className)}>
      {points.length >= 2 ? (
        <LineChart width={width} height={height} data={points} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
          <YAxis hide domain={domain ?? ["dataMin", "dataMax"]} />
          <Line
            type="monotone"
            dataKey="v"
            stroke="currentColor"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      ) : (
        <span className="text-2xs text-fg-subtle">—</span>
      )}
    </div>
  );
}
