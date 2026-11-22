from gen import *


def glass(id_, x1, y1, x2, y2, a=.5, b=.06):
    return (f'<defs><linearGradient id="{id_}" gradientUnits="userSpaceOnUse" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{WHITE}" stop-opacity="{a}"/>'
            f'<stop offset="1" stop-color="{WHITE}" stop-opacity="{b}"/></linearGradient></defs>')


def pane(cell, gid, sw=2.4, facet=(4, 5, 0), fo=".38"):
    cx = sum(p[0] for p in cell) / 6
    cy = sum(p[1] for p in cell) / 6
    return [poly(cell, f"url(#{gid})", f' stroke="{WHITE}" stroke-width="{sw}" stroke-linejoin="round"'),
            poly([(cx, cy)] + [cell[i] for i in facet], WHITE, f' fill-opacity="{fo}"')]


def h1():
    b = ['<!-- H1 Cellwork: honeycomb-cell wings, hex head, segmented hex abdomen -->',
         glass("g1", 0, 26, 0, 80, .42, .04), '<g transform="translate(0 5)">']
    for m in (False, True):
        f = mirror if m else (lambda p: p)
        up = f(hexpts(31, 42, 16, flat=True))
        lo = f(hexpts(33.5, 67.5, 10.5, flat=True))
        fac = (3, 4, 5) if not m else (0, 5, 4)
        b += pane(up, "g1", facet=fac)
        b += pane(lo, "g1", facet=fac, fo=".28")
    for m in (False, True):
        f = mirror if m else (lambda p: p)
        b.append(line(f([(58, 20), (54, 12), (49, 8)]), WHITE, 2.4))
        b.append(poly(f(hexpts(46.5, 6.5, 3.6)), HONEY_L))
    head = hexpts(64, 34, 17, flat=True)
    b.append(poly(band(head, 0, 34), WHITE))
    b.append(poly(band(head, 34, 99), "#D8D0C2"))
    b += eye(57, 35, 5.6)
    b += eye(71, 35, 5.6)
    b.append(line([(61, 43), (64, 45), (67, 43)], INK, 1.6))
    ab = [(51, 53), (77, 53), (88, 69), (88, 87), (64, 114), (40, 87), (40, 69)]
    for y0, y1 in ((53, 66), (72, 84), (90, 115)):
        seg = band(ab, y0, y1)
        b.append(poly(half(seg, 64, True), HONEY_L))
        b.append(poly(half(seg, 64, False), HONEY))
    b.append('</g>')
    svg("h1.svg", "hive bee H1 Cellwork", b)


# ---------------------------------------------------------------- H2 Prism
def h2():
    b = ['<!-- H2 Prism: low-poly amber bee in profile, light-plane wings -->',
         '<g transform="translate(0 9)" stroke-linejoin="round">']
    tri = lambda pts, c: b.append(poly(pts, c))
    b.insert(1, glass("g2", 50, 10, 72, 52, .55, .05))
    w = f' stroke="{WHITE}" stroke-opacity=".6" stroke-width="1.3"'
    b.append(poly([(66, 52), (30, 38), (40, 24)], "url(#g2)", w))
    b.append(poly([(66, 52), (40, 24), (58, 22)], "url(#g2)", ' fill-opacity=".6"' + w))
    b.append(poly([(71, 50), (44, 18), (62, 7)], "url(#g2)", w))
    b.append(poly([(71, 50), (62, 7), (88, 25)], "url(#g2)", ' fill-opacity=".55"' + w))
    b.append(line([(92, 47), (95, 32), (103, 23)], HONEY_DD, 2.4))
    for pts in ([(65, 78), (61, 89), (56, 92)], [(72, 80), (72, 92), (67, 95)], [(79, 77), (85, 88), (83, 94)]):
        b.append(line(pts, HONEY_D, 2.4))
    S = [(63, 54, 66, 78), (51, 48, 65, 84), (37, 49, 67, 86), (24, 55, 70, 84)]
    tip = (11, 71)
    hon = [(HONEY_HI, HONEY_L), (HONEY, HONEY_M)]
    drk = [("#7A5212", "#664410"), ("#523710", "#3F2B0C")]
    pal = [hon, drk, hon, drk]
    for i in range(3):
        (xa, ta, ma, ba), (xb, tb, mb, bb) = S[i], S[i + 1]
        (u1, u2), (l1, l2) = pal[i]
        tri([(xa, ta), (xb, tb), (xb, mb)], u1)
        tri([(xa, ta), (xb, mb), (xa, ma)], u2)
        tri([(xa, ma), (xb, mb), (xa, ba)], l1)
        tri([(xa, ba), (xb, mb), (xb, bb)], l2)
    xa, ta, ma, ba = S[3]
    (u1, u2), (l1, l2) = pal[3]
    tri([(xa, ta), tip, (xa, ma)], u1)
    tri([(xa, ma), tip, (xa, ba)], l2)
    c = (73, 64)
    T = [(62, 54), (70, 47), (83, 50), (85, 64), (82, 77), (70, 80), (62, 78)]
    tc = [HONEY_D, HONEY_M, HONEY_D, "#7A5212", "#664410", "#523710", "#7A5212"]
    for i in range(7):
        tri([c, T[i], T[(i + 1) % 7]], tc[i])
    c = (96, 61)
    H = [(84, 47), (98, 43), (110, 52), (112, 66), (104, 77), (89, 78), (82, 63)]
    hc = [HONEY_HI, HONEY_L, HONEY, HONEY_M, HONEY_D, HONEY, HONEY_L]
    for i in range(7):
        tri([c, H[i], H[(i + 1) % 7]], hc[i])
    E = [(96, 49), (106, 53), (108, 62), (101, 67), (94, 61), (93, 53)]
    b.append(poly(E, INK))
    b.append(poly([(96, 50.5), (101, 52.5), (97.5, 56)], WHITE))
    b.append(poly([(103, 62), (106, 61), (102, 65)], INK2))
    b.append(line([(99, 44), (105, 29), (114, 22)], HONEY_L, 2.6))
    b.append(poly(hexpts(115.5, 21, 3.4), HONEY_L))
    b.append('</g>')
    svg("h2.svg", "hive bee H2 Prism", b)


