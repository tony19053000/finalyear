"""hive. character couriers C1..C4 (from P1 Amber Courier), hero-material version.

Material = the 3D honeycomb hero: dark olive-bronze faceted hex prisms, near-black
sides, honey light leaking from inside the seams, light from the upper right.
No outlines, no white sticker parts: personality lives in the visor glow + posture.
Each take: neutral, happy, think, carry. Hex / straight-line geometry only."""
from gen import *

GLOW = "#FFE27A"
# facet bands (darker-left, lighter-right: key light from upper right)
BR = [("#4A4124", "#5A4E2A"), ("#2E2918", "#38321D"), ("#1A170E", "#211D11"), ("#100E09", "#14120B")]
BR_DARK = [("#3A3520", "#463E22"), ("#24200F", "#2B2616"), ("#14120B", "#19160D"), ("#0E0C08", "#110F09")]
LIMB = "#4E4425"
JOINT = "#6A5C30"
VISOR = "#070605"


class Doc:
    def __init__(self, uid):
        self.uid = uid
        self.defs = [f'<filter id="gl{uid}" x="-60%" y="-60%" width="220%" height="220%">'
                     f'<feGaussianBlur stdDeviation="1.6"/></filter>',
                     f'<filter id="gw{uid}" x="-80%" y="-80%" width="260%" height="260%">'
                     f'<feGaussianBlur stdDeviation="3.2"/></filter>']
        self.n = 0

    def grad(self, p0, p1, stops):
        self.n += 1
        gid = f"g{self.uid}{self.n}"
        st = "".join(f'<stop offset="{o}" stop-color="{c}" stop-opacity="{a}"/>' for o, c, a in stops)
        self.defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="{p0[0]:.1f}" y1="{p0[1]:.1f}" '
                         f'x2="{p1[0]:.1f}" y2="{p1[1]:.1f}">{st}</linearGradient>')
        return f"url(#{gid})"

    def glow(self, pts, col=HONEY_L, op=".8", wide=False):
        return poly(pts, col, f' filter="url(#{"gw" if wide else "gl"}{self.uid})" opacity="{op}"')

    def glowline(self, pts, w, col=HONEY_L, op=".8"):
        return line(pts, col, w, f' filter="url(#gl{self.uid})" opacity="{op}"')


def facets(o, cuts, cols, xs):
    """Low-poly: horizontal facet bands, each split on a staggered seam (left dark / right lit)."""
    out = []
    for i, (cl, cr) in enumerate(cols):
        s = band(o, cuts[i], cuts[i + 1])
        if len(s) < 3:
            continue
        x = xs + (2.5 if i % 2 else -2.5)
        a, b = clip(s, 1, 0, x), clip(s, -1, 0, -x)
        if len(a) > 2:
            out.append(poly(a, cl))
        if len(b) > 2:
            out.append(poly(b, cr))
    return out


def prism(x0, x1, cy, h, cols=BR, tb=3, tf=3):
    o = [(x0, cy), (x0 + tb, cy - h), (x1 - tf, cy - h), (x1, cy), (x1 - tf, cy + h), (x0 + tb, cy + h)]
    cuts = [cy - h - 1, cy - h * .55, cy - h * .05, cy + h * .5, cy + h + 1]
    return o, facets(o, cuts, cols, (x0 + x1) / 2)


def seam(d, x, cy, h, op_bloom=".55"):
    """Honey light leaking through the gap between two prisms."""
    core = [(x - .9, cy - h * .92), (x + .9, cy - h * .92), (x + .9, cy + h * .92), (x - .9, cy + h * .92)]
    return [poly(core, GLOW)], [d.glow(core, HONEY_L, op_bloom)]


