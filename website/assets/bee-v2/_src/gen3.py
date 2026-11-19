"""hive. Prism courier-drone variations P1..P5 (+ carry poses).
Profile, facing right, nose-down flight tilt. Hex/straight-line geometry only."""
from gen import *

GRAPH = ("#5F584C", "#4A4439", "#36312A")     # graphite core
STONEC = (WHITE, STONE, STONE_D)              # logo white-cube faces
AMBER = (HONEY_L, HONEY, HONEY_M)
AMBER_D = ("#8F5F10", "#6E4A0E", "#523710")
RING_H = (HONEY_L, HONEY, HONEY_M)
RING_W = (WHITE, "#D8D0C2", "#B3AB9C")
TILT = 6


def defs(uid):
    return (f'<defs>'
            f'<linearGradient id="w{uid}" gradientUnits="userSpaceOnUse" x1="0" y1="6" x2="0" y2="50">'
            f'<stop offset="0" stop-color="{WHITE}" stop-opacity=".62"/>'
            f'<stop offset="1" stop-color="{WHITE}" stop-opacity=".08"/></linearGradient>'
            f'<filter id="glow{uid}" x="-50%" y="-50%" width="200%" height="200%">'
            f'<feGaussianBlur stdDeviation="2.2"/></filter>'
            f'</defs>')


def glow(pts, uid, col=HONEY_L, op=".7"):
    return poly(pts, col, f' filter="url(#glow{uid})" opacity="{op}"')


def outline(x0, x1, cy, h, tb=3, tf=3):
    """Side view of a hex prism module: chamfered ends, flat top/bottom."""
    return [(x0, cy), (x0 + tb, cy - h), (x1 - tf, cy - h), (x1, cy), (x1 - tf, cy + h), (x0 + tb, cy + h)]


def strips(o, cy, h, cols, split=None):
    """Three facets of a hex prism: lit top, side, shaded underside."""
    out = []
    y = [cy - h - 1, cy - h * .28, cy + h * .42, cy + h + 1]
    for i in range(3):
        s = band(o, y[i], y[i + 1])
        if not s:
            continue
        if split is None:
            out.append(poly(s, cols[i]))
        else:  # low-poly: split each facet on a diagonal
            xa, xb = split
            a = clip(s, 1, 0, (xa + xb) / 2 + (2 if i % 2 else -2))
            b2 = clip(s, -1, 0, -((xa + xb) / 2 + (2 if i % 2 else -2)))
            out.append(poly(a, cols[i]))
            out.append(poly(b2, shade(cols[i])))
    return out


def shade(c):
    m = {HONEY_L: HONEY_HI, HONEY: HONEY_L, HONEY_M: HONEY, "#8F5F10": "#7A5212",
         "#6E4A0E": "#5E400D", "#523710": "#463010"}
    return m.get(c, c)


def ring(o, cy, h, x, w, cols):
    s = clip(clip(o, -1, 0, -x), 1, 0, x + w)
    return strips(s, cy, h, cols) if s else []


def wings(uid, back=True, front=True, sweep=0):
    w = f' stroke="{WHITE}" stroke-opacity=".7" stroke-width="1.3" stroke-linejoin="round"'
    out = []
    if back:
        out.append(poly([(73, 47), (64, 48), (29 + sweep, 34), (37 + sweep, 25)], f"url(#w{uid})", ' fill-opacity=".7"' + w))
    if front:
        out.append(poly([(87, 46), (75, 47), (50 + sweep, 15), (64 + sweep, 9)], f"url(#w{uid})", w))
        out.append(poly([(87, 46), (64 + sweep, 9), (56 + sweep, 12)], WHITE, ' fill-opacity=".22"'))
    return out


def cargo(cx=79, cy=92, w=10):
    """A job: the logo's honey cube (an isometric cube reads as a hexagon)."""
    h = w * math.tan(math.radians(30))
    e = 2 * h
    top = [(cx, cy - h), (cx + w, cy), (cx, cy + h), (cx - w, cy)]
    left = [(cx - w, cy), (cx, cy + h), (cx, cy + h + e), (cx - w, cy + e)]
    right = [(cx, cy + h), (cx + w, cy), (cx + w, cy + e), (cx, cy + h + e)]
    return [poly(top, HONEY_L), poly(left, HONEY_M), poly(right, HONEY_DD)]


