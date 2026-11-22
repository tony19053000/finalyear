"""Generates the hive. logo option SVGs + preview.html.
Run:  python _build.py   (from this folder or anywhere)
All geometry is computed here so angles/strokes stay exact. No fonts are used:
both wordmark styles ("Hexline" and "Geo") are drawn as SVG paths.
"""
import math, os

OUT = os.path.dirname(os.path.abspath(__file__))
INK = "#F2EDE4"      # warm white
HONEY = "#FFC93C"
HONEY_D = "#F5A623"
BLACK = "#0B0A08"
S3 = math.sqrt(3)


def f(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def pts(ps):
    return " ".join(f"{f(x)},{f(y)}" for x, y in ps)


def hexv(cx, cy, R, rot=-90):
    return [(cx + R * math.cos(math.radians(rot + 60 * k)),
             cy + R * math.sin(math.radians(rot + 60 * k))) for k in range(6)]


def svg(w, h, body, defs=""):
    d = f"<defs>{defs}</defs>" if defs else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {f(w)} {f(h)}" '
            f'width="{f(w)}" height="{f(h)}">{d}{body}</svg>\n')


# ---------------------------------------------------------------- wordmarks
# Each glyph fn returns (svg, ink_width). x = left ink edge, B = baseline.

class Hexline:
    """Lowercase built only from pointy-hex edges (0/60/120 deg + verticals)."""
    def __init__(self, xh=40, w=6.5):
        self.R = xh / 2; self.a = self.R * S3 / 2; self.w = w; self.xh = xh
        self.asc = xh * 1.42

    def h(self, x, B, c):
        R, a, w = self.R, self.a, self.w; x0 = x + w / 2
        d = (f"M{f(x0)} {f(B - self.asc)}V{f(B)}"
             f"M{f(x0)} {f(B - 1.5 * R)}L{f(x0 + a)} {f(B - 2 * R)}L{f(x0 + 2 * a)} {f(B - 1.5 * R)}V{f(B)}")
        return (f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{f(w)}" stroke-linejoin="miter"/>',
                2 * a + w)

    def i(self, x, B, c):
        R, w = self.R, self.w; x0 = x + w / 2
        top = B - 2 * R
        dr = w * 0.78
        dot = pts(hexv(x0, top - w * 0.95 - dr, dr))
        return (f'<path d="M{f(x0)} {f(top)}V{f(B)}" stroke="{c}" stroke-width="{f(w)}"/>'
                f'<polygon points="{dot}" fill="{c}"/>', w)

    def v(self, x, B, c, ang=60):
        T = B - 2 * self.R
        t = math.tan(math.radians(ang))
        tip = B + self.w * 0.18
        half = (tip - T) / t
        wh = self.w / math.sin(math.radians(ang))
        P = [(x, T), (x + half, tip), (x + 2 * half, T), (x + 2 * half - wh, T),
             (x + half, tip - wh * t), (x + wh, T)]
        return f'<polygon points="{pts(P)}" fill="{c}"/>', 2 * half

    def e(self, x, B, c):
        R, a, w = self.R, self.a, self.w; cx = x + a + w / 2
        t = 0.62
        ex, ey = cx + a * t, B - 0.5 * R * t
        d = (f"M{f(cx - a)} {f(B - R)}H{f(cx + a)}V{f(B - 1.5 * R)}L{f(cx)} {f(B - 2 * R)}"
             f"L{f(cx - a)} {f(B - 1.5 * R)}V{f(B - .5 * R)}L{f(cx)} {f(B)}L{f(ex)} {f(ey)}")
        return (f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{f(w)}" stroke-linejoin="miter"/>',
                2 * a + w)


