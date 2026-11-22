"""Round 2 - honeycomb SYMBOLS (M1-M6), paired with the site's existing wordmark.
Run:  python _build_m.py
Writes m*-mark.svg, m*-mark-mono.svg, m*-favicon.svg and preview-honeycomb.html.
The wordmark is NOT redrawn: the preview uses the exact site CSS
(.wordmark = Bricolage Grotesque 700, -0.045em, honey hex .dot).
"""
import math, os
from _build import f, pts, hexv, svg, S3, INK, HONEY, BLACK

OUT = os.path.dirname(os.path.abspath(__file__))
MUTE = "#5C5446"


def rhex(cx, cy, R, rc=0.0, rot=-90):
    """Hexagon path with corner radius rc (exact tangent arcs)."""
    V = hexv(cx, cy, R, rot)
    if rc <= 0:
        return "M" + "L".join(f"{f(x)} {f(y)}" for x, y in V) + "Z"
    t = rc / math.tan(math.radians(60))          # tangent length for a 120 deg corner
    d = []
    for k in range(6):
        p0, p1, p2 = V[k - 1], V[k], V[(k + 1) % 6]
        def toward(a, b, L):
            ex, ey = b[0] - a[0], b[1] - a[1]; n = math.hypot(ex, ey)
            return (a[0] + ex / n * L, a[1] + ey / n * L)
        a = toward(p1, p0, t); b = toward(p1, p2, t)
        d.append(("M" if k == 0 else "L") + f"{f(a[0])} {f(a[1])}")
        d.append(f"A{f(rc)} {f(rc)} 0 0 1 {f(b[0])} {f(b[1])}")
    return "".join(d) + "Z"


def fit(centers, R, box=64, margin=4.0, rot=-90):
    """Offset that centres a set of hex cells (radius R) in the box; returns (ox, oy, scale)."""
    xs, ys = [], []
    for cx, cy in centers:
        for x, y in hexv(cx, cy, R, rot):
            xs.append(x); ys.append(y)
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = min((box - 2 * margin) / w, (box - 2 * margin) / h)
    ox = box / 2 - (min(xs) + max(xs)) / 2 * s
    oy = box / 2 - (min(ys) + max(ys)) / 2 * s
    return ox, oy, s


def pointy_lattice(q, r, R):
    """Axial coords -> centre, pointy-top cells."""
    return (S3 * R * (q + r / 2), 1.5 * R * r)


def flat_lattice(col, row, R):
    return (1.5 * R * col, S3 * R * row)


# ------------------------------------------------------------------ marks
# each: fn(ink, acc, uid, mono) -> (body, defs)

def m1(ink, acc, uid, mono):
    """Triad: three precision-cut cells, the top one lit."""
    R = 16.0; g = 3.4; rc = 2.6
    cells = [(0, 0), (1, 0), (1, -1)]           # bottom-left, bottom-right, top
    C = [pointy_lattice(q, r, R) for q, r in cells]
    ox, oy, s = fit(C, R)
    out = []
    for i, (x, y) in enumerate(C):
        col = acc if i == 2 else ink
        out.append(f'<path d="{rhex(x * s + ox, y * s + oy, (R - g / S3) * s, rc * s)}" fill="{col}"/>')
    return "".join(out), ""


def m2(ink, acc, uid, mono):
    """Stack: three isometric blocks on the comb lattice; one honey block on top."""
    R = 16.0; g = 3.0; fg = 1.15                  # gap between blocks / hairline between faces
    cells = [(0, 0), (1, 0), (1, -1)]
    C = [pointy_lattice(q, r, R) for q, r in cells]
    ox, oy, s = fit(C, R)
    if mono:
        tones = [(ink, ink, ink)] * 2 + [(acc, acc, acc)]
    else:
        tones = [(INK, "#A59D8F", "#5F584C")] * 2 + [(HONEY, "#D9971F", "#8F5F10")]
    out = []
    for i, (x, y) in enumerate(C):
        cx, cy = x * s + ox, y * s + oy
        rr = (R - g / S3) * s
        V = hexv(cx, cy, rr)
        c = (cx, cy)
        faces = [[V[0], V[1], c, V[5]],         # top
                 [V[5], c, V[3], V[4]],         # left
                 [c, V[1], V[2], V[3]]]         # right
        for face, col in zip(faces, tones[i]):
            out.append(f'<polygon points="{pts(inset(face, fg / 2))}" fill="{col}"/>')
    return "".join(out), ""