def clamp(carry, x0=74, x1=84, y=71, col=STONE, cx=79, cy=92, w=10):
    out = [poly([(x0 - 1.5, y - 1), (x1 + 1.5, y - 1), (x1 - 1, y + 3.5), (x0 + 1, y + 3.5)], col)]
    e = 2 * w * math.tan(math.radians(30))
    for s in (-1, 1):
        xa = x0 if s < 0 else x1
        if carry:
            elbow = (cx + s * (w + 4.5), y + 8.5)
            tip = (cx + s * (w + 1.2), cy + e * .5)
            out.append(line([(xa, y + 2.5), elbow, tip], col, 3))
            out.append(poly(hexpts(elbow[0], elbow[1], 2.4), WHITE))
            out.append(poly([(tip[0] - s * 2.2, tip[1] - 3.5), (tip[0] + s * 1.6, tip[1] - 4.5),
                             (tip[0] + s * 1.6, tip[1] + 3.5), (tip[0] - s * 2.2, tip[1] + 2.5)], WHITE))
        else:
            elbow = (xa + s * 3.5, y + 6.5)
            out.append(line([(xa, y + 2.5), elbow, (cx + s * 1.6, y + 10)], col, 3))
            out.append(poly(hexpts(elbow[0], elbow[1], 2.2), WHITE))
    return out


def head_visor(o, cy, h, x_from, nose, uid, slit=HONEY_L):
    vis = clip(band(o, cy - h * .52, cy + h * .2), -1, 0, -x_from)
    sl = clip(clip(band(o, cy - h * .3, cy - h * .06), -1, 0, -(x_from + 4)), 1, 0, nose - 4)
    return [poly(vis, INK), glow(sl, uid, slit, ".9"), poly(sl, slit)]


def wrap(name, title, uid, body, carry):
    b = [f'<!-- {title} -->', defs(uid), f'<g transform="translate(64 66) scale(1.06) rotate({TILT}) translate(-64 -60)">'] + body + ['</g>']
    svg(name, title, b)


# ------------------------------------------------------------------ P1
def p1(carry):
    u = "1c" if carry else "1"
    b = wings(u, front=False)
    mods = [(44, 64, 62, 14), (28, 44, 65, 12), (16, 28, 68, 9)]
    for i, (x0, x1, cy, h) in enumerate(mods):
        o = outline(x0, x1, cy, h, 3, 3)
        b += strips(o, cy, h, AMBER, split=(x0, x1))
        b += ring(o, cy, h, x0 + 3, 3, RING_W)
    b.append(poly([(16, 64), (16, 72), (5, 69)], HONEY_M))
    b.append(poly([(16, 64), (5, 69), (9, 66)], HONEY_L))
    b += clamp(carry, col=STONE)
    o = outline(65, 88, 58, 14, 3, 4)
    b += strips(o, 58, 14, AMBER_D, split=(65, 88))
    o = outline(88, 114, 56, 11.5, 2, 9)
    b += strips(o, 56, 11.5, AMBER, split=(88, 106))
    b += head_visor(o, 56, 11.5, 97, 114, u, WHITE)
    b.append(line([(97, 45), (101, 33), (110, 27)], HONEY_L, 2.2))
    b.append(poly(hexpts(111.5, 26, 3), HONEY_L))
    b += wings(u, back=False)
    if carry:
        b += cargo()
    wrap(f"p1{'-carry' if carry else ''}.svg", "hive courier P1 Amber Courier" + (" carrying" if carry else ""), u, b, carry)


