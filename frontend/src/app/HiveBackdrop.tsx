/**
 * The honeycomb band behind the top of every page (dark theme only). Same
 * still comb as before (56 x 100 tile, #332D22 lines, soft radial fade), plus
 * one touch: the comb's lines brighten softly in honey around the mouse
 * while it moves over the top of the page, and settle back when it leaves.
 *
 * Drawn on a canvas; it only redraws when the mouse moves (no loop). Reduced
 * motion: the glow still follows the mouse but without the ease.
 */
import { useEffect, useRef } from "react";

const TILE_W = 56;
const TILE_H = 100;
const HEX: [number, number][] = [
  [0, -33],
  [28, -17],
  [28, 17],
  [0, 33],
  [-28, 17],
  [-28, -17],
];
const GLOW_R = 190;

export function HiveBackdrop() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (canvas === null) return undefined;
    let ctx: CanvasRenderingContext2D | null;
    try {
      ctx = canvas.getContext("2d");
    } catch {
      ctx = null;
    }
    if (ctx === null) return undefined;
    const c = ctx;
    let w = 0;
    let h = 0;
    // glow: where it is, and how strong (eased toward `target`)
    let gx = -9999;
    let gy = -9999;
    let strength = 0;
    let target = 0;
    let raf = 0;

    const size = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      w = canvas.clientWidth;
      h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const comb = () => {
      c.beginPath();
      for (let y = -TILE_H; y < h + TILE_H; y += TILE_H) {
        for (let x = -TILE_W; x < w + TILE_W; x += TILE_W) {
          const cx = x + 28;
          const cy = y + 33;
          HEX.forEach(([dx, dy], i) => {
            if (i === 0) c.moveTo(cx + dx, cy + dy);
            else c.lineTo(cx + dx, cy + dy);
          });
          c.closePath();
          // the stem down to the next row (as in the old tile)
          c.moveTo(cx, cy + 33);
          c.lineTo(cx, cy + 67);
        }
      }
    };

    const draw = () => {
      c.clearRect(0, 0, w, h);
      c.lineWidth = 1;
      comb();
      c.strokeStyle = "rgba(51,45,34,0.55)";
      c.stroke();
      if (strength > 0.01) {
        const g = c.createRadialGradient(gx, gy, 0, gx, gy, GLOW_R);
        g.addColorStop(0, `rgba(255,201,60,${(0.55 * strength).toFixed(3)})`);
        g.addColorStop(0.5, `rgba(245,166,35,${(0.18 * strength).toFixed(3)})`);
        g.addColorStop(1, "rgba(245,166,35,0)");
        c.strokeStyle = g;
        c.stroke();
      }
    };

    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const tick = () => {
      strength += (target - strength) * (reduced ? 1 : 0.18);
      draw();
      raf = Math.abs(target - strength) > 0.01 ? requestAnimationFrame(tick) : 0;
    };
    const kick = () => {
      if (raf === 0) raf = requestAnimationFrame(tick);
    };

    const onMove = (e: PointerEvent) => {
      const r = canvas.getBoundingClientRect();
      gx = e.clientX - r.left;
      gy = e.clientY - r.top;
      target = gy >= -40 && gy <= h + 40 ? 1 : 0;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    };
    const onLeave = () => {
      target = 0;
      kick();
    };

    size();
    draw();
    const ro =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            size();
            draw();
          });
    ro?.observe(canvas);
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
    return () => {
      cancelAnimationFrame(raf);
      ro?.disconnect();
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerleave", onLeave);
    };
  }, []);

  return <canvas ref={ref} aria-hidden="true" className="mf-backdrop" />;
}