class Geo:
    """Monoline geometric lowercase: true circles, flat stems, sharp v."""
    def __init__(self, xh=40, w=6.0):
        self.xh = xh; self.w = w; self.asc = xh * 1.42; self.o = xh * 0.015

    def h(self, x, B, c):
        w = self.w; x0 = x + w / 2; r = self.xh * 0.30
        cy = B - self.xh + w / 2 + r
        d = (f"M{f(x0)} {f(B - self.asc)}V{f(B)}"
             f"M{f(x0)} {f(B)}V{f(cy)}A{f(r)} {f(r)} 0 0 1 {f(x0 + 2 * r)} {f(cy)}V{f(B)}")
        return f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{f(w)}"/>', 2 * r + w

    def i(self, x, B, c):
        w = self.w; x0 = x + w / 2; dr = w * 0.66
        return (f'<path d="M{f(x0)} {f(B - self.xh)}V{f(B)}" stroke="{c}" stroke-width="{f(w)}"/>'
                f'<circle cx="{f(x0)}" cy="{f(B - self.xh - w * 0.9 - dr)}" r="{f(dr)}" fill="{c}"/>', w)

    v = Hexline.v

    def e(self, x, B, c):
        w, o = self.w, self.o
        rr = (self.xh + 2 * o - w) / 2; cx = x + rr + w / 2; cy = B - self.xh / 2
        an = math.radians(42)
        ex, ey = cx + rr * math.cos(an), cy + rr * math.sin(an)
        d = f"M{f(cx - rr)} {f(cy)}H{f(cx + rr)}A{f(rr)} {f(rr)} 0 1 0 {f(ex)} {f(ey)}"
        return f'<path d="{d}" fill="none" stroke="{c}" stroke-width="{f(w)}"/>', 2 * rr + w


def wordmark(font, x, B, ink, dot_c, dot="hex", gap=None, vang=60):
    """Lays out h i v e . ; returns (svg, right_edge)."""
    g = gap if gap is not None else font.xh * 0.2 + font.w * 0.55
    # Hexline gets its R from xh; Geo needs it for v
    if not hasattr(font, "R"):
        font.R = font.xh / 2
    out = []
    s, wd = font.h(x, B, ink); out.append(s); x += wd + g
    s, wd = font.i(x, B, ink); out.append(s); x += wd + g * 0.82
    s, wd = font.v(x, B, ink, vang); out.append(s); x += wd + g * 0.62
    s, wd = font.e(x, B, ink); out.append(s); x += wd + g * 0.72
    if dot == "hex":
        rd = font.w * 0.95
        out.append(f'<polygon points="{pts(hexv(x + rd * S3 / 2, B - rd + font.w*0.12, rd))}" fill="{dot_c}"/>')
        x += rd * S3
    else:
        rd = font.w * 0.82
        out.append(f'<circle cx="{f(x + rd)}" cy="{f(B - rd)}" r="{f(rd)}" fill="{dot_c}"/>')
        x += 2 * rd
    return "".join(out), x


# ---------------------------------------------------------------- marks
# Every mark: fn(ink, accent, bg, mono) -> inner svg for a 64x64 box.

def hex_d(P):
    return "M" + "L".join(f"{f(x)} {f(y)}" for x, y in P) + "Z"


def mark_L1(ink, acc, uid):
    """Open-cell h: stem + hex roof (half a cell) + honey cell full stop."""
    R = 16.5; a = R * S3 / 2; w = 6.2; B = 55.5
    rd = 5.6
    total = w + 2 * a + 4.2 + rd * S3
    x0 = (64 - total) / 2 + w / 2
    top = 7.5
    d = (f"M{f(x0)} {f(top)}V{f(B)}"
         f"M{f(x0)} {f(B - 1.5 * R)}L{f(x0 + a)} {f(B - 2 * R)}L{f(x0 + 2 * a)} {f(B - 1.5 * R)}V{f(B)}")
    dx = x0 + 2 * a + w / 2 + 4.2 + rd * S3 / 2
    return (f'<path d="{d}" fill="none" stroke="{ink}" stroke-width="{f(w)}" stroke-linejoin="miter"/>'
            f'<polygon points="{pts(hexv(dx, B - rd + 0.6, rd))}" fill="{acc}"/>')


def clip_poly(P, a, b, keep_left):
    """Sutherland-Hodgman clip of polygon P by line a->b."""
    def side(p):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    out = []
    for i in range(len(P)):
        cur, nxt = P[i], P[(i + 1) % len(P)]
        sc, sn = side(cur), side(nxt)
        ic = (sc >= 0) if keep_left else (sc <= 0)
        inn = (sn >= 0) if keep_left else (sn <= 0)
        if ic:
            out.append(cur)
        if ic != inn:
            t = sc / (sc - sn)
            out.append((cur[0] + t * (nxt[0] - cur[0]), cur[1] + t * (nxt[1] - cur[1])))
    return out


