/**
 * The sign-in page's backdrop: the Overview's 3D honeycomb (the landing
 * page's comb, drawn on a 2D canvas) across the whole screen, a few honey
 * cells standing tall like laptops at work. Decorative only.
 */
import { useEffect, useRef } from "react";

import { HoneycombScene, type CombNode } from "./overview/honeycombScene";

const CELLS: CombNode[] = [
  { id: "a", name: "", state: "online", load: 0.85 },
  { id: "b", name: "", state: "online", load: 0.55 },
  { id: "c", name: "", state: "suspect", load: 0.35 },
  { id: "d", name: "", state: "online", load: 1 },
  { id: "e", name: "", state: "online", load: 0.25 },
  { id: "f", name: "", state: "offline", load: null },
  { id: "g", name: "", state: "online", load: 0.7 },
];

export function LoginComb() {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const el = canvas.current;
    const box = wrap.current;
    if (el === null || box === null || navigator.userAgent.includes("jsdom")) return undefined;
    let ctx: CanvasRenderingContext2D | null;
    try {
      ctx = el.getContext("2d");
    } catch {
      ctx = null;
    }
    if (ctx === null) return undefined;
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // a still watermark unless motion is welcome; even then only the slow idle drift
    const s = new HoneycombScene(el, ctx, { reduced, zoom: 2.1 });
    s.setData(CELLS);
    const size = () => {
      s.resize(box.clientWidth, box.clientHeight);
    };
    size();
    const update = () => {
      if (document.hidden) s.stop();
      else s.start();
    };
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(size);
    ro?.observe(box);
    document.addEventListener("visibilitychange", update);
    update();
    return () => {
      s.stop();
      ro?.disconnect();
      document.removeEventListener("visibilitychange", update);
    };
  }, []);

  return (
    <div ref={wrap} aria-hidden="true" className="mf-login-comb pointer-events-none absolute inset-0">
      <canvas ref={canvas} className="absolute inset-0 h-full w-full" />
    </div>
  );
}
