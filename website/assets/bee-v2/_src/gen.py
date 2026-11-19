"""Geometry helper that writes the hive. bee-v2 SVG directions (h1..h6).
Every shape is a polygon / polyline built from hex & straight-line geometry."""
import math, os

OUT = r"C:\Users\HP\UI MeshForge\mf-dev\website\assets\bee-v2"
os.makedirs(OUT, exist_ok=True)

HONEY_HI = "#FFD86A"
HONEY_L = "#FFC93C"
HONEY = "#F5A623"
HONEY_M = "#D9971F"
HONEY_D = "#B97A14"
HONEY_DD = "#8F5F10"
WHITE = "#F2EDE4"
STONE = "#A59D8F"
STONE_D = "#5F584C"
INK = "#16140F"
INK2 = "#2A251C"


def hexpts(cx, cy, r, flat=False, sx=1.0, sy=1.0):
    off = 0 if flat else -90
    return [(cx + r * sx * math.cos(math.radians(60 * i + off)),
             cy + r * sy * math.sin(math.radians(60 * i + off))) for i in range(6)]


def clip(poly, a, b, c):
    """Keep the part of convex polygon where a*x + b*y <= c."""
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        fp, fq = a * p[0] + b * p[1] - c, a * q[0] + b * q[1] - c
        if fp <= 0:
            out.append(p)
        if (fp < 0 < fq) or (fq < 0 < fp):
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def band(poly, y0, y1):
    return clip(clip(poly, 0, -1, -y0), 0, 1, y1)


def half(poly, x, left=True):
    return clip(poly, 1, 0, x) if left else clip(poly, -1, 0, -x)


def P(pts):
    return " ".join(f"{x:.1f},{y:.1f}".replace(".0,", ",").rstrip("0").rstrip(".") if False else f"{round(x,1):g},{round(y,1):g}" for x, y in pts)


def poly(pts, fill, extra=""):
    return f'<polygon points="{P(pts)}" fill="{fill}"{extra}/>'


def line(pts, stroke, w, extra=""):
    return (f'<polyline points="{P(pts)}" fill="none" stroke="{stroke}" stroke-width="{w}" '
            f'stroke-linejoin="miter" stroke-linecap="square"{extra}/>')


def mirror(pts, cx=64):
    return [(2 * cx - x, y) for x, y in pts]


def svg(name, title, body, vb="0 0 128 128"):
    s = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="128" height="128" '
         f'role="img" aria-label="{title}">\n<title>{title}</title>\n' + "\n".join(body) + "\n</svg>\n")
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(s)


def eye(cx, cy, r, glint=True, fill=INK, gl=WHITE):
    out = [poly(hexpts(cx, cy, r), fill)]
    if glint:
        out.append(poly(hexpts(cx + r * .32, cy - r * .34, r * .3), gl))
    return out


# ---------------------------------------------------------------- H1 Cellwork
def h1():
    b = ['<!-- H1 Cellwork: honeycomb-cell wings, hex head, segmented hex abdomen -->',
         '<g transform="translate(0 5)">']
    wing_f = f' fill-opacity=".16" stroke="{WHITE}" stroke-opacity=".75" stroke-width="2.4" stroke-linejoin="round"'
    for side in (1, -1):
        up = hexpts(31, 42, 16, flat=True)
        lo = hexpts(33.5, 67.5, 10.5, flat=True)
        if side == -1:
            up, lo = mirror(up), mirror(lo)
        b.append(poly(up, WHITE, wing_f))
        b.append(poly(lo, WHITE, wing_f))
    # antennae with hex nodes
    for side in (1, -1):
        pts = [(58, 20), (54, 12), (49, 8)]
        node = hexpts(46.5, 6.5, 3.6)
        if side == -1:
            pts, node = mirror(pts), mirror(node)
        b.append(line(pts, WHITE, 2.4))
        b.append(poly(node, HONEY_L))
    # head: flat-top hex, lit top, shaded bottom
    head = hexpts(64, 34, 17, flat=True)
    b.append(poly(band(head, 0, 34), WHITE))
    b.append(poly(band(head, 34, 99), "#D8D0C2"))
    b += eye(57, 35, 5.6)
    b += eye(71, 35, 5.6)
    b.append(line([(61, 43), (64, 45), (67, 43)], INK, 1.6))
    # abdomen: elongated hex, honey bands separated by dark gaps (bg shows through)
    ab = [(51, 53), (77, 53), (88, 69), (88, 87), (64, 114), (40, 87), (40, 69)]
    for y0, y1 in ((53, 66), (72, 84), (90, 115)):
        seg = band(ab, y0, y1)
        b.append(poly(half(seg, 64, True), HONEY_L))
        b.append(poly(half(seg, 64, False), HONEY))
    b.append('</g>')
    svg("h1.svg", "hive bee H1 Cellwork", b)