def mark_L2(ink, acc, uid):
    """Aperture: six blades (machines) closing around one honey core."""
    C = 32; R = 29.5; r = 11.0; g = 2.6
    O = hexv(C, C, R); I = hexv(C, C, r, rot=-90 + 0)
    blades = []
    for k in range(6):
        Pp = O[:]
        # outer side of L_k (through I_k, I_k+1); inner side of L_{k-1}
        Pp = clip_poly(Pp, I[k], I[(k + 1) % 6], keep_left=False)
        Pp = clip_poly(Pp, I[k - 1], I[k], keep_left=True)
        blades.append(Pp)
    mid = f"m{uid}"
    m = (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
         + "".join(f'<polygon points="{pts(b)}" fill="#fff" stroke="#000" stroke-width="{f(g)}" stroke-linejoin="round"/>' for b in blades)
         + '</mask>')
    core = r - g / S3 - 0.35
    body = (f'<g mask="url(#{mid})"><polygon points="{pts(O)}" fill="{ink}"/></g>'
            f'<polygon points="{pts(hexv(C, C, core))}" fill="{acc}"/>')
    return body, m


def inset_tri(T, d):
    """Inset a convex polygon by distance d (offset each edge inward)."""
    n = len(T)
    # signed area -> orientation
    A = sum(T[i][0] * T[(i + 1) % n][1] - T[(i + 1) % n][0] * T[i][1] for i in range(n))
    sgn = 1 if A > 0 else -1
    lines = []
    for i in range(n):
        p, q = T[i], T[(i + 1) % n]
        ex, ey = q[0] - p[0], q[1] - p[1]; L = math.hypot(ex, ey)
        nx, ny = -ey / L * sgn, ex / L * sgn
        lines.append(((p[0] + nx * d, p[1] + ny * d), (ex, ey)))
    out = []
    for i in range(n):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        den = d1[0] * d2[1] - d1[1] * d2[0]
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        out.append((p1[0] + d1[0] * t, p1[1] + d1[1] * t))
    return out


def seg_x(p1, p2, p3, p4):
    d = (p2[0]-p1[0])*(p4[1]-p3[1]) - (p2[1]-p1[1])*(p4[0]-p3[0])
    t = ((p3[0]-p1[0])*(p4[1]-p3[1]) - (p3[1]-p1[1])*(p4[0]-p3[0])) / d
    return (p1[0] + t*(p2[0]-p1[0]), p1[1] + t*(p2[1]-p1[1]))


def mark_L4(ink, acc, uid):
    """Weave: two cells linked over/under - machines joined into one fabric."""
    w = 5.2; gw = 2.8
    R = 19.6; a = R * S3 / 2; dx = 1.18 * a
    cxA = 32 - dx / 2; cxB = 32 + dx / 2; cy = 32
    A = hexv(cxA, cy, R); Bv = hexv(cxB, cy, R)
    # pointy hex order: 0 top,1 upper-right,2 lower-right,3 bottom,4 lower-left,5 upper-left
    top = seg_x(A[0], A[1], Bv[5], Bv[0])
    bot = seg_x(A[2], A[3], Bv[3], Bv[4])
    def seg(p, q, c, L):
        ex, ey = q[0] - p[0], q[1] - p[1]; n = math.hypot(ex, ey); ex /= n; ey /= n
        return (c[0] - ex * L, c[1] - ey * L), (c[0] + ex * L, c[1] + ey * L)
    L = w * 1.3
    s1 = seg(A[0], A[1], top, L)      # A over B at the top
    s2 = seg(Bv[3], Bv[4], bot, L)    # B over A at the bottom
    mA, mB = f"a{uid}", f"b{uid}"
    def cutmask(mid, sg):
        return (f'<mask id="{mid}" maskUnits="userSpaceOnUse" x="-8" y="-8" width="80" height="80">'
                f'<rect x="-8" y="-8" width="80" height="80" fill="#fff"/>'
                f'<path d="M{f(sg[0][0])} {f(sg[0][1])}L{f(sg[1][0])} {f(sg[1][1])}" stroke="#000" stroke-width="{f(w + 2 * gw)}"/></mask>')
    m = cutmask(mB, s1) + cutmask(mA, s2)
    st = f'fill="none" stroke-width="{f(w)}" stroke-linejoin="miter"'
    body = (f'<polygon points="{pts(Bv)}" {st} stroke="{ink}" mask="url(#{mB})"/>'
            f'<polygon points="{pts(A)}" {st} stroke="{acc}" mask="url(#{mA})"/>')
    return body, m