# ------------------------------------------------------------------ P2
def p2(carry):
    u = "2c" if carry else "2"
    b = wings(u, front=False)
    mods = [(44, 64, 62, 14), (28, 44, 65, 12), (16, 28, 68, 9)]
    for x0, x1, cy, h in mods:
        o = outline(x0, x1, cy, h, 3, 3)
        b += strips(o, cy, h, STONEC)
        rg = clip(clip(o, -1, 0, -(x0 + 3.5)), 1, 0, x0 + 6.7)
        b.append(glow(rg, u, HONEY_L, ".3"))
        b += ring(o, cy, h, x0 + 3.5, 3.2, RING_H)
    tail = [(16, 64), (16, 72), (4, 69)]
    b.append(glow(tail, u, HONEY_L, ".8"))
    b.append(poly(tail, HONEY_L))
    b.append(poly([(16, 68.5), (16, 72), (4, 69)], HONEY))
    b += clamp(carry, col=STONE)
    o = outline(65, 88, 58, 14, 3, 4)
    b += strips(o, 58, 14, GRAPH)
    b.append(poly(band(clip(o, -1, 0, -70), 56, 58.6), HONEY))  # core status light
    o = outline(88, 114, 56, 11.5, 2, 9)
    b += strips(o, 56, 11.5, STONEC)
    b += head_visor(o, 56, 11.5, 97, 114, u)
    b.append(line([(97, 45), (101, 33), (110, 27)], WHITE, 2.2))
    b.append(poly(hexpts(111.5, 26, 3), HONEY_L))
    b += wings(u, back=False)
    if carry:
        b += cargo()
    wrap(f"p2{'-carry' if carry else ''}.svg", "hive courier P2 Stone Courier" + (" carrying" if carry else ""), u, b, carry)


# ------------------------------------------------------------------ P3
def p3(carry):
    u = "3c" if carry else "3"
    b = wings(u, front=False, sweep=-2)
    # chain of hex cells (flat-top) linked by honey light bars
    cells = [(15, 69, 6.5), (31, 66, 9.5), (52, 62, 12.5), (76, 58, 12), (99, 56, 10.5)]
    for (xa, ya, ra), (xb, yb, rb) in zip(cells, cells[1:]):
        b.append(line([(xa + ra - 1, ya), (xb - rb + 1, yb)], HONEY_L, 3.4))
    b += clamp(carry, col=STONE)
    for i, (x, y, r) in enumerate(cells):
        hx = hexpts(x, y, r, flat=True)
        if i == 0:
            b.append(glow(hx, u, HONEY_L, ".8"))
            b.append(poly(band(hx, 0, y), HONEY_L)); b.append(poly(band(hx, y, 200), HONEY))
            continue
        top, bot = (WHITE, STONE) if i != 3 else (GRAPH[0], GRAPH[2])
        b.append(poly(band(hx, 0, y), top))
        b.append(poly(band(hx, y, 200), bot))
        if i in (1, 2):  # honey light strip across the cell
            s = clip(clip(hx, -1, 0, -(x - r * .18)), 1, 0, x + r * .18)
            b.append(poly(band(s, 0, y), HONEY_L)); b.append(poly(band(s, y, 200), HONEY))
    x, y, r = cells[-1]
    hx = hexpts(x, y, r, flat=True)
    vis = clip(band(hx, y - r * .42, y + r * .2), -1, 0, -(x - r * .05))
    sl = clip(clip(band(hx, y - r * .24, y - r * .02), -1, 0, -(x + 1)), 1, 0, x + r - 3)
    b += [poly(vis, INK), glow(sl, u), poly(sl, HONEY_L)]
    b.append(line([(97, 47), (101, 36), (109, 31)], WHITE, 2.2))
    b.append(poly(hexpts(110.5, 30, 3), HONEY_L))
    b += wings(u, back=False, sweep=-2)
    if carry:
        b += cargo()
    wrap(f"p3{'-carry' if carry else ''}.svg", "hive courier P3 Node Chain" + (" carrying" if carry else ""), u, b, carry)