def jet(d, x0, x1, cy, h, ang, flen, far=False):
    out = []
    cols = BR_DARK if not far else [(c[0], c[0]) for c in BR_DARK]
    o = [(x0, cy - h * .6), (x0 + 3, cy - h), (x1 - 2, cy - h), (x1, cy), (x1 - 2, cy + h), (x0 + 3, cy + h), (x0, cy + h * .6)]
    nz = (x0 - 1, cy)
    a = math.radians(ang)
    ux, uy = math.cos(a), math.sin(a)
    px, py = -uy, ux
    tip = (nz[0] + ux * flen, nz[1] + uy * flen)
    w = h * 1.0
    outer = [(nz[0] + px * w, nz[1] + py * w), tip, (nz[0] - px * w, nz[1] - py * w)]
    ct = (nz[0] + ux * flen * .5, nz[1] + uy * flen * .5)
    core = [(nz[0] + px * w * .45, nz[1] + py * w * .45), ct, (nz[0] - px * w * .45, nz[1] - py * w * .45)]
    k = .5 if far else 1
    out.append(poly(outer, d.grad(nz, tip, [(0, HONEY_L, .85 * k), (.55, HONEY, .35 * k), (1, HONEY, 0)])))
    out.append(poly(core, d.grad(nz, ct, [(0, GLOW, 1 * k), (1, HONEY_L, 0)])))
    out.append(d.glow([(nz[0] + px * w, nz[1] + py * w), ct, (nz[0] - px * w, nz[1] - py * w)], HONEY_L, f"{.5 * k:.2f}", wide=True))
    out.append(poly([(x0 - 2.2, cy - h * .7), (x0 + .5, cy - h * .7), (x0 + .5, cy + h * .7), (x0 - 2.2, cy + h * .7)], "#0E0C08"))
    out += facets(o, [cy - h - 1, cy - h * .45, cy + h * .1, cy + h * .55, cy + h + 1], cols, (x0 + x1) / 2)
    s = clip(clip(o, -1, 0, -(x1 - 5.5)), 1, 0, x1 - 4.3)
    out.append(poly(s, GLOW if not far else HONEY_M))
    if not far:
        out.append(d.glow(s, HONEY_L, ".5"))
    return out


# ---------------------------------------------------------------- face
def rel(cfg, fx, fy):
    hx, hy, hw, hh = cfg["head"]
    return (hx + fx * hw, hy + fy * hh)


def eye_shapes(d, cfg, expr):
    out = []
    s = cfg.get("eye", 6.0)
    near, back = rel(cfg, *cfg["eyes"][0]), rel(cfg, *cfg["eyes"][1])
    brow = cfg.get("brow", 0)
    kb = cfg.get("eye_back", .8)  # size of the far eye relative to the near one (1 = equal eyes)

    def tall(c, k, dy=0, r=1.0):
        pts = hexpts(c[0], c[1] + dy, s * k * r, sy=1.36)
        if brow:
            pts = clip(pts, brow, -1, brow * c[0] - (c[1] + dy) + s * k * .62)
        return pts

    if expr in ("neutral", "carry"):
        for c, k in ((near, 1), (back, kb)):
            e = tall(c, k)
            out += [d.glow(e, HONEY_L, ".85"), poly(e, HONEY), poly(tall(c, k, -s * k * .08, .58), GLOW)]
    elif expr == "happy":
        for c, k in ((near, 1), (back, kb)):
            w, hgt = s * k * .72, s * k * 1.0
            pts = [(c[0] - w, c[1] + hgt * .5), (c[0], c[1] - hgt * .55), (c[0] + w, c[1] + hgt * .5)]
            out += [d.glowline(pts, 3.6 * k, HONEY_L, ".9"), line(pts, GLOW, 2.6 * k)]
    elif expr == "think":
        e = hexpts(near[0], near[1] + s * .25, s * 1.0, flat=True, sy=.3)
        out += [d.glow(e, HONEY_L, ".85"), poly(e, GLOW)]
        c, k = (back[0] + s * .3, back[1] - s * .42), kb
        e = hexpts(c[0], c[1], s * k, sy=1.3)
        out += [d.glow(e, HONEY_L, ".85"), poly(e, HONEY), poly(hexpts(c[0] + s * .15, c[1] - s * .25, s * k * .55, sy=1.3), GLOW)]
    mc = rel(cfg, *cfg["mouth"])
    if expr == "neutral":
        out.append(line([(mc[0] - 2.6, mc[1] - .5), (mc[0], mc[1] + 1), (mc[0] + 2.6, mc[1] - .5)], HONEY, 1.3))
    elif expr == "happy":
        mp = [(mc[0] - 4, mc[1] - 1.4), (mc[0] + 4, mc[1] - 1.4), (mc[0] + 2.1, mc[1] + 2.2), (mc[0] - 2.1, mc[1] + 2.2)]
        out += [d.glow(mp, HONEY_L, ".7"), poly(mp, GLOW)]
    elif expr == "carry":
        out.append(line([(mc[0] - 3.4, mc[1] - .8), (mc[0], mc[1] + 1.5), (mc[0] + 3.4, mc[1] - .8)], HONEY_L, 1.5))
    elif expr == "think":
        out.append(line([(mc[0] - 2.2, mc[1] + .7), (mc[0] + 2.2, mc[1] - .3)], HONEY, 1.3))
    return out


