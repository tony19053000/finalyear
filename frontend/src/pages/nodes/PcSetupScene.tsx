/**
 * The Hardware tab's centrepiece: the machine as a softly lit 3D tower
 * (one three-quarter still pre-rendered by frontend/blender/pc_setup.py, with
 * one light layer per part). It tilts gently towards the cursor and settles
 * when the cursor leaves. Its lights are live data: the CPU cooler ring glows
 * honey with the CPU load, the four RAM bars fill teal with the memory in use
 * (the last one partly), the GPU fan rings glow coral with GPU utilisation
 * (dark without a GPU). Each light is a layer composited with
 * mix-blend-mode: screen; its opacity and the tilt are CSS custom properties
 * set through the CSSOM (no inline style attribute - CSP).
 * It fills its parent, so the page decides its size; the specs beside it
 * carry the same facts as text.
 */
import { type PointerEvent, useEffect, useRef, useState } from "react";

import type { NodeDetail } from "../../api/fabric";
import { formatPercent } from "../../lib/format";
import { LIGHTS, lightLevels, towerUrl } from "./pcSetup";

function setVars(el: HTMLElement | null, vars: Record<string, string>) {
  if (el === null) return;
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v);
}

const REST = { "--pc-ry": "0deg", "--pc-rx": "0deg", "--pc-tx": "0px" };

export function PcSetupScene({ node }: { node: NodeDetail }) {
  const stageRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const levels = lightLevels(node);
  const key = LIGHTS.map((l) => levels[l].toFixed(3)).join(" ");

  // live lights -> custom properties (the CSS fades them)
  useEffect(() => {
    const lv = key.split(" ");
    setVars(stageRef.current, Object.fromEntries(LIGHTS.map((l, i) => [`--pc-${l}`, lv[i] ?? "0"])));
  }, [key, failed]);

  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    const u = Math.max(-1, Math.min(1, ((e.clientX - r.left) / r.width) * 2 - 1));
    const v = Math.max(-1, Math.min(1, ((e.clientY - r.top) / r.height) * 2 - 1));
    setVars(e.currentTarget, {
      "--pc-ry": `${(u * 6).toFixed(2)}deg`,
      "--pc-rx": `${(-v * 3.5).toFixed(2)}deg`,
      "--pc-tx": `${(u * 8).toFixed(1)}px`,
    });
  };
  const onLeave = (e: PointerEvent<HTMLDivElement>) => {
    setVars(e.currentTarget, REST);
  };

  if (failed) {
    return (
      <div className="pc-stage flex items-center justify-center" data-testid="pc-setup-fallback">
        <p className="max-w-xs text-center text-sm text-fg-muted">The picture of this machine could not load. The specs beside it are complete.</p>
      </div>
    );
  }

  const live = node.connected && node.cpu_percent != null;
  const label = live
    ? `The machine, lit by its live load: CPU ${formatPercent(levels.cpu * 100)}, memory ${formatPercent((levels.ram0 + levels.ram1 + levels.ram2 + levels.ram3) * 25)}, GPU ${formatPercent(levels.gpu * 100)}`
    : "The machine (offline: its lights are dark)";

  return (
    <div ref={stageRef} className="pc-stage" role="img" aria-label={label} data-testid="pc-setup-scene" onPointerMove={onMove} onPointerLeave={onLeave}>
      <div className="pc-tilt">
        <img
          src={towerUrl()}
          alt=""
          draggable={false}
          decoding="async"
          className="pc-base"
          onError={() => {
            setFailed(true);
          }}
        />
        {LIGHTS.map((l) => (
          <img key={l} src={towerUrl(l)} alt="" draggable={false} decoding="async" className={`pc-light pc-light-${l}`} />
        ))}
      </div>
    </div>
  );
}