def inset(P, d):
    n = len(P)
    A = sum(P[i][0] * P[(i + 1) % n][1] - P[(i + 1) % n][0] * P[i][1] for i in range(n))
    sg = 1 if A > 0 else -1
    L = []
    for i in range(n):
        p, q = P[i], P[(i + 1) % n]
        ex, ey = q[0] - p[0], q[1] - p[1]; m = math.hypot(ex, ey)
        nx, ny = -ey / m * sg, ex / m * sg
        L.append(((p[0] + nx * d, p[1] + ny * d), (ex, ey)))
    out = []
    for i in range(n):
        (p1, d1), (p2, d2) = L[i - 1], L[i]
        den = d1[0] * d2[1] - d1[1] * d2[0]
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        out.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
    return out


def m3(ink, acc, uid, mono):
    """Lift: a seven-cell comb with one cell lifting out of its socket."""
    R = 10.0; g = 2.8; rc = 1.6; lift = 0.62
    ring = [(1, -1), (1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1)]
    cells = [(0, 0)] + ring
    C = [pointy_lattice(q, r, R) for q, r in cells]
    up = 0                                          # ring[0] = upper-right neighbour
    ux, uy = C[1][0] * lift, C[1][1] * lift
    moved = (C[1][0] + ux, C[1][1] + uy)
    ox, oy, s = fit(C + [moved], R, margin=3.5)
    rr = (R - g / S3) * s
    out = []
    for i, (x, y) in enumerate(C):
        if i == 1:
            out.append(f'<path d="{rhex(x * s + ox, y * s + oy, rr - 0.6 * s, rc * s)}" fill="none" '
                       f'stroke="{ink if mono else MUTE}" stroke-width="{f(1.2 * s)}" opacity="{0.45 if mono else 1}"/>')
            continue
        out.append(f'<path d="{rhex(x * s + ox, y * s + oy, rr, rc * s)}" fill="{ink}"/>')
    out.append(f'<path d="{rhex(moved[0] * s + ox, moved[1] * s + oy, rr, rc * s)}" fill="{acc}"/>')
    return "".join(out), ""


def m4(ink, acc, uid, mono):
    """Wax: comb walls of one constant thickness; one cell is capped with honey."""
    R = 10.4; t = 3.4; rc_in = 1.5
    ring = [(1, -1), (1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1)]
    cells = [(0, 0)] + ring
    C = [pointy_lattice(q, r, R) for q, r in cells]
    ox, oy, s = fit(C, R + t / S3, margin=4.0)
    mid = f"w{uid}"
    outerR = (R + t / S3) * s          # cell grown by half a wall
    holeR = (R - t / S3) * s           # cell shrunk by half a wall
    shell = "".join(f'<path d="{rhex(x * s + ox, y * s + oy, outerR, 1.4 * s)}" fill="#fff"/>' for x, y in C)
    holes = "".join(f'<path d="{rhex(x * s + ox, y * s + oy, holeR, rc_in * s)}" fill="#000"/>' for x, y in C)
    defs = (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
            f'{shell}{holes}</mask>')
    cap = C[0]
    body = (f'<rect width="64" height="64" fill="{ink}" mask="url(#{mid})"/>'
            f'<path d="{rhex(cap[0] * s + ox, cap[1] * s + oy, holeR - 1.6 * s, max(rc_in - .6, .6) * s)}" fill="{acc}"/>')
    return body, defs


def m5(ink, acc, uid, mono):
    """Slice: one bold cell cut exactly along the comb lattice - a cross-section.
    The big cell's edges run through the centres of the six neighbour cells and its
    corners land on lattice vertices, so every wall ends cleanly in a corner."""
    C = 32; R = 29.5; c = 3.8; rc = 2.2
    Rc = R / 2                                  # lattice cell radius
    mid = f"s{uid}"
    inner = hexv(C, C, Rc)
    outer = hexv(C, C, R)
    walls = f'<path d="{rhex(C, C, Rc)}" fill="none" stroke="#000" stroke-width="{f(c)}"/>'
    walls += "".join(f'<path d="M{f(inner[k][0])} {f(inner[k][1])}L{f(outer[k][0] + (outer[k][0]-C)*.2)} {f(outer[k][1] + (outer[k][1]-C)*.2)}" stroke="#000" stroke-width="{f(c)}"/>' for k in range(6))
    defs = (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
            f'<path d="{rhex(C, C, R, rc)}" fill="#fff"/>{walls}</mask>')
    body = (f'<rect width="64" height="64" fill="{ink}" mask="url(#{mid})"/>'
            f'<path d="{rhex(C, C, Rc - c / S3, 1.2)}" fill="{acc}"/>')
    return body, defs


