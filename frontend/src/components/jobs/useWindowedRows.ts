/**
 * Row windowing for long tables (D174: rows virtualised over 200).
 *
 * A small stand-in for `@tanstack/react-virtual` (not installed - question
 * Q-G4-1 in the G4 progress file). Same idea and a compatible shape: fixed row
 * height, a scroll element, and the window `[start, end)` plus the space above
 * and below it (`padTop`/`padBottom`, the `getTotalSize()` remainder). Swap:
 * replace `useWindowedRows` with `useVirtualizer({count, getScrollElement,
 * estimateSize})` and read `getVirtualItems()`; the table code stays.
 *
 * CSP (D181, `style-src 'self'`): spacer heights are set through the CSSOM
 * (`element.style.height`), which the CSP allows; there is no `style=`
 * attribute anywhere (see `useCssHeight`).
 */
import { useEffect, useLayoutEffect, useState, type RefObject } from "react";

export interface RowWindow {
  /** First rendered row (inclusive). */
  start: number;
  /** One past the last rendered row. */
  end: number;
  /** Pixels of rows above the window (a spacer). */
  padTop: number;
  /** Pixels of rows below the window (a spacer). */
  padBottom: number;
  /** False when every row is rendered (count <= threshold). */
  windowed: boolean;
  /** The last row inside the viewport (for "load the next page" near the end). */
  lastVisible: number;
}

export interface WindowOptions {
  rowHeight: number;
  /** Rows rendered beyond the visible ones, on each side. */
  overscan?: number;
  /** Up to this many rows the table renders them all. */
  threshold?: number;
  /** Pixels between the scroll element's top and the first row (a sticky header). */
  offset?: number;
}

export const WINDOW_THRESHOLD = 200;

/** The pure window computation (unit-tested). */
export function computeWindow(
  count: number,
  scrollTop: number,
  viewportHeight: number,
  { rowHeight, overscan = 10, threshold = WINDOW_THRESHOLD, offset = 0 }: WindowOptions,
): RowWindow {
  const height = rowHeight > 0 ? rowHeight : 1;
  const top = Math.max(0, scrollTop - offset);
  const visible = Math.max(1, Math.ceil(Math.max(viewportHeight, height) / height));
  const first = Math.max(0, Math.min(count - 1, Math.floor(top / height)));
  const lastVisible = Math.max(-1, Math.min(count - 1, first + visible - 1));
  if (count <= threshold || rowHeight <= 0) {
    return { start: 0, end: count, padTop: 0, padBottom: 0, windowed: false, lastVisible };
  }
  const start = Math.max(0, first - overscan);
  const end = Math.min(count, first + visible + overscan);
  return { start, end, padTop: start * height, padBottom: (count - end) * height, windowed: true, lastVisible };
}

/** The window of rows to render for `count` rows inside `scrollRef`. */
export function useWindowedRows(scrollRef: RefObject<HTMLElement | null>, count: number, options: WindowOptions): RowWindow {
  const [view, setView] = useState({ scrollTop: 0, height: 0 });

  useEffect(() => {
    const element = scrollRef.current;
    if (element === null) return undefined;
    const update = () => {
      setView((old) =>
        old.scrollTop === element.scrollTop && old.height === element.clientHeight
          ? old
          : { scrollTop: element.scrollTop, height: element.clientHeight },
      );
    };
    update();
    element.addEventListener("scroll", update, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(element);
    return () => {
      element.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, [scrollRef]);

  // Before the first measurement assume a tall viewport (no blank first
  // paint) but claim nothing is visible yet (no page load on a guess).
  if (view.height === 0) return { ...computeWindow(count, 0, 1200, options), lastVisible: -1 };
  return computeWindow(count, view.scrollTop, view.height, options);
}

/** Size an element through the CSSOM (CSP-safe; no `style=` attribute). */
export function useCssHeight(ref: RefObject<HTMLElement | null>, pixels: number): void {
  useLayoutEffect(() => {
    const element = ref.current;
    if (element !== null) element.style.height = `${pixels}px`;
  }, [ref, pixels]);
}