def head(d, cfg, expr):
    hx, hy, hw, hh = cfg["head"]
    shape = cfg.get("shape", [(-1, -.4), (-.62, -1), (.5, -1), (1, -.38), (1, .42), (.62, 1), (-.6, 1), (-1, .5)])
    o = [(hx + x * hw, hy + y * hh) for x, y in shape]
    out = []
    for i, a in enumerate(cfg["antennae"]):
        pts = [rel(cfg, *p) for p in a]
        out.append(line(pts, LIMB if i == 0 else "#38321D", 2))
        n = hexpts(pts[-1][0], pts[-1][1], 2.6)
        out += [d.glow(n, HONEY_L, ".8"), poly(n, GLOW if i == 0 else HONEY)]
    cuts = [hy - hh - 1, hy - hh * .55, hy - hh * .05, hy + hh * .5, hy + hh + 1]
    out += facets(o, cuts, BR, hx - hw * .15)
    # thin lit rim on the top-right edges (key light), not an outline
    tr = [p for p in o if p[1] <= hy - hh * .3]
    if len(tr) >= 2:
        out.append(line(sorted(tr, key=lambda p: p[0])[-2:], "#7A6A38", 1.1))
    v = [rel(cfg, *p) for p in cfg["visor"]]
    # honey seam around the visor = light from inside the head
    out.append(poly(v, "none", f' stroke="{HONEY_L}" stroke-width="2.2" filter="url(#gl{d.uid})" opacity=".45"'))
    out.append(poly(v, VISOR, f' stroke="{GLOW}" stroke-width=".7" stroke-opacity=".6" stroke-linejoin="round"'))
    ys = [p[1] for p in v]
    refl = clip(clip(v, 0, 1, min(ys) + (max(ys) - min(ys)) * .26), 1, 0, max(p[0] for p in v) - hw * .1)
    if len(refl) > 2:
        out.append(poly(refl, "#14120C"))
    out += eye_shapes(d, cfg, expr)
    return out


def arm(pts, far=False):
    c = "#2E2918" if far else LIMB
    out = [line(pts, c, 3)]
    out.append(poly(hexpts(pts[1][0], pts[1][1], 1.9), "#3A3520" if far else JOINT))
    h = pts[-1]
    out.append(poly(hexpts(h[0], h[1], 3, flat=True), "#38321D" if far else "#5A4E2A"))
    if not far:
        out.append(poly(hexpts(h[0] + .6, h[1] - .6, 1.1, flat=True), HONEY_L))
    return out


def cargo(d, cx, cy, r=8.5):
    """A job: a solid honey cell (hex prism), glowing from inside."""
    top = hexpts(cx, cy, r, flat=True, sy=.55)
    dep = r * 1.05
    # visible sides: the three lower edges extruded down
    lower = [top[0], top[1], top[2], top[3]]
    sides = []
    for i, col in zip(range(3), ("#F5A623", "#D9971F", "#9A6510")):
        p, q = lower[i], lower[i + 1]
        sides.append(poly([p, q, (q[0], q[1] + dep), (p[0], p[1] + dep)], col))
    sil = top[3:] + top[:1] + [(top[0][0], top[0][1] + dep), (top[1][0], top[1][1] + dep), (top[2][0], top[2][1] + dep), (top[3][0], top[3][1] + dep)]
    return [d.glow(sil, HONEY_L, ".55", wide=True)] + sides + [poly(top, GLOW),
            poly(hexpts(cx, cy, r * .55, flat=True, sy=.55), "#FFF0B8", ' opacity=".7"')]


def thought(d, cfg):
    out = []
    for (fx, fy, r) in cfg["thought"]:
        c = rel(cfg, fx, fy)
        hx = hexpts(c[0], c[1], r)
        out += [d.glow(hx, HONEY_L, ".6"), poly(hx, HONEY)]
    return out


def cargo_pos(cfg):
    hx, hy, hw, hh = cfg["head"]
    return (hx - hw * .02, hy + hh + 7.5)