def mark_L5(ink, acc, uid):
    """Spiral: one continuous line winding into a single honey core."""
    w = 5.0; p = 9.6
    delta = p / (6 * math.sin(math.radians(60)))
    dirs = [270, -30, 30, 90, 150, 210]
    L0 = 27.0
    P = [(0.0, 0.0)]
    k = 0
    while True:
        l = (L0 - k * delta) * (0.62 if k == 0 else 1)
        if l < 6.0:
            break
        an = math.radians(dirs[k % 6])
        x, y = P[-1]
        P.append((x + l * math.cos(an), y + l * math.sin(an)))
        k += 1
    # stop the line partway into the last edge for an open, intentional terminal
    xs = [q[0] for q in P]; ys = [q[1] for q in P]
    # core: centre of the innermost ring
    inner = P[-6:]
    ccx = sum(q[0] for q in inner) / 6; ccy = sum(q[1] for q in inner) / 6
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    ox = 32 - (minx + maxx) / 2; oy = 32 - (miny + maxy) / 2
    d = "M" + "L".join(f"{f(x + ox)} {f(y + oy)}" for x, y in P)
    return (f'<path d="{d}" fill="none" stroke="{ink}" stroke-width="{f(w)}" stroke-linejoin="miter"/>'
            f'<polygon points="{pts(hexv(ccx + ox, ccy + oy, 3.4))}" fill="{acc}"/>'), (P, ox, oy)


def mark_L3(ink, acc, uid):
    """Cluster h: five honeycomb cells on a true lattice spell a lowercase h."""
    g = 2.4
    R = 11.0                      # flat-top lattice radius
    cs, rs = 1.5 * R, S3 * R
    cells = [(0, 0), (0, 1), (0, 2), (1, 1.5), (2, 2)]
    xs = [c * cs for c, r in cells]; ys = [r * rs for c, r in cells]
    ox = 32 - (min(xs) + max(xs)) / 2; oy = 32 - (min(ys) + max(ys)) / 2
    rr = R - g / S3               # shrink so the gap between cells is exactly g
    out = []
    for (c, r), x, y in zip(cells, xs, ys):
        col = acc if (c, r) == (1, 1.5) else ink
        out.append(f'<polygon points="{pts(hexv(x + ox, y + oy, rr, rot=0))}" fill="{col}"/>')
    return "".join(out)


def mark_L6(ink, acc, uid):
    """Split cell: two half-cells (two machines) with a honey chunk crossing between."""
    C = 32; R = 28.5; a = R * S3 / 2; w = 5.6; s = 4.2
    V = hexv(C, C, R)
    left = [(C + 6, C - R - 3.46), V[0], V[5], V[4], V[3], (C + 6, C + R + 3.46)]
    right = [(C - 6, C - R - 3.46), V[0], V[1], V[2], V[3], (C - 6, C + R + 3.46)]
    cl, cr = f"l{uid}", f"r{uid}"
    defs = (f'<clipPath id="{cl}"><rect x="-4" y="-4" width="{f(C - s + 4)}" height="72"/></clipPath>'
            f'<clipPath id="{cr}"><rect x="{f(C + s)}" y="-4" width="40" height="72"/></clipPath>')
    st = f'fill="none" stroke="{ink}" stroke-width="{f(w)}" stroke-linejoin="miter"'
    def poly(P, dx):
        return "M" + "L".join(f"{f(x + dx)} {f(y)}" for x, y in P)
    body = (f'<path d="{poly(left, 0)}" {st} clip-path="url(#{cl})"/>'
            f'<path d="{poly(right, 0)}" {st} clip-path="url(#{cr})"/>'
            f'<polygon points="{pts(hexv(C, C, 7.4))}" fill="{acc}"/>')
    return body, defs


# ---------------------------------------------------------------- assemble
def mk(fn, ink, acc, uid):
    r = fn(ink, acc, uid)
    if isinstance(r, tuple):
        body, extra = r
        defs = extra if isinstance(extra, str) else ""
    else:
        body, defs = r, ""
    return body, defs


