/**
 * The Overview's 3D honeycomb, drawn on a 2D canvas.
 *
 * Same geometry as the landing page's three.js comb (website/assets/js/home.js:
 * pointy hex prisms on an axial grid, warm key light from the top left) but
 * projected by hand and flat-shaded, with no bloom or glow: three.js is not a
 * dependency of the dashboard and would not fit its 350 kB chunk budget
 * (scripts/check-dist.mjs), and ~50 prisms need no GPU.
 *
 * One real laptop = one cell; its height follows its load. State look (top
 * face colour only): online = honey; offline = dark; suspect =
 * a dimmer honey with a slow pulse; draining = half honey (still); enrolling =
 * the cell rising in. Fewer dark filler cells surround them, fading toward the
 * edge. Offline = a taller solid black prism. Hover: the laptop lifts and
 * brightens, a ring spreads from its base, the rest dims; only then its name.
 *
 * Performance: one canvas, DPR capped at 1.5, the loop runs only while the
 * canvas is on screen and the tab is visible; reduced motion draws still
 * frames on change only.
 */

export type CombState = "online" | "offline" | "suspect" | "draining" | "enrolling";

export interface CombNode {
  id: string;
  name: string;
  state: CombState;
  /** 0..1 (null = not reporting). */
  load: number | null;
}

interface Cell {
  q: number;
  r: number;
  x: number;
  z: number;
  node: CombNode | null;
  h: number;
  /** Display height (animated toward h). */
  dh: number;
  phase: number;
  shade: number;
  /** 1 near the centre -> faint at the edge (fillers only). */
  fade: number;
  /** Hover amount 0..1 (eased). */
  hk: number;
  /** Part of the framed core (the camera fits these; the rest spreads past the edges). */
  core: boolean;
  /** For enrolling: when it started rising (scene time). */
  born: number;
}

const R = 0.5;
const GAP = 0.08;
const D = Math.sqrt(3) * R + GAP;
const SQ3_2 = Math.sqrt(3) / 2;
const TILT = 0.52;
const VERT = 0.86;

const CORNERS: [number, number][] = Array.from({ length: 6 }, (_, k) => {
  const a = (k * Math.PI) / 3;
  return [R * Math.sin(a), R * Math.cos(a)];
});