def build(cfg, pose):
    d = Doc(cfg["id"] + pose[0])
    back, body, bloom = [], [], []
    S = cfg["shoulder"]
    jets = [dict(j, flen=j["flen"] * (1.18 if pose == "happy" else 1)) for j in cfg["jets"]]
    for j in jets:
        if j.get("far"):
            back += jet(d, **j)
    if pose == "carry":
        cx, cy = cargo_pos(cfg)
        back += arm([(S[0] + 3, S[1] - 1), (cx + 1, cy - 5), (cx + 9.5, cy + 1.5)], far=True)
    # stinger: dark spike with a honey emitter at the tip
    st = cfg["stinger"]
    body.append(poly(st, "#2B2616"))
    body.append(poly([st[0], st[2], ((st[0][0] + st[1][0]) / 2, (st[0][1] + st[1][1]) / 2)], "#4A4124"))
    tipc = st[2]
    em = hexpts(tipc[0] + 1.2, tipc[1], 1.8)
    bloom.append(d.glow(em, HONEY_L, ".9"))
    body.append(poly(em, GLOW))
    # abdomen + thorax prisms with honey seams between them
    segs = [(m, BR) for m in cfg["abdomen"]] + [(cfg["thorax"], BR_DARK)]
    for (x0, x1, cy, h), cols in segs:
        _, f = prism(x0 + .8, x1 - .8, cy, h * 1.14, cols)
        c, bl = seam(d, x1, cy, min(h * 1.14, 9))
        body = c + body if False else body
        back += c
        bloom += bl
        body += f
    # neck seam
    tx1 = cfg["thorax"][1]
    c, bl = seam(d, tx1 + .5, cfg["thorax"][2] - 2, 7)
    back += c
    bloom += bl
    for j in jets:
        if not j.get("far"):
            body += jet(d, **j)
    tilt = {"think": -8, "happy": cfg.get("tilt", 0) - 5}.get(pose, cfg.get("tilt", 0))
    hx, hy, hw, hh = cfg["head"]
    neck = (hx - hw * .3, hy + hh)
    hd = [f'<g transform="rotate({tilt} {neck[0]:.1f} {neck[1]:.1f})">'] + head(d, cfg, pose) + ['</g>']
    front = []
    if pose == "think":
        front += thought(d, cfg)
    if pose == "neutral":
        front += arm([S, (S[0] + 3, S[1] + 8), (S[0] + 10, S[1] + 10.5)])
    elif pose == "happy":
        front += arm([S, (S[0] + 5, S[1] + 6), (S[0] + 12, S[1] + 3)])
    elif pose == "think":
        front += arm([S, (S[0] + 4, S[1] + 10), rel(cfg, .14, 1.1)])
    elif pose == "carry":
        cx, cy = cargo_pos(cfg)
        front += cargo(d, cx, cy)
        front += arm([S, (S[0] + 2, S[1] + 9), (cx - 7.5, cy + 4)])
    out = [f'<!-- hive character {cfg["name"]} - {pose}: hero material (bronze prisms, honey seam light) -->',
           '<defs>' + "".join(d.defs) + '</defs>',
           f'<g transform="{cfg["frame"]}">'] + back + body + bloom + hd + front + ['</g>']
    name = cfg["id"] + ("" if pose == "neutral" else "-" + pose) + ".svg"
    svg(name, f"hive assistant {cfg['name']} {pose}", out)


# ---------------------------------------------------------------- takes
C1 = dict(
    id="c1", name="C1 Buzz",
    head=(84, 48, 24, 19),
    visor=[(-.4, -.05), (-.22, -.58), (.76, -.6), (.97, -.12), (.88, .52), (-.28, .56)],
    eyes=[(.63, -.04), (.08, -.04)], eye=5.6, eye_back=1.0, mouth=(.36, .42),
    antennae=[[(-.35, -.95), (-.55, -1.55), (-.78, -1.72)]],
    thorax=(50, 70, 64, 11), abdomen=[(24, 36, 70, 7.5), (35, 51, 67, 10)],
    stinger=[(24.5, 66.5), (24.5, 73.5), (13, 71.5)],
    jets=[dict(x0=50, x1=66, cy=80, h=4.6, ang=122, flen=24),
          dict(x0=40, x1=54, cy=77, h=3.8, ang=128, flen=18, far=True)],
    shoulder=(70, 68), cargo=(93, 84),
    thought=[(1.1, -1.25, 1.6), (1.32, -1.6, 2.2), (1.6, -2.0, 2.9)],
    tilt=-3, frame="translate(2 6) rotate(-4 64 64)",
)