CONCEPTS = [
    dict(id="L1", name="Open Cell", fn=mark_L1,
         why="The h is half a honeycomb cell, left open at the base; the full stop is the one closed honey cell. Every letter of the wordmark is cut from the same hex edges, so the wordmark is the logo and the mark is just its first letter.",
         font=("hex", 6.6), dot="hex", lockup="wordmark"),
    dict(id="L2", name="Aperture", fn=mark_L2,
         why="Six blades (six machines) close around one honey core, like a lens. Many parts, one computer.",
         font=("geo", 5.6), dot="hex"),
    dict(id="L3", name="Cluster h", fn=mark_L3,
         why="Five cells on a true honeycomb lattice spell a lowercase h; the honey cell is the joint where one machine hands work to the next.",
         font=("hex", 8.0), dot="hex"),
    dict(id="L4", name="Weave", fn=mark_L4,
         why="Two cells linked over-and-under: separate machines woven into one fabric. Extends into a chain of N.",
         font=("geo", 6.6), dot="round"),
    dict(id="L5", name="Spiral", fn=mark_L5,
         why="One unbroken line builds the cell from the outside in and lands on a honey core: everything converges.",
         font=("hex", 4.6), dot="hex"),
    dict(id="L6", name="Split Cell", fn=mark_L6,
         why="Two half-cells (two laptops) make one cell, with a honey chunk crossing the seam. Also reads as < > - a developer's tool.",
         font=("geo", 7.2), dot="hex"),
]


def mark_svg(cfg, ink, acc, uid, scale=1.0, tile=None):
    body, defs = mk(cfg["fn"], ink, acc, uid)
    if tile:
        s = scale
        body = (f'<rect width="64" height="64" rx="14" fill="{tile}"/>'
                f'<g transform="translate({f(32 - 32 * s)} {f(32 - 32 * s)}) scale({f(s)})">{body}</g>')
    return svg(64, 64, body, defs)


def lockup_svg(cfg, ink, acc, uid):
    kind, w = cfg["font"]
    font = Hexline(40, w) if kind == "hex" else Geo(40, w)
    B = 70
    markH = font.asc * 1.30          # mark box (64) height in lockup units
    s = markH / 64
    my = B - font.asc / 2 - markH / 2 - font.xh * 0.04
    if cfg.get("lockup") == "wordmark":
        body, defs, mark, x = "", "", "", 2
    else:
        body, defs = mk(cfg["fn"], ink, acc, uid)
        mark = f'<g transform="translate(0 {f(my)}) scale({f(s)})">{body}</g>'
        x = markH + font.xh * 0.42
    words, right = wordmark(font, x, B, ink, acc, dot=cfg["dot"])
    W = right + 2
    Hh = B + 12
    return svg(W, Hh, mark + words, defs)




def colors(cfg, mode):
    """mode: dark (on near-black) | mono (black on honey/light)."""
    if mode == "mono":
        return BLACK, BLACK
    return INK, HONEY


def build():
    files = []
    for cfg in CONCEPTS:
        i = cfg["id"].lower()
        ink, acc = colors(cfg, "dark")
        mi, ma = colors(cfg, "mono")
        out = {
            f"{i}-mark.svg": mark_svg(cfg, ink, acc, i + "d"),
            f"{i}-mark-mono.svg": mark_svg(cfg, mi, ma, i + "m"),
            f"{i}-favicon.svg": mark_svg(cfg, ink, acc, i + "f", scale=0.84, tile=BLACK),
            f"{i}-lockup.svg": lockup_svg(cfg, ink, acc, i + "ld"),
            f"{i}-lockup-mono.svg": lockup_svg(cfg, mi, ma, i + "lm"),
        }
        for n, s in out.items():
            with open(os.path.join(OUT, n), "w", encoding="utf-8") as fh:
                fh.write(s)
            files.append(n)
    return files


if __name__ == "__main__":
    print("\n".join(build()))