def m6(ink, acc, uid, mono):
    """Quiet h: four flat-top cells on the lattice - a tall pair, a lit joint, a
    landing cell. Reads as comb first; the h is only implied by the silhouette."""
    R = 12.0; g = 3.4; rc = 2.0
    cells = [(0, 0), (0, 1), (2, 1), (1, 0.5)]          # last = lit joint
    C = [flat_lattice(c, r, R) for c, r in cells]
    ox, oy, s = fit(C, R, margin=4.0, rot=0)
    out = []
    for i, (x, y) in enumerate(C):
        out.append(f'<path d="{rhex(x * s + ox, y * s + oy, (R - g / S3) * s, rc * s, rot=0)}" fill="{acc if i == len(C) - 1 else ink}"/>')
    return "".join(out), ""


MARKS = [
    ("M1", "Triad", m1, "Three precision-cut cells, one lit: the smallest possible comb - two machines and the one that just joined."),
    ("M2", "Stack", m2, "Each cell is also an isometric block (three tones), so the comb reads as stacked compute; the honey block sits on top."),
    ("M3", "Lift", m3, "A seven-cell comb with one cell lifting out of its socket: a chunk of work in transit between machines."),
    ("M4", "Wax", m4, "Real comb anatomy: walls of one constant thickness, rounded cell corners, one cell capped with honey."),
    ("M5", "Slice", m5, "One bold cell cut exactly along the comb lattice: a cross-section where every wall ends in a corner. One computer, made of many cells."),
    ("M6", "Quiet h", m6, "Four comb cells: a tall pair, a lit joint, a landing cell. It reads as comb first; the h of hive is only implied by the silhouette."),
]


def mark_file(fn, ink, acc, uid, mono, tile=None, scale=1.0):
    body, defs = fn(ink, acc, uid, mono)
    if tile:
        body = (f'<rect width="64" height="64" rx="14" fill="{tile}"/>'
                f'<g transform="translate({f(32 - 32 * scale)} {f(32 - 32 * scale)}) scale({f(scale)})">{body}</g>')
    return svg(64, 64, body, defs)


def build():
    for mid, name, fn, why in MARKS:
        i = mid.lower()
        files = {
            f"{i}-mark.svg": mark_file(fn, INK, HONEY, i + "d", False),
            f"{i}-mark-mono.svg": mark_file(fn, BLACK, BLACK, i + "m", True),
            f"{i}-favicon.svg": mark_file(fn, INK, HONEY, i + "f", False, tile=BLACK, scale=0.86),
        }
        for n, s in files.items():
            with open(os.path.join(OUT, n), "w", encoding="utf-8") as fh:
                fh.write(s)