# ------------------------------------------------------------------ P4
def p4(carry):
    u = "4c" if carry else "4"
    b = []
    w = f' stroke="{WHITE}" stroke-opacity=".7" stroke-width="1.3" stroke-linejoin="round"'
    b.append(poly([(70, 48), (58, 49), (26, 36), (33, 29)], f"url(#w{u})", ' fill-opacity=".7"' + w))
    top = [(116, 58), (98, 48), (72, 44), (48, 50), (30, 58), (20, 64)]
    bot = [(20, 64), (30, 70), (52, 74), (78, 74), (98, 68), (116, 58)]
    mid = lambda x: 58 + (116 - x) / 108 * 6 + (2 if 30 < x < 100 else 0)
    body = top + bot[1:-1]
    b += clamp(carry, col=STONE, y=73, cy=93)
    segs = [(8, 28), (30.5, 47), (49, 70), (72, 96), (98, 117)]
    for i, (xa, xb) in enumerate(segs):
        s = clip(clip(body, -1, 0, -xa), 1, 0, xb)
        ym = mid((xa + xb) / 2)
        cols = (WHITE, STONE_D) if i in (0, 1, 2) else (WHITE, STONE)
        if i == 3:
            cols = (GRAPH[0], GRAPH[2])
        b.append(poly(band(s, 0, ym - 3), cols[0]))
        b.append(poly(band(s, ym - 3, ym + 4), STONE if i != 3 else GRAPH[1]))
        b.append(poly(band(s, ym + 4, 200), cols[1]))
    # honey light seam running the length of the body
    seam = [(26, mid(26) + .5), (96, mid(96) + .5)]
    b.append(line(seam, HONEY_L, 1.6, ' opacity=".85"'))
    # segment joints glow honey: light strips instead of stripes
    for xa, xb in ((28, 30.5), (47, 49), (70, 72)):
        j = clip(clip(body, -1, 0, -xa), 1, 0, xb)
        b.append(glow(j, u, HONEY_L, ".6"))
        b.append(poly(band(j, 0, mid(xa)), HONEY_L))
        b.append(poly(band(j, mid(xa), 200), HONEY))
    # tail thruster chevrons
    for k, op in ((0, "1"), (5.5, ".45")):
        b.append(line([(17 - k, 60.5), (13 - k, 64), (17 - k, 67.5)], HONEY_L, 2.2, f' opacity="{op}"'))
    # visor at nose
    vis = [(98, 53), (113, 57.5), (110, 61), (97, 58.5)]
    b.append(poly(vis, INK))
    sl = [(101, 55), (110, 57.6), (109, 59), (100.5, 57)]
    b += [glow(sl, u), poly(sl, HONEY_L)]
    b.append(poly([(86, 46), (72, 46), (46, 14), (60, 10)], f"url(#w{u})", w))
    b.append(poly([(86, 46), (60, 10), (53, 12)], WHITE, ' fill-opacity=".22"'))
    if carry:
        b += cargo(cx=79, cy=93)
    wrap(f"p4{'-carry' if carry else ''}.svg", "hive courier P4 Dart" + (" carrying" if carry else ""), u, b, carry)


# ------------------------------------------------------------------ P5
def p5(carry):
    u = "5c" if carry else "5"
    b = []
    w = f' stroke="{WHITE}" stroke-opacity=".75" stroke-width="1.6" stroke-linejoin="round"'
    b += clamp(carry, x0=72, x1=82, y=73, col=STONE, cx=77, cy=93)
    # abdomen: one long pointed hex, honey, light strips as negative space
    ab = [(6, 66), (20, 54), (60, 54), (66, 64), (60, 74), (20, 74)]
    parts = [(0, 30), (34, 44), (48, 70)]
    for xa, xb in parts:
        s = clip(clip(ab, -1, 0, -xa), 1, 0, xb)
        b.append(poly(band(s, 0, 64), HONEY_L))
        b.append(poly(band(s, 64, 200), HONEY))
    # head-thorax: one stone hex with visor
    hd = hexpts(88, 58, 22, flat=True, sy=.78)
    b.append(poly(band(hd, 0, 58), WHITE))
    b.append(poly(band(hd, 58, 200), STONE))
    vis = clip(band(hd, 51, 61), -1, 0, -92)
    sl = clip(clip(band(hd, 53.5, 57), -1, 0, -96), 1, 0, 105)
    b += [poly(vis, INK), glow(sl, u), poly(sl, HONEY_L)]
    # single light-plane wing
    b.append(poly([(86, 42), (70, 43), (44, 10), (60, 6)], f"url(#w{u})", w))
    if carry:
        b += cargo(cx=77, cy=93)
    wrap(f"p5{'-carry' if carry else ''}.svg", "hive courier P5 Pictogram" + (" carrying" if carry else ""), u, b, carry)


if __name__ == "__main__":
    for fn in (p1, p2, p3, p4, p5):
        fn(False); fn(True)
    print("ok")
