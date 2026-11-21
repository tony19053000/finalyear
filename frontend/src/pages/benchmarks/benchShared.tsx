/**
 * Non-component helpers of the Benchmarks pages' presentation (kept apart
 * from the components so fast refresh works): node names, the sticky-header
 * scroll flag, the table head class, the dash, the chart tones.
 */
import { useEffect, useMemo, useState, type RefObject } from "react";

import { useNodes } from "../../api/hooks";
import { nodeNames } from "../jobs/jobView";

/** node_id -> the name the dashboard shows (falls back to the id). */
export function useNodeNames(): (id: string) => string {
  const nodes = useNodes();
  const names = useMemo(() => nodeNames(nodes.data?.nodes ?? []), [nodes.data]);
  return (id: string) => names.get(id) ?? id;
}

/** True once the element has scrolled (the sticky header then takes its fill). */
export function useScrolled(ref: RefObject<HTMLElement | null>): boolean {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    // listen on the document (scroll does not bubble, so capture): the
    // scroll box often mounts after this effect ran (it waits for data)
    const update = (event: Event) => {
      const el = ref.current;
      if (el !== null && event.target === el) setScrolled(el.scrollTop > 2);
    };
    document.addEventListener("scroll", update, { capture: true, passive: true });
    return () => {
      document.removeEventListener("scroll", update, { capture: true });
    };
  }, [ref]);
  return scrolled;
}

/** Column heads in the Jobs table's mono label style. */
export const TH = "text-left font-mono font-medium tracking-[0.14em] whitespace-nowrap text-fg-subtle uppercase";

export const dash = <span className="text-fg-subtle">—</span>;

export type Tone = "hive" | "base" | "other";

/** Text-colour classes per tone (the marks use currentColor). Other series cycle through quiet tints. */
const OTHER = ["text-fg", "text-series-ram", "text-series-gpu", "text-fg-muted"] as const;

export function toneClass(tone: Tone, index = 0): string {
  if (tone === "hive") return "text-[#ffc93c]";
  if (tone === "base") return "text-fg-subtle";
  return OTHER[index % OTHER.length] ?? "text-fg";
}
