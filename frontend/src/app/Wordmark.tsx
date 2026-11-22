import { cn } from "../lib/cn";

/** Wordmark sizes: the top bars (dashboard, agent console) and the login card. */
const SIZE = {
  bar: "text-[34px]",
  login: "text-[40px]",
} as const;

export type WordmarkSize = keyof typeof SIZE;

/**
 * The "hive." wordmark: the word with the honey hex as its dot. Decorative to
 * assistive tech (the visible text reads "hive"); callers give the accessible name.
 */
export function Wordmark({ size = "bar" }: { size?: WordmarkSize }) {
  return (
    <span
      aria-hidden="true"
      data-testid="hive-wordmark"
      className={cn(
        "inline-flex items-baseline font-display leading-none font-bold tracking-[-0.045em] whitespace-nowrap",
        SIZE[size],
      )}
    >
      hive
      <span className="ml-[0.06em] inline-block h-[0.34em] w-[0.3em] bg-accent [clip-path:polygon(50%_0,100%_25%,100%_75%,50%_100%,0_75%,0_25%)]" />
    </span>
  );
}
