import { useEffect, type RefObject } from "react";

/**
 * While `open`, a pointer press or focus outside `root` calls `close`.
 * (Escape is left to the caller: a nested panel handles it on its own root
 * and stops it there, so the popover around it stays open.)
 */
export function useOutsideClose(open: boolean, root: RefObject<HTMLElement | null>, close: () => void): void {
  useEffect(() => {
    if (!open) return;
    const outside = (event: Event) => {
      if (event.target instanceof Node && root.current?.contains(event.target) !== true) close();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("focusin", outside);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
    };
  }, [open, root, close]);
}

/** Whether a panel of about `panelHeight` px under `el` should open upwards (not enough room below, more above). */
export function opensUp(el: HTMLElement | null, panelHeight: number): boolean {
  if (el === null) return false;
  const rect = el.getBoundingClientRect();
  const below = window.innerHeight - rect.bottom;
  return below < panelHeight && rect.top > below;
}