# ---------------------------------------------------------------- H3 Voxel
class Scene:
    def __init__(self):
        self.items = []

    def poly(self, pts, fill, extra=""):
        self.items.append(("p", pts, fill, extra, 0))

    def line(self, pts, stroke, w, extra=""):
        self.items.append(("l", pts, stroke, extra, w))

    def render(self, size=128, pad=8, dy=0):
        xs = [x for it in self.items for x, _ in it[1]]
        ys = [y for it in self.items for _, y in it[1]]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        k = (size - 2 * pad) / max(x1 - x0, y1 - y0)
        ox = (size - (x1 - x0) * k) / 2 - x0 * k
        oy = (size - (y1 - y0) * k) / 2 - y0 * k + dy
        out = []
        for kind, pts, col, extra, w in self.items:
            t = [(x * k + ox, y * k + oy) for x, y in pts]
            out.append(poly(t, col, extra) if kind == "p" else line(t, col, w, extra))
        return out


def h3():
    W, H = 18, 18 * math.tan(math.radians(30))
    E = 2 * H

    def iso(x, y, z):
        return ((x - y) * W, (x + y) * H - z * E)

    sc = Scene()
    HONEYC = (HONEY_L, HONEY_M, HONEY_DD)
    WHITEC = (WHITE, STONE, STONE_D)
    g = 0.07

    def cube(x, y, z, s, cols, face=None):
        a, b2, c = x + g, y + g, z
        d = s - 2 * g
        top = [iso(a, b2, c + d), iso(a + d, b2, c + d), iso(a + d, b2 + d, c + d), iso(a, b2 + d, c + d)]
        left = [iso(a, b2 + d, c + d), iso(a + d, b2 + d, c + d), iso(a + d, b2 + d, c), iso(a, b2 + d, c)]
        right = [iso(a + d, b2, c + d), iso(a + d, b2 + d, c + d), iso(a + d, b2 + d, c), iso(a + d, b2, c)]
        sc.poly(top, cols[0]); sc.poly(left, cols[1]); sc.poly(right, cols[2])
        if face:
            face(a + d, b2, c, d)

    def wing(pts, op):
        sc.poly([iso(*p) for p in pts], "url(#g3)",
                f' fill-opacity="{op}" stroke="{WHITE}" stroke-opacity=".85" stroke-width="1.8" stroke-linejoin="round"')

    wing([(1.1, 0.25, 1.0), (1.9, 0.25, 1.0), (2.35, -0.55, 2.3), (2.0, -0.85, 2.95), (1.3, -0.65, 2.45)], ".8")
    tipp = iso(-0.75, 0.5, 0.42)
    sc.poly([tipp, iso(g, 1 - g, 0.93), iso(g, 1 - g, 0)], HONEY_M)
    sc.poly([tipp, iso(g, g, 0.93), iso(g, 1 - g, 0.93)], HONEY_L)
    cube(0, 0, 0, 1, HONEYC)
    cube(1, 0, 0, 1, WHITEC)

    def visor(fx, y0, z0, d):
        def f(y, z):
            return iso(fx, y0 + y * d, z0 + z * d)
        sc.poly([f(.1, .82), f(.9, .82), f(.9, .22), f(.1, .22)], INK)
        for yc in (.3, .7):
            sc.poly([f(yc - .12, .66), f(yc + .12, .66), f(yc + .12, .4), f(yc - .12, .4)], HONEY_L)

    for yy in (0.3, 0.75):
        a1 = iso(2.55, yy, 0.95)
        a2 = iso(2.95, yy - 0.15, 1.65)
        a3 = iso(3.35, yy - 0.15, 1.9)
        sc.line([a1, a2, a3], WHITE, 2.2)
        sc.poly(hexpts(a3[0], a3[1], 3.2), HONEY_L)
    cube(2, 0, 0, 1, HONEYC, visor)
    wing([(1.3, 0.7, 1.0), (1.95, 0.7, 1.0), (2.2, 0.1, 2.4), (1.85, -0.1, 3.0), (1.25, 0.1, 2.45)], "1")
    body = sc.render(pad=10)
    b = ['<!-- H3 Voxel: isometric cube-built bee, logo face shading -->', glass("g3", 0, 8, 0, 72, .55, .28)] + body
    svg("h3.svg", "hive bee H3 Voxel", b)