def preview():
    cards = []
    for mid, name, fn, why in MARKS:
        i = mid.lower()
        cards.append(f'''
<section class="card" id="{i}">
  <header><span class="num">{mid}</span><h2>{name}</h2><p>{why}</p></header>
  <div class="grid">
    <div class="big dark"><img src="{i}-mark.svg" alt="{mid} mark" width="200" height="200"></div>
    <div class="col">
      <div class="lock dark"><a class="brand xl"><img src="{i}-mark.svg" alt=""><span class="wordmark">hive<span class="dot"></span></span></a></div>
      <div class="lock honey"><a class="brand xl mono"><img src="{i}-mark-mono.svg" alt=""><span class="wordmark">hive<span class="dot"></span></span></a></div>
      <div class="row">
        <div class="tiny dark nav"><span>site header (exact)</span><a class="brand"><img src="{i}-mark.svg" alt=""><span class="wordmark">hive<span class="dot"></span></span></a></div>
        <div class="tiny dark"><span>32</span><img src="{i}-favicon.svg" alt="" width="32" height="32"></div>
        <div class="tiny dark"><span>16</span><img src="{i}-favicon.svg" alt="" width="16" height="16"></div>
        <div class="tiny dark"><span>16 x6</span><canvas data-src="{i}-favicon.svg"></canvas></div>
        <div class="tiny honey"><span>32</span><img src="{i}-mark-mono.svg" alt="" width="32" height="32"></div>
        <div class="tiny honey"><span>16</span><img src="{i}-mark-mono.svg" alt="" width="16" height="16"></div>
        <div class="tiny light"><span>tab</span><img src="{i}-favicon.svg" alt="" width="16" height="16"><em>hive.</em></div>
      </div>
      <p class="meta">{i}-mark.svg · {i}-mark-mono.svg · {i}-favicon.svg</p>
    </div>
  </div>
</section>''')
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>hive. honeycomb marks</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#0B0A08;--panel:#16140F;--line:#2A251C;--honey:#FFC93C;--honey-d:#F5A623;--text:#F2EDE4;--mute:#8C8475;
--f-display:"Bricolage Grotesque",ui-sans-serif,system-ui,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 Geist,system-ui,sans-serif;padding:48px 24px 80px}}
.top{{max-width:1180px;margin:0 auto 32px}}
.top h1{{font-weight:500;font-size:28px;margin:0 0 6px;letter-spacing:-.01em}}
.top p{{color:var(--mute);margin:0;max-width:820px}}
.card{{max-width:1180px;margin:0 auto 20px;border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:24px}}
.card header{{display:grid;grid-template-columns:auto auto 1fr;gap:14px;align-items:baseline;margin-bottom:18px}}
.num{{font:500 13px Geist Mono,monospace;color:var(--honey);border:1px solid var(--line);border-radius:999px;padding:2px 10px}}
h2{{margin:0;font-weight:500;font-size:20px}}
.card header p{{margin:0;color:var(--mute)}}
.grid{{display:grid;grid-template-columns:280px 1fr;gap:16px}}
.dark{{background:var(--bg)}} .honey{{background:var(--honey-d)}} .light{{background:#EDEAE4;color:#333}}
.big{{border-radius:14px;display:grid;place-items:center;aspect-ratio:1;border:1px solid var(--line)}}
.col{{display:grid;gap:12px;min-width:0}}
.lock{{border-radius:14px;padding:22px 28px;display:flex;align-items:center;border:1px solid var(--line);min-height:112px}}
.row{{display:flex;flex-wrap:wrap;gap:10px}}
.tiny{{border-radius:10px;border:1px solid var(--line);padding:10px 12px;display:flex;align-items:center;gap:8px;min-height:62px;position:relative}}
.tiny>span{{position:absolute;top:3px;left:8px;font:10px Geist Mono,monospace;color:var(--mute)}}
.tiny.honey>span{{color:#5a3d06}} .tiny.light>span{{color:#888}}
.tiny>img,.tiny>canvas,.tiny>em,.tiny>.brand{{margin-top:10px}}
.tiny em{{font-style:normal;font-size:12px}}
canvas{{width:96px;height:96px;image-rendering:pixelated}}
.meta{{color:var(--mute);font:12px Geist Mono,monospace;margin:0}}
/* exact copy of the site's brand CSS (assets/css/site.css) */
.brand{{display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:var(--text)}}
.brand img{{width:30px;height:30px;flex:none}}
.wordmark{{font:700 25px/1 var(--f-display);letter-spacing:-.045em;display:inline-flex;align-items:baseline}}
.wordmark .dot{{display:inline-block;width:.3em;height:.34em;margin-left:.06em;background:var(--honey);clip-path:polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%)}}
.brand.xl{{gap:22px}} .brand.xl img{{width:64px;height:64px}} .brand.xl .wordmark{{font-size:56px}}
.brand.mono{{color:#0B0A08}} .brand.mono .dot{{background:#0B0A08}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}.big{{max-width:280px}}.brand.xl img{{width:44px;height:44px}}.brand.xl .wordmark{{font-size:38px}}}}
</style></head><body>
<div class="top"><h1>hive. — honeycomb marks M1–M6</h1>
<p>New symbol only; the wordmark is the site's existing one (Bricolage Grotesque 700 + honey hex dot, same CSS). Each mark: large, favicon at 32/16 px plus a 6x zoom of the real 16 px raster, mono on honey, and in the exact site header size (30 px mark + 25 px wordmark).</p></div>
{"".join(cards)}
<script>
const only=new URLSearchParams(location.search).get('only');
if(only){{const k=only.split(',');document.querySelectorAll('.card').forEach(c=>{{if(!k.includes(c.id))c.remove()}})}}
document.querySelectorAll('canvas[data-src]').forEach(cv=>{{
  cv.width=16;cv.height=16;const im=new Image();
  im.onload=()=>{{cv.getContext('2d').drawImage(im,0,0,16,16)}};im.src=cv.dataset.src;
}});
</script>
</body></html>
'''
    with open(os.path.join(OUT, "preview-honeycomb.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    build()
    preview()
    print("ok")