C2 = dict(
    id="c2", name="C2 Chibi",
    head=(78, 47, 30, 25),
    shape=[(-1, -.35), (-.66, -1), (.55, -1), (1, -.4), (1, .45), (.66, 1), (-.62, 1), (-1, .45)],
    visor=[(-.3, -.08), (-.15, -.56), (.8, -.58), (.97, -.1), (.9, .5), (-.18, .54)],
    eyes=[(.64, -.06), (.13, -.06)], eye=6.8, mouth=(.38, .38),
    antennae=[[(-.2, -.98), (-.32, -1.42), (-.5, -1.58)], [(-.55, -.9), (-.8, -1.3), (-1.0, -1.38)]],
    thorax=(38, 52, 66, 9.5), abdomen=[(26, 39, 69, 8.5)],
    stinger=[(26.5, 65.5), (26.5, 72.5), (16, 70)],
    jets=[dict(x0=40, x1=54, cy=80, h=4.4, ang=116, flen=22),
          dict(x0=30, x1=43, cy=77, h=3.6, ang=122, flen=16, far=True)],
    shoulder=(52, 70), cargo=(86, 89),
    thought=[(1.02, -1.08, 1.6), (1.2, -1.36, 2.2), (1.42, -1.7, 2.9)],
    tilt=-4, frame="translate(4 4) rotate(-3 64 64)",
)

C3 = dict(
    id="c3", name="C3 Helm",
    head=(86, 46, 22, 22),
    shape=[(-1, -.3), (-.7, -1), (.45, -1), (1, -.5), (1, .55), (.7, 1), (-.7, 1), (-1, .5)],
    visor=[(-.55, -.32), (.86, -.36), (1.0, .04), (.88, .4), (-.55, .4), (-.7, .04)],
    eyes=[(.62, -.03), (.02, -.03)], eye=5.2, mouth=(.32, .3),
    antennae=[[(-.2, -.98), (-.25, -1.5), (-.45, -1.66)], [(-.55, -.95), (-.72, -1.38), (-.92, -1.48)]],
    thorax=(50, 66, 62, 10), abdomen=[(16, 26, 69, 6), (25, 38, 67, 8), (37, 51, 64.5, 9.5)],
    stinger=[(16.5, 66), (16.5, 72), (8, 70)],
    jets=[dict(x0=44, x1=60, cy=76, h=4.4, ang=160, flen=26),
          dict(x0=34, x1=48, cy=73, h=3.6, ang=163, flen=20, far=True)],
    shoulder=(66, 66), cargo=(92, 84),
    thought=[(1.12, -1.1, 1.6), (1.32, -1.42, 2.2), (1.58, -1.78, 2.9)],
    tilt=-2, frame="translate(2 8) rotate(-5 64 64)",
)

C4 = dict(
    id="c4", name="C4 Ace",
    head=(86, 50, 25, 16),
    shape=[(-1, -.3), (-.6, -1), (.4, -1), (1, -.25), (.94, .5), (.55, 1), (-.6, 1), (-1, .55)],
    visor=[(-.42, -.2), (-.24, -.66), (.5, -.62), (1.0, -.1), (.86, .5), (-.3, .56)],
    eyes=[(.62, .0), (.1, .0)], eye=5.6, brow=.2, mouth=(.36, .4),
    antennae=[[(-.3, -.95), (-.62, -1.5), (-.95, -1.62)]],
    thorax=(52, 70, 64, 10), abdomen=[(27, 39, 68, 7), (38, 53, 66, 9)],
    stinger=[(27.5, 65), (27.5, 71), (20, 68.5)],
    jets=[dict(x0=21, x1=30, cy=68, h=3.4, ang=184, flen=26),
          dict(x0=54, x1=67, cy=78, h=4.0, ang=130, flen=16)],
    shoulder=(70, 67), cargo=(95, 85),
    thought=[(1.05, -1.5, 1.6), (1.25, -2.0, 2.2), (1.5, -2.6, 2.9)],
    tilt=-4, frame="translate(4 6) rotate(-6 64 64)",
)

TAKES = (C1, C2, C3, C4)

if __name__ == "__main__":
    for c in TAKES:
        for p in ("neutral", "happy", "think", "carry"):
            build(c, p)
    print("ok")