# ---------------------------------------------------------------- H4 Circuit
def h4():
    b = ['<!-- H4 Circuit: wireframe bee, trace legs, network-node antennae -->',
         '<g transform="translate(0 4)">']
    st = ' stroke-linejoin="miter"'
    for m in (False, True):
        f = mirror if m else (lambda p: p)
        up, lo = hexpts(31, 44, 15, flat=True), hexpts(33, 68, 9.5, flat=True)
        b.append(poly(f(up), WHITE, f' fill-opacity=".07" stroke="{WHITE}" stroke-width="3"{st}'))
        b.append(poly(f(hexpts(31, 44, 8, flat=True)), "none", f' stroke="{WHITE}" stroke-opacity=".35" stroke-width="1.6"'))
        b.append(poly(f(lo), WHITE, f' fill-opacity=".07" stroke="{WHITE}" stroke-width="3"{st}'))
        for pts in ([(42, 76), (34, 76), (28, 82), (28, 90)], [(43, 88), (38, 93), (38, 101)]):
            b.append(line(f(pts), HONEY_L, 2.2))
            e = pts[-1]
            b.append(poly(f([(e[0] - 2.6, e[1]), (e[0] + 2.6, e[1]), (e[0] + 2.6, e[1] + 4), (e[0] - 2.6, e[1] + 4)]), HONEY_L))
        b.append(line(f([(58, 22), (58, 15), (50, 7)]), HONEY_L, 2.4))
        b.append(poly(f(hexpts(48, 5, 4)), INK, f' stroke="{HONEY_L}" stroke-width="2.2"'))
        b.append(poly(f(hexpts(48, 5, 1.4)), HONEY_L))
    b.append(poly(hexpts(64, 36, 15, flat=True), INK, f' stroke="{HONEY_L}" stroke-width="4"{st}'))
    b += eye(57.5, 36, 4.6, fill=WHITE, gl=INK)
    b += eye(70.5, 36, 4.6, fill=WHITE, gl=INK)
    ab = [(52, 56), (76, 56), (86, 71), (86, 88), (64, 113), (42, 88), (42, 71)]
    b.append(poly(ab, INK, f' stroke="{HONEY_L}" stroke-width="4"{st}'))
    for y in (72, 87):
        b.append(poly(band(ab, y - 3, y + 3), HONEY_L))
    b.append(line([(64, 60), (64, 66)], HONEY_L, 2))
    b.append(line([(64, 78), (64, 81)], HONEY_L, 2))
    b.append(line([(64, 93), (64, 101)], HONEY_L, 2))
    for y, r in ((66, 2.2), (81, 2.2), (101, 2.4)):
        b.append(poly(hexpts(64, y, r), WHITE))
    b.append('</g>')
    svg("h4.svg", "hive bee H4 Circuit", b)