function rand(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

function clamp(v: number, a: number, b: number): number {
  return v < a ? a : v > b ? b : v;
}

/** Cell positions ordered from the centre outward over a wide ellipse (the card is wide). */
function layout(count: number): { q: number; r: number; x: number; z: number }[] {
  const out: { q: number; r: number; x: number; z: number; d: number }[] = [];
  const N = 9;
  for (let q = -N * 2; q <= N * 2; q++) {
    for (let r = -N; r <= N; r++) {
      const x = D * (q + r / 2);
      const z = D * SQ3_2 * r;
      out.push({ q, r, x, z, d: Math.hypot(x / 2.8, z) + q * 1e-6 + r * 1e-7 });
    }
  }
  out.sort((a, b) => a.d - b.d);
  return out.slice(0, count);
}

type RGB = [number, number, number];

function mix(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

function css(c: RGB): string {
  return `rgb(${String(Math.round(c[0]))},${String(Math.round(c[1]))},${String(Math.round(c[2]))})`;
}

function scaled(c: RGB, k: number): RGB {
  return [c[0] * k, c[1] * k, c[2] * k];
}

// Dark bronze fillers, honey laptops: one flat colour per face.
const FILLER_TOP: RGB = [46, 39, 29];
const FILLER_SIDE: RGB = [34, 29, 22];
const BRONZE_TOP: RGB = [33, 28, 21];
const BRONZE_SIDE: RGB = [24, 20, 15];
// Offline: a solid black prism.
const BLACK_TOP: RGB = [14, 13, 11];
const BLACK_SIDE: RGB = [10, 9, 8];
const HONEY_TOP: RGB = [247, 182, 52];
const HONEY_SIDE: RGB = [132, 86, 20];

export interface SceneOptions {
  reduced: boolean;
}

export class HoneycombScene {
  private ctx: CanvasRenderingContext2D;
  private cells: Cell[] = [];
  private w = 0;
  private h = 0;
  private scale = 40;
  private cx = 0;
  private cy = 0;
  private t = 0;
  private last = 0;
  private raf = 0;
  private running = false;
  private hovered: string | null = null;
  private hulls = new Map<string, [number, number][]>();
  private yaw = 0.42;
  /** 0..1: how much a hover dims the rest of the comb (eased). */
  private focus = 0;

  constructor(
    private canvas: HTMLCanvasElement,
    ctx: CanvasRenderingContext2D,
    private opts: SceneOptions,
  ) {
    this.ctx = ctx;
  }

  setData(nodes: readonly CombNode[]): void {
    const prev = new Map(this.cells.filter((c) => c.node).map((c) => [c.node?.id ?? "", c]));
    // A core the camera frames, then a wide field of extra cells running past
    // the card's edges, so the laptops sit in a big network.
    const coreCount = nodes.length + Math.max(26, Math.min(64, nodes.length * 3 + 20));
    const spots = layout(coreCount + 260);
    const rnd = rand(11);
    // Laptops take spots from the centre outward, never next to another
    // laptop (a filler between them keeps names and heights readable); the
    // rest are fillers. Falls back to any free spot if the comb is crowded.
    const nodeSpots = new Set<number>();
    const minGap = D * 1.9;
    // Balanced scatter: laptops spread evenly across the comb's width in a
    // gentle zigzag (front/back), each on the free cell nearest its target, so
    // they read as spread through the network - not a straight row, not a clump.
    let spanX = 1;
    let spanZ = 1;
    for (let i = 0; i < coreCount; i++) {
      const sp = spots[i];
      if (sp === undefined) continue;
      spanX = Math.max(spanX, Math.abs(sp.x));
      spanZ = Math.max(spanZ, Math.abs(sp.z));
    }
    const n = nodes.length;
    const jitter = rand(29);
    for (let k = 0; k < n; k++) {
      const tx = n === 1 ? 0 : (-0.78 + (1.56 * k) / (n - 1)) * spanX;
      const lane = k % 3 === 0 ? -0.22 : k % 3 === 1 ? 0.22 : 0;
      const tz = (lane + (jitter() - 0.5) * 0.2) * spanZ;
      let best = -1;
      let bestD = Infinity;
      for (let i = 0; i < spots.length; i++) {
        const sp = spots[i];
        if (sp === undefined || nodeSpots.has(i)) continue;
        const crowded = [...nodeSpots].some((j) => {
          const o = spots[j];
          return o !== undefined && Math.hypot(o.x - sp.x, o.z - sp.z) < minGap;
        });
        const d = Math.hypot(sp.x - tx, sp.z - tz) + (crowded ? 100 : 0);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      if (best >= 0) nodeSpots.add(best);
    }
    const order = [...nodeSpots].sort((a, b) => a - b);
    const far = Math.max(1, ...spots.map((sp) => Math.hypot(sp.x / 2.8, sp.z)));
    const nodeAt = new Map(order.map((spot, k) => [spot, nodes[k] ?? null]));
    this.cells = spots.map((s, i) => {
      const node = nodeAt.get(i) ?? null;
      const old = node ? prev.get(node.id) : undefined;
      const dist = Math.hypot(s.x / 2.8, s.z);
      const h = node ? (node.state === "offline" ? 0.55 : 0.3 + 1.05 * clamp(node.load ?? 0, 0, 1)) : (0.12 + rnd() * 0.3) * clamp(1.1 - dist / 6, 0.25, 1);
      return {
        ...s,
        node,
        h,
        dh: old ? old.dh : node?.state === "enrolling" ? 0 : h,
        phase: node ? hash(node.id) * Math.PI * 2 : rnd() * Math.PI * 2,
        shade: 0.9 + rnd() * 0.18,
        hk: 0,
        core: i < coreCount || node !== null,
        fade: node ? 1 : clamp(1.1 - (dist / far) * 0.7, 0.4, 1),
        born: old ? old.born : this.t,
      };
    });
    this.fit();
    if (!this.running) this.draw();
  }

  resize(width: number, height: number): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    this.w = width;
    this.h = height;
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.fit();
    if (!this.running) this.draw();
  }

  private fit(): void {
    let maxX = 1;
    let maxZ = 1;
    for (const c of this.cells) {
      if (!c.core) continue;
      maxX = Math.max(maxX, Math.abs(c.x) + R);
      maxZ = Math.max(maxZ, Math.abs(c.z) + R);
    }
    const s = Math.min((this.w * 0.53) / (maxX * 1.02), (this.h * 0.56) / (maxZ * TILT + 0.9 * VERT + 0.3));
    this.scale = clamp(s, 14, 110);
    this.cx = this.w / 2;
    this.cy = this.h * 0.56 + this.scale * 0.35;
  }

  setHovered(id: string | null): void {
    if (this.hovered === id) return;
    this.hovered = id;
    if (!this.running) this.draw();
  }

  /** The node cell under a point (css px), front cells first. */
  hitTest(px: number, py: number): string | null {
    for (const [id, hull] of [...this.hulls.entries()].reverse()) {
      if (inside(hull, px, py)) return id;
    }
    return null;
  }

  start(): void {
    if (this.running || this.opts.reduced) {
      this.draw();
      return;
    }
    this.running = true;
    this.last = performance.now();
    const frame = (now: number) => {
      if (!this.running) return;
      const dt = Math.min(0.1, (now - this.last) / 1000);
      this.last = now;
      this.t += dt;
      const k = 1 - Math.exp(-dt * 1.6);
      const kh = 1 - Math.exp(-dt * 4.5);
      for (const c of this.cells) {
        c.dh += (c.h - c.dh) * k;
        const want = c.node !== null && c.node.id === this.hovered ? 1 : 0;
        c.hk += (want - c.hk) * kh;
      }
      this.focus += ((this.hovered === null ? 0 : 1) - this.focus) * kh;
      this.yaw = 0.42 + Math.sin(this.t * 0.05) * 0.07;
      this.draw();
      this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  /** How honey the top is (0 = bronze, 1 = honey). */
  private lit(c: Cell): number {
    const node = c.node;
    if (node === null) return 0;
    const t = this.opts.reduced ? 0 : this.t;
    switch (node.state) {
      case "online":
        return 1;
      case "suspect":
        // dimmer honey, slow calm pulse
        return this.opts.reduced ? 0.45 : 0.42 + 0.14 * Math.sin(t * 0.9 + c.phase);
      case "draining":
        return 0.45;
      case "enrolling":
        return 0.6;
      default:
        return 0;
    }
  }

  private heightOf(c: Cell): number {
    if (c.node?.state === "enrolling" && !this.opts.reduced) {
      const u = clamp((this.t - c.born) / 3.2, 0, 1);
      const loop = ((this.t - c.born) % 6) / 6;
      const rise = u < 1 ? u : clamp(loop * 2.2, 0, 1);
      return c.dh * (1 - Math.pow(1 - rise, 3));
    }
    // alive: every cell drifts up and down a touch, slowly and out of step
    // (the plain cells a bit more than the laptops)
    const idle = this.opts.reduced ? 0 : Math.sin(this.t * 0.7 + c.phase * 3.1) * (c.node === null ? 0.1 : 0.05);
    const hk = this.opts.reduced ? (c.node !== null && c.node.id === this.hovered ? 1 : 0) : c.hk;
    // hovered: lifts with a small overshoot-free ease and a gentle bob
    return Math.max(0.02, c.dh + idle) + hk * (0.32 + (this.opts.reduced ? 0 : 0.06 * Math.sin(this.t * 1.1 + c.phase)));
  }

  private project(x: number, y: number, z: number): [number, number] {
    const cos = Math.cos(this.yaw);
    const sin = Math.sin(this.yaw);
    const xr = x * cos - z * sin;
    const zr = x * sin + z * cos;
    return [this.cx + xr * this.scale, this.cy + zr * this.scale * TILT - y * this.scale * VERT];
  }

  draw(): void {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.lineJoin = "round";
    if (this.w === 0) return;
    const cos = Math.cos(this.yaw);
    const sin = Math.sin(this.yaw);
    const order = [...this.cells].sort((a, b) => a.x * sin + a.z * cos - (b.x * sin + b.z * cos));
    this.hulls.clear();
    const labels: { x: number; y: number; text: string; dim: boolean; strong: boolean }[] = [];
    // Names show only for the hovered laptop.
    for (const c of order) {
      const h = this.heightOf(c);
      const lit = this.lit(c);
      const hovered = c.node !== null && c.node.id === this.hovered;
      const bottom = CORNERS.map(([dx, dz]) => this.project(c.x + dx, 0, c.z + dz));
      const top = CORNERS.map(([dx, dz]) => this.project(c.x + dx, h, c.z + dz));
      const offline = c.node?.state === "offline";
      const sideBase = c.node === null ? FILLER_SIDE : offline ? BLACK_SIDE : mix(BRONZE_SIDE, HONEY_SIDE, lit);
      // a hover dims everything else, so the picked laptop stands alone
      const focus = this.opts.reduced ? (this.hovered === null ? 0 : 1) : this.focus;
      const dimmed = hovered ? 1 : 1 - 0.55 * focus;
      // fillers fade out under the title and counts so the text stays readable
      let band = 1;
      if (c.node === null) {
        const ty = Math.min(...top.map((p) => p[1]));
        band = clamp((ty - this.h * 0.08) / (this.h * 0.22), 0.12, 1);
      }
      ctx.globalAlpha = c.fade * dimmed * band;

      // Sides facing the viewer, flat-shaded by a key light from the left.
      for (let k = 0; k < 6; k++) {
        const a = ((k + 0.5) * Math.PI) / 3;
        const nx = Math.sin(a);
        const nz = Math.cos(a);
        const nzr = nx * sin + nz * cos;
        if (nzr <= 0.02) continue;
        const nxr = nx * cos - nz * sin;
        // three flat tones: lit (left-facing), mid, shadow (right-facing)
        const lambert = 0.5 - 0.5 * nxr;
        const light = (lambert > 0.66 ? 1 : lambert > 0.33 ? 0.78 : 0.58) * (c.node === null ? c.shade : 1);
        const b0 = bottom[k];
        const b1 = bottom[(k + 1) % 6];
        const t1 = top[(k + 1) % 6];
        const t0 = top[k];
        if (!b0 || !b1 || !t0 || !t1) continue;
        ctx.beginPath();
        ctx.moveTo(b0[0], b0[1]);
        ctx.lineTo(b1[0], b1[1]);
        ctx.lineTo(t1[0], t1[1]);
        ctx.lineTo(t0[0], t0[1]);
        ctx.closePath();
        ctx.fillStyle = css(scaled(sideBase, light));
        ctx.fill();
        ctx.strokeStyle = "rgba(8,7,5,0.85)";
        ctx.lineWidth = 0.75;
        ctx.stroke();
      }

      // Top face, one flat colour.
      ctx.beginPath();
      top.forEach(([x, y], i) => {
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      if (c.node === null) {
        ctx.fillStyle = css(scaled(FILLER_TOP, c.shade));
        ctx.fill();
        ctx.strokeStyle = "rgba(92,78,56,0.55)";
        ctx.lineWidth = 0.75;
      } else {
        const topCol = offline ? BLACK_TOP : mix(BRONZE_TOP, HONEY_TOP, lit);
        ctx.fillStyle = css(hovered && !offline ? mix(topCol, [255, 226, 140], 0.35 * c.hk) : topCol);
        ctx.fill();
        ctx.strokeStyle = hovered ? "rgba(242,237,228,0.9)" : c.node.state === "online" ? "rgba(255,222,140,0.55)" : "rgba(8,7,5,0.7)";
        ctx.lineWidth = hovered ? 1.4 : 0.9;
      }
      ctx.stroke();

      if (c.node !== null) {
        this.hulls.set(c.node.id, convexHull([...bottom, ...top]));
        if (hovered) {
          // a soft hex ring spreading out from the hovered cell's base
          const ring = this.opts.reduced ? 0.6 : (this.t * 0.8) % 1;
          ctx.globalAlpha = (1 - ring) * 0.6 * c.hk;
          ctx.beginPath();
          CORNERS.forEach(([dx, dz], i) => {
            const [px, py] = this.project(c.x + dx * (1.05 + ring * 1.3), 0, c.z + dz * (1.05 + ring * 1.3));
            if (i === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          });
          ctx.closePath();
          ctx.strokeStyle = "rgba(255,201,60,0.9)";
          ctx.lineWidth = 1.2;
          ctx.stroke();
          ctx.globalAlpha = 1;
        }
      }
    }

    ctx.globalAlpha = 1;
    ctx.font = "500 9.5px 'JetBrains Mono Variable', ui-monospace, monospace";
    ctx.textAlign = "center";
    for (const l of labels) {
      const text = l.text.length > 16 ? `${l.text.slice(0, 15)}…` : l.text;
      ctx.fillStyle = l.strong ? "rgba(242,237,228,0.95)" : l.dim ? "rgba(168,160,145,0.55)" : "rgba(242,237,228,0.7)";
      ctx.fillText(text, l.x, l.y);
    }
  }
}

function cross(o: [number, number], a: [number, number], b: [number, number]): number {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

/** Monotone-chain convex hull. */
function convexHull(points: [number, number][]): [number, number][] {
  const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const build = (list: [number, number][]) => {
    const out: [number, number][] = [];
    for (const pt of list) {
      while (out.length >= 2) {
        const a = out[out.length - 2];
        const b = out[out.length - 1];
        if (a === undefined || b === undefined || cross(a, b, pt) > 0) break;
        out.pop();
      }
      out.push(pt);
    }
    out.pop();
    return out;
  };
  return build(p).concat(build([...p].reverse()));
}

function inside(poly: [number, number][], x: number, y: number): boolean {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i];
    const b = poly[j];
    if (!a || !b) continue;
    if (a[1] > y !== b[1] > y && x < ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]) + a[0]) hit = !hit;
  }
  return hit;
}