# ---------------------------------------------------------------- preview
def preview():
    cards = []
    for c in CONCEPTS:
        i = c["id"].lower()
        kind, w = c["font"]
        fdesc = ("Hexline (custom, drawn from hex edges)" if kind == "hex" else "Geo (custom monoline, true circles)")
        cards.append(f'''
<section class="card" id="{i}">
  <header><span class="num">{c["id"]}</span><h2>{c["name"]}</h2><p>{c["why"]}</p></header>
  <div class="grid">
    <div class="big dark"><img src="{i}-mark.svg" alt="{c["id"]} mark" width="200" height="200"></div>
    <div class="col">
      <div class="lock dark"><img src="{i}-lockup.svg" alt="{c["id"]} lockup" class="lk"></div>
      <div class="lock honey"><img src="{i}-lockup-mono.svg" alt="{c["id"]} lockup mono" class="lk"></div>
      <div class="row">
        <div class="tiny dark"><span>nav 28px</span><img src="{i}-lockup.svg" alt="" style="height:28px"></div>
        <div class="tiny dark"><span>32</span><img src="{i}-favicon.svg" alt="" width="32" height="32"></div>
        <div class="tiny dark"><span>16</span><img src="{i}-favicon.svg" alt="" width="16" height="16"></div>
        <div class="tiny dark"><span>16 x6</span><canvas data-src="{i}-favicon.svg"></canvas></div>
        <div class="tiny honey"><span>32</span><img src="{i}-mark-mono.svg" alt="" width="32" height="32"></div>
        <div class="tiny honey"><span>16</span><img src="{i}-mark-mono.svg" alt="" width="16" height="16"></div>
        <div class="tiny light"><span>tab</span><img src="{i}-favicon.svg" alt="" width="16" height="16"><em>hive.</em></div>
      </div>
      <p class="meta">Wordmark: {fdesc}, stroke {w}/40 x-height. Files: {i}-mark.svg · {i}-mark-mono.svg · {i}-favicon.svg · {i}-lockup.svg · {i}-lockup-mono.svg</p>
    </div>
  </div>
</section>''')
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>hive. logo options</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap">
<style>
:root{{--bg:#0B0A08;--panel:#16140F;--line:#2A251C;--honey:#F5A623;--honey2:#FFC93C;--ink:#F2EDE4;--mute:#8C8475}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Geist,system-ui,sans-serif;padding:48px 24px 80px}}
.top{{max-width:1180px;margin:0 auto 32px}}
.top h1{{font-weight:500;font-size:28px;margin:0 0 6px;letter-spacing:-.01em}}
.top p{{color:var(--mute);margin:0}}
.card{{max-width:1180px;margin:0 auto 20px;border:1px solid var(--line);border-radius:18px;background:var(--panel);padding:24px}}
.card header{{display:grid;grid-template-columns:auto auto 1fr;gap:14px;align-items:baseline;margin-bottom:18px}}
.num{{font:500 13px Geist Mono,monospace;color:var(--honey2);border:1px solid var(--line);border-radius:999px;padding:2px 10px}}
h2{{margin:0;font-weight:500;font-size:20px}}
.card header p{{margin:0;color:var(--mute)}}
.grid{{display:grid;grid-template-columns:280px 1fr;gap:16px}}
.dark{{background:var(--bg)}} .honey{{background:var(--honey)}} .light{{background:#EDEAE4;color:#333}}
.big{{border-radius:14px;display:grid;place-items:center;aspect-ratio:1;border:1px solid var(--line)}}
.col{{display:grid;gap:12px;min-width:0}}
.lock{{border-radius:14px;padding:22px 28px;display:flex;align-items:center;border:1px solid var(--line)}}
.lk{{height:64px;max-width:100%}}
.row{{display:flex;flex-wrap:wrap;gap:10px}}
.tiny{{border-radius:10px;border:1px solid var(--line);padding:10px 12px 10px;display:flex;align-items:center;gap:8px;min-height:56px;position:relative}}
.tiny span{{position:absolute;top:3px;left:8px;font:10px Geist Mono,monospace;color:var(--mute)}}
.tiny.honey span{{color:#5a3d06}} .tiny.light span{{color:#888}}
.tiny img,.tiny canvas{{margin-top:8px}}
.tiny em{{font-style:normal;font-size:12px;margin-top:8px}}
canvas{{width:96px;height:96px;image-rendering:pixelated}}
.meta{{color:var(--mute);font:12px Geist Mono,monospace;margin:0}}
@media (max-width:760px){{.grid{{grid-template-columns:1fr}}.big{{max-width:280px}}.lk{{height:44px}}}}
</style></head><body>
<div class="top"><h1>hive. — logo options L1–L6</h1>
<p>Each mark shown large, as a favicon at 32/16 px (plus a 6x pixel zoom of the real 16 px raster), mono on honey, and as a horizontal lockup. All type is drawn as SVG paths — no font dependency.</p></div>
{"".join(cards)}
<script>
const only=new URLSearchParams(location.search).get('only');
if(only){{const k=only.split(',');document.querySelectorAll('.card').forEach(c=>{{if(!k.includes(c.id))c.remove()}})}}
document.querySelectorAll('canvas[data-src]').forEach(cv=>{{
  cv.width=16;cv.height=16;const im=new Image();
  im.onload=()=>{{const x=cv.getContext('2d');x.drawImage(im,0,0,16,16)}};im.src=cv.dataset.src;
}});
</script>
</body></html>
'''
    with open(os.path.join(OUT, "preview.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    preview()