# ---------------------------------------------------------------- H5 Visor
def h5():
    b = ['<!-- H5 Visor: faceted helmet robot-bee, glowing hex eyes -->',
         '<g transform="translate(0 2)">']
    b.insert(1, glass("g5", 0, 30, 0, 96, .6, .06))
    w = f' stroke="{WHITE}" stroke-opacity=".75" stroke-width="1.5" stroke-linejoin="round"'
    for m in (False, True):
        f = mirror if m else (lambda p: p)
        b.append(poly(f([(38, 66), (6, 30), (3, 48), (26, 72)]), "url(#g5)", w))
        b.append(poly(f([(38, 66), (6, 30), (20, 30)]), "url(#g5)", ' fill-opacity=".5"' + w))
        b.append(poly(f([(38, 66), (6, 30), (12, 52)]), WHITE, ' fill-opacity=".16"'))
        b.append(poly(f([(40, 76), (14, 84), (20, 98), (36, 86)]), "url(#g5)", ' fill-opacity=".8"' + w))
        b.append(poly(f([(40, 76), (14, 84), (26, 86)]), WHITE, ' fill-opacity=".22"'))
        b.append(line(f([(52, 26), (46, 14), (40, 10)]), WHITE, 2.6))
        b.append(poly(f(hexpts(37, 8.5, 4.2)), HONEY_L))
    ab = [(48, 74), (80, 74), (87, 87), (64, 115), (41, 87)]
    for y0, y1 in ((74, 87), (92, 100), (105, 116)):
        seg = band(ab, y0, y1)
        b.append(poly(half(seg, 64, True), HONEY_L))
        b.append(poly(half(seg, 64, False), HONEY))
    hx = hexpts(64, 50, 30, flat=True, sx=1.08, sy=0.92)
    c = (64, 50)
    shades = ["#C9C1B3", "#B3AB9C", "#D8D0C2", "#E8E2D7", WHITE, "#E0D9CC"]
    for i in range(6):
        b.append(poly([c, hx[i], hx[(i + 1) % 6]], shades[i]))
    V = [(41, 42), (87, 42), (93, 52), (87, 62), (41, 62), (35, 52)]
    b.append(poly(V, INK))
    b.append(poly([(41, 42), (87, 42), (89, 45.5), (39, 45.5)], INK2))
    for x in (52.5, 75.5):
        e = hexpts(x, 52.5, 7.2, sy=1.05)
        b.append(poly(e, HONEY_L))
        b.append(poly(band(e, 52.5, 99), HONEY))
        b.append(poly(hexpts(x + 2.2, 49.6, 1.9), WHITE))
    b.append('</g>')
    svg("h5.svg", "hive bee H5 Visor", b)


# ---------------------------------------------------------------- H6 Glyph
def h6():
    b = ['<!-- H6 Glyph: three honeycomb cells - body/face cell + two wing cells -->',
         glass("g6", 0, 10, 0, 50, .6, .15), '<g transform="translate(0 7)">']
    cx, cy, rb, rw, gap = 64, 72, 34, 18, 4.5
    hx = hexpts(cx, cy, rb)
    d = rb * .866 + gap + rw * .866
    for ang in (240, 300):
        wx, wy = cx + d * math.cos(math.radians(ang)), cy + d * math.sin(math.radians(ang))
        cell = hexpts(wx, wy, rw)
        b += pane(cell, "g6", sw=3, facet=(4, 5, 0) if ang == 240 else (0, 1, 2), fo=".5")
    for m in (False, True):
        f = mirror if m else (lambda p: p)
        b.append(line(f([(60.5, 41), (58.5, 29)]), WHITE, 2.6))
        b.append(poly(f(hexpts(58.2, 26, 3.2)), HONEY_L))
    head = band(hx, 0, 77)
    b.append(poly(half(head, cx, True), HONEY_L))
    b.append(poly(half(head, cx, False), HONEY))
    for y0, y1 in ((83.5, 92), (98.5, 107)):
        seg = band(hx, y0, y1)
        b.append(poly(half(seg, cx, True), HONEY_L))
        b.append(poly(half(seg, cx, False), HONEY))
    b += eye(52.5, 61, 6.6)
    b += eye(75.5, 61, 6.6)
    b.append('</g>')
    svg("h6.svg", "hive bee H6 Glyph", b)


if __name__ == "__main__":
    for fn in (h1, h2, h3, h4, h5, h6):
        fn()
    print("ok")
