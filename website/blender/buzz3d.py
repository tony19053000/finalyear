"""hive. mascot "Buzz" (C1) as a real low-poly 3D model -> assets/models/buzz.glb + preview PNGs.

Run headless (Blender 5.3; 4.2 also works):
  blender -b --factory-startup --python buzz3d.py            # model + previews
  blender -b --factory-startup --python buzz3d.py -- --nopng # model only

Design follows the 2D C1 Buzz (assets/bee-v2/_src/gen4.py, take C1): every number below is in
the 2D's 128-unit SVG space (x right, y DOWN) and converted with S metres per unit.
Material follows the 3D comb in assets/js/home.js: hex prism cells R=0.5 (flat-to-flat 0.866),
MeshStandardMaterial metalness 0.5 / roughness 0.36, flat shading, honey emissive #F5A623/#FFC93C.

Model frame (glTF): Y up, +X forward (Buzz faces right), +Z = Buzz's right side (towards a
three.js camera at +Z), origin = centre of the body's bounding box, units = home.js units.
Both eyes are identical and mirror-symmetric (also the happy/think variants).
"""
import bpy, bmesh, math, os, sys
from mathutils import Vector, Quaternion

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.normpath(os.path.join(HERE, ".."))
OUT = os.path.join(SITE, "assets", "models")
os.makedirs(OUT, exist_ok=True)
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
NOPNG = "--nopng" in argv
ONLY = [a.split("=", 1)[1] for a in argv if a.startswith("--only=")]

S = 0.009            # metres per 2D unit -> head 0.43 long (0.50 of a cell's 0.866 width), body 0.86 long
CX, CY = 61.0, 56.5  # 2D point that becomes the model origin (centre of the body's bounding box)
C30 = math.cos(math.radians(30))

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene


def srgb(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


# ------------------------------------------------------------------ materials
MATS = {}


def mat(name, base, metal=0.5, rough=0.36, emit=None, strength=0.0, alpha=1.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*srgb(base), 1)
    b.inputs["Metallic"].default_value = metal
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*srgb(emit), 1)
        b.inputs["Emission Strength"].default_value = strength
    if alpha < 1:
        b.inputs["Alpha"].default_value = alpha
        for attr, val in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
            try:
                setattr(m, attr, val)
            except Exception:
                pass
        try:
            m.use_backface_culling = True
        except Exception:
            pass
    MATS[name] = m
    return m


# bronze shell: same metal/roughness as the comb; the facet tone tiers follow the 2D bands
mat("BronzeHi", "#433A1F")    # upward facets (lit tops, ~#3A3520-#5A4E2A)
mat("BronzeMid", "#211D10")   # side facets
mat("BronzeLo", "#100E08")    # undersides (near black)
mat("Joint", "#6A5C30", 0.6, 0.32)
mat("Visor", "#070605", 0.35, 0.10)
mat("Honey", "#F5A623", 0.0, 0.5, "#F5A623", 1.6)        # eye body, nodes
mat("HoneyGlow", "#FFE27A", 0.0, 0.5, "#FFE27A", 2.4)    # seams, eye glints, tips
mat("HoneyRim", "#F5A623", 0.0, 0.5, "#F5A623", 0.8)     # light leaking around the visor
mat("Job", "#FFC93C", 0.3, 0.35, "#F5A623", 0.75)        # = home.js chunkMat
mat("JobCore", "#FFF0B8", 0.0, 0.4, "#FFE9A0", 1.4)
mat("Flame", "#FFC93C", 0.0, 0.5, "#FFC93C", 2.2, alpha=0.55)
mat("FlameCore", "#FFE27A", 0.0, 0.5, "#FFF0B8", 3.5)


# ------------------------------------------------------------------ geometry helpers (2D units)
def conv(p):
    """2D-space point (x right, y down, z = Buzz's right) -> Blender (X fwd, Z up, -Y = right)."""
    x, y, z = p
    return Vector(((x - CX) * S, -z * S, (CY - y) * S))


class MB:
    """Mesh builder: parts in 2D units, materials per face ('AUTO' = bronze tier by facet normal)."""

    def __init__(self):
        self.v, self.f, self.m = [], [], []

    def add(self, part, m):
        verts, faces = part
        b = len(self.v)
        self.v += list(verts)
        self.f += [[b + i for i in f] for f in faces]
        self.m += [m] * len(faces)
        return self

    def obj(self, name, pivot=(CX, CY, 0), parent=None, merge=True):
        me = bpy.data.meshes.new(name)
        bm = bmesh.new()
        pv = conv(pivot)
        vs = [bm.verts.new(conv(p) - pv) for p in self.v]
        slots = []
        for f, m in zip(self.f, self.m):
            try:
                face = bm.faces.new([vs[i] for i in f])
            except ValueError:
                continue
            face.smooth = False
            key = m
            if key not in slots:
                slots.append(key)
            face.material_index = slots.index(key)
        if merge:
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        # resolve AUTO -> bronze tier by the (Blender, Z-up) facet normal
        final = []
        for face in bm.faces:
            key = slots[face.material_index]
            if key == "AUTO":
                nz = face.normal.z
                key = "BronzeHi" if nz > 0.45 else ("BronzeLo" if nz < -0.4 else "BronzeMid")
            if key not in final:
                final.append(key)
            face.material_index = final.index(key)
        bm.to_mesh(me)
        bm.free()
        for k in final:
            me.materials.append(MATS[k])
        ob = bpy.data.objects.new(name, me)
        sc.collection.objects.link(ob)
        place(ob, pv, parent)
        return ob


def place(ob, world_loc, parent=None):
    if parent is not None:
        ob.parent = parent
        ob.location = world_loc - parent.matrix_world.translation
    else:
        ob.location = world_loc
    bpy.context.view_layer.update()


def empty(name, pivot, parent=None):
    ob = bpy.data.objects.new(name, None)
    ob.empty_display_size = 0.05
    sc.collection.objects.link(ob)
    place(ob, conv(pivot), parent)
    return ob


def loft(rings, cap0=True, cap1=True):
    n = len(rings[0])
    verts = [p for r in rings for p in r]
    faces = []
    for i in range(len(rings) - 1):
        a, b = i * n, (i + 1) * n
        for k in range(n):
            k2 = (k + 1) % n
            faces.append([a + k, a + k2, b + k2, b + k])
    if cap0:
        faces.append(list(range(n))[::-1])
    if cap1:
        faces.append(list(range((len(rings) - 1) * n, len(rings) * n)))
    return verts, faces


def cone(ring, tip):
    n = len(ring)
    verts = list(ring) + [tip]
    faces = [[k, (k + 1) % n, n] for k in range(n)] + [list(range(n))[::-1]]
    return verts, faces


def hex_yz(x, yc, R, zs=1.0, zc=0.0):
    """Hex ring across the body axis (flat top/bottom, corners at the sides)."""
    return [(x, yc + R * math.sin(math.radians(a)), zc + R * math.cos(math.radians(a)) * zs) for a in range(0, 360, 60)]


def hex_xz(xc, yc, R, shift=0.0, zc=0.0, rot=0.0):
    """Horizontal hex ring (a comb cell's outline); rot=0 -> a corner points forward (+x)."""
    return [(xc + shift + R * math.cos(math.radians(a + rot)), yc, zc + R * math.sin(math.radians(a + rot))) for a in range(0, 360, 60)]


def hex_xy(xc, yc, z, R, rot=30.0):
    """Hex ring facing the side camera (axis = z); rot=30 -> pointy top, rot=0 -> flat top."""
    return [(xc + R * math.cos(math.radians(a + rot)), yc + R * math.sin(math.radians(a + rot)), z) for a in range(0, 360, 60)]


def gem_z(xc, yc, zc, R, t, rot=30.0, k=0.68):
    """Small chamfered hex prism with its axis across the body (joints, nodes, hands)."""
    c = t * 0.3
    return loft([hex_xy(xc, yc, zc - t / 2, R * k, rot), hex_xy(xc, yc, zc - t / 2 + c, R, rot),
                 hex_xy(xc, yc, zc + t / 2 - c, R, rot), hex_xy(xc, yc, zc + t / 2, R * k, rot)])


def gem_x(xc, yc, R, L, zc=0.0, k=0.62):
    c = L * 0.3
    return loft([hex_yz(xc - L / 2, yc, R * k, zc=zc), hex_yz(xc - L / 2 + c, yc, R, zc=zc),
                 hex_yz(xc + L / 2 - c, yc, R, zc=zc), hex_yz(xc + L / 2, yc, R * k, zc=zc)])


def basis(d):
    d = d.normalized()
    up = Vector((0, 0, 1)) if abs(d.z) < 0.9 else Vector((1, 0, 0))
    u = d.cross(up).normalized()
    return u, d.cross(u).normalized()


def rod(p0, p1, r, rot=0.0):
    a, b = Vector(p0), Vector(p1)
    u, v = basis(b - a)
    def ring(c):
        return [tuple(c + r * (math.cos(math.radians(t + rot)) * u + math.sin(math.radians(t + rot)) * v)) for t in range(0, 360, 60)]
    return loft([ring(a), ring(b)])


def hex2d(cx, cy, r, sy=1.0, flat=False):
    """2D hex in (s, y-down) face space, matching gen.py hexpts()."""
    off = 0 if flat else 30
    return [(cx + r * math.cos(math.radians(a + off)), cy + r * math.sin(math.radians(a + off)) * sy) for a in range(0, 360, 60)]


def stroke2d(pts, w):
    """Polyline -> closed outline (mitred), width w."""
    def nrm(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        l = math.hypot(dx, dy)
        return (-dy / l, dx / l)
    L, Rr = [], []
    n = len(pts)
    for i, p in enumerate(pts):
        if i == 0:
            nx, ny = nrm(pts[0], pts[1]); k = 1
        elif i == n - 1:
            nx, ny = nrm(pts[-2], pts[-1]); k = 1
        else:
            a, b = nrm(pts[i - 1], p), nrm(p, pts[i + 1])
            nx, ny = a[0] + b[0], a[1] + b[1]
            l = math.hypot(nx, ny); nx, ny = nx / l, ny / l
            k = 1 / max(0.35, nx * a[0] + ny * a[1])
        L.append((p[0] + nx * w / 2 * k, p[1] + ny * w / 2 * k))
        Rr.append((p[0] - nx * w / 2 * k, p[1] - ny * w / 2 * k))
    return L + Rr[::-1]


def clip_s(poly, sign):
    """Keep the part of a 2D polygon with sign*s >= 0 (Sutherland-Hodgman on s = 0)."""
    out = []
    for i in range(len(poly)):
        a, b = poly[i - 1], poly[i]
        ia, ib = sign * a[0] >= -1e-9, sign * b[0] >= -1e-9
        if ib:
            if not ia:
                t = a[0] / (a[0] - b[0]); out.append((0.0, a[1] + t * (b[1] - a[1])))
            out.append(b)
        elif ia:
            t = a[0] / (a[0] - b[0]); out.append((0.0, a[1] + t * (b[1] - a[1])))
    return out


# ------------------------------------------------------------------ the head (a comb cell, corner forward)
HX, HY, HR, HH = 84.0, 48.0, 24.0, 19.0     # 2D head centre, hex radius (half length), half height
NECK = (HX - 0.3 * HR, HY + HH, 0.0)          # head pivot (gen4 tilts the head about this point)


def face_pt(s, y, d):
    """Point on the head's two front faces (s>0: right face, s<0: left face), d = outward offset."""
    side = 1.0 if s >= 0 else -1.0
    a = abs(s)
    d += 0.45  # clears the mid-height ridge facet
    return (HX + HR + d / C30 - 0.5 * a, y, side * C30 * a)


def face_slab(poly, d0, d1):
    """Extrude a 2D outline (s, y) lying on the head's front faces from offset d0 to d1."""
    verts, faces = [], []
    for sign in (1, -1):
        h = clip_s(poly, sign)
        if len(h) < 3:
            continue
        n = len(h)
        b = len(verts)
        verts += [face_pt(s, y, d1) for s, y in h] + [face_pt(s, y, d0) for s, y in h]
        faces.append(list(range(b, b + n)))
        faces.append(list(range(b + n, b + 2 * n))[::-1])
        for i in range(n):
            j = (i + 1) % n
            if abs(h[i][0]) < 1e-7 and abs(h[j][0]) < 1e-7:
                continue  # the crease: the other half continues here
            faces.append([b + i, b + n + i, b + n + j, b + j])
    return verts, faces


def build_head(root):
    mb = MB()
    # side profile = the 2D head's chamfered octagon; two-step chamfers = premium facets
    rings = [(HY - HH, 0.60, -1.2), (HY - HH + 2.4, 0.83, -0.5), (HY - HH + 7.0, 0.985, 0.0), (HY - 1.0, 1.02, 0.0),
             (HY + HH - 6.0, 0.985, 0.0), (HY + HH - 2.0, 0.85, -0.3), (HY + HH, 0.66, -0.6)]
    mb.add(loft([hex_xz(HX, y, HR * k, shift) for y, k, shift in rings]), "AUTO")
    head = mb.obj("Head", NECK, root)

    # visor: dark glass across both front faces, honey light leaking round its edge
    V = [(-21, 41.5), (-18.5, 37.6), (18.5, 37.6), (21, 41.5), (21, 56.5), (18, 59.6), (-18, 59.6), (-21, 56.5)]
    def grow(poly, g):
        return [(s * (1 + g / 21), HY + (y - HY) * (1 + g / 11)) for s, y in poly]
    vb = MB().add(face_slab(V, 0.0, 0.9), "Visor").add(face_slab(grow(V, 0.55), 0.3, 0.6), "HoneyRim")
    vb.obj("Visor", (HX + HR, HY, 0), head)

    EY, ES, EX = 47.4, 5.6, 11.5     # eye centre height, eye size (gen4 C1 eye=5.6), distance from the nose crease
    MY = HY + 0.42 * HH              # mouth height

    def eye_neutral(c):
        b = MB()
        b.add(face_slab(hex2d(c, EY, ES, 1.36), 0.75, 1.30), "Honey")
        b.add(face_slab(hex2d(c, EY - ES * 0.1, ES * 0.58, 1.36), 1.2, 1.55), "HoneyGlow")
        return b

    def eye_happy(c):
        w, h = ES * 0.72, ES * 1.0
        pts = [(c - w, EY + h * 0.5), (c, EY - h * 0.55), (c + w, EY + h * 0.5)]
        return MB().add(face_slab(stroke2d(pts, 3.4), 0.75, 1.15), "Honey").add(face_slab(stroke2d(pts, 2.2), 1.1, 1.5), "HoneyGlow")

    def eye_think(c):
        # both eyes narrowed to a flat slit, lifted a touch: "hmm..."
        return (MB().add(face_slab(hex2d(c, EY + ES * 0.05, ES * 1.0, 0.34, flat=True), 0.75, 1.2), "Honey")
                .add(face_slab(hex2d(c, EY + ES * 0.05, ES * 0.62, 0.22, flat=True), 1.1, 1.5), "HoneyGlow"))

    eyes = {}
    for nm, fn, hide in (("", eye_neutral, False), ("_Happy", eye_happy, True), ("_Think", eye_think, True)):
        for side, sgn in (("R", 1), ("L", -1)):
            c = sgn * EX
            o = fn(c).obj(f"Eye{side}{nm}", face_pt(c, EY, 1.0), head)
            o["variant"] = nm.strip("_").lower() or "neutral"
            eyes[f"Eye{side}{nm}"] = o

    mouths = {}
    mpts = {"": stroke2d([(-3.0, MY - 0.6), (0, MY + 1.2), (3.0, MY - 0.6)], 1.5),
            "_Happy": [(-4.0, MY - 1.4), (4.0, MY - 1.4), (2.1, MY + 2.2), (-2.1, MY + 2.2)],
            "_Think": stroke2d([(-2.4, MY + 0.8), (2.4, MY - 0.4)], 1.4)}
    for nm, poly in mpts.items():
        o = MB().add(face_slab(poly, 0.7, 1.25), "HoneyGlow").obj(f"Mouth{nm}", face_pt(0, MY, 1.0), head)
        o["variant"] = nm.strip("_").lower() or "neutral"
        mouths[f"Mouth{nm}"] = o

    # antenna: bronze hex rod, bent back, honey hex nodes (gen4 C1 antenna points)
    def rel(fx, fy):
        return (HX + fx * HR, HY + fy * HH, 0.0)
    a0, a1, a2 = rel(-0.35, -0.97), rel(-0.55, -1.55), rel(-0.78, -1.72)
    ab = MB()
    ab.add(loft([hex_xz(a0[0], a0[1] + 1.0, 2.4, rot=30), hex_xz(a0[0], a0[1] - 1.0, 2.0, rot=30)]), "AUTO")  # collar
    ab.add(rod(a0, a1, 0.95), "AUTO").add(rod(a1, a2, 0.85), "AUTO")
    ab.add(gem_z(a1[0], a1[1], 0, 1.7, 2.4), "Honey")
    ab.add(gem_z(a2[0], a2[1], 0, 2.7, 3.0), "HoneyGlow")
    ab.obj("Antenna", a0, head)
    return head, eyes, mouths


# ------------------------------------------------------------------ body: thorax + 2 abdomen prisms, honey seams
MODS = [  # x0, x1, cy, half-height (gen4 prism uses h*1.14)
    (25.6, 35.4, 70.0, 7.5 * 1.14),
    (36.6, 50.0, 67.0, 10 * 1.14),
    (51.0, 69.0, 64.0, 11 * 1.14),
]
ZS = 0.9  # body cross-section a little narrower than tall-ish


def build_body(root):
    mb = MB()
    for x0, x1, cy, h in MODS:
        R = h / C30
        c = min(3.0, (x1 - x0) * 0.22)
        mid = (x0 + x1) / 2
        mb.add(loft([hex_yz(x0, cy, R * 0.74, ZS), hex_yz(x0 + c, cy, R * 0.97, ZS), hex_yz(mid, cy, R * 1.03, ZS),
                     hex_yz(x1 - c, cy, R * 0.97, ZS), hex_yz(x1, cy, R * 0.74, ZS)]), "AUTO")
    # seams: honey light from inside, between the modules (and at the neck)
    for (a, b) in ((MODS[0], MODS[1]), (MODS[1], MODS[2])):
        x = (a[1] + b[0]) / 2
        R = min(a[3], b[3]) / C30 * 0.86
        cy = (a[2] + b[2]) / 2
        mb.add(loft([hex_yz(x - 0.9, cy, R, ZS), hex_yz(x + 0.9, cy, R, ZS)]), "HoneyGlow")
    t = MODS[2]
    mb.add(loft([hex_yz(t[1] - 0.2, t[2] + 3.0, t[3] / C30 * 0.5, ZS), hex_yz(t[1] + 0.7, t[2] + 3.0, t[3] / C30 * 0.5, ZS)]), "Honey")
    # stinger: dark hex spike + honey emitter at the tip
    sx, sy = 25.8, 70.0
    tip = (14.0, 71.4, 0.0)
    mb.add(cone(hex_yz(sx, sy, 4.0), tip), "AUTO")
    mb.add(gem_x(12.9, 71.5, 1.9, 2.6), "HoneyGlow")
    return mb.obj("Body", (CX, CY, 0), root)


# ------------------------------------------------------------------ jets (no wings): faceted pods + honey flame
JET = dict(x0=48.5, x1=65.5, cy=79.5, h=4.4, z=9.2, tilt=16.0)
FLAME_DIR = math.radians(-42)  # below horizontal, pointing back


def rot2(p, c, ang):
    """Rotate (x, y-down) about c by ang degrees (positive = nose up on screen)."""
    a = math.radians(ang)
    dx, dy = p[0] - c[0], p[1] - c[1]
    return (c[0] + dx * math.cos(a) + dy * math.sin(a), c[1] - dx * math.sin(a) + dy * math.cos(a))


def build_jets(root):
    J = JET
    R = J["h"] / C30
    cx, cy = (J["x0"] + J["x1"]) / 2, J["cy"]
    jets, flames = [], []
    fl_root = empty("Flame", (CX, CY, 0), root)
    for side, sgn in (("R", 1), ("L", -1)):
        z = sgn * J["z"]
        rings = []
        for x, k in ((J["x0"] - 0.6, 0.70), (J["x0"] + 1.0, 0.94), (J["x0"] + 3.5, 1.0), (J["x1"] - 3.0, 1.0), (J["x1"], 0.52)):
            ring = []
            for (px, py, pz) in hex_yz(x, cy, R * k, 1.0, z):
                qx, qy = rot2((px, py), (cx, cy), J["tilt"])
                ring.append((qx, qy, pz))
            rings.append(ring)
        b = MB().add(loft(rings), "AUTO")
        # nozzle: dark mouth with honey light inside
        nz = rot2((J["x0"] - 0.7, cy), (cx, cy), J["tilt"])
        nz_in = rot2((J["x0"] + 0.4, cy), (cx, cy), J["tilt"])
        ax = Vector((nz[0] - nz_in[0], nz[1] - nz_in[1], 0)).normalized()
        b.add(rod((nz[0] - ax.x * 0.2, nz[1] - ax.y * 0.2, z), (nz[0] + ax.x * 0.5, nz[1] + ax.y * 0.5, z), R * 0.5, rot=90), "HoneyGlow")
        # pylon up into the thorax
        p_top = (57.5, 70.5, sgn * 6.0)
        p_bot = rot2((57.0, cy - J["h"] * 0.6), (cx, cy), J["tilt"])
        b.add(rod(p_top, (p_bot[0], p_bot[1], z * 0.98), 1.1), "AUTO")
        jets.append(b.obj(f"Jet{side}", (cx, cy, z), root))

        # flame: faceted honey cone + bright core, origin at the nozzle so the site can scale/flicker it
        origin = (nz[0] + ax.x * 0.6, nz[1] + ax.y * 0.6, z)
        d = Vector((-math.cos(FLAME_DIR), -math.sin(FLAME_DIR), 0))  # back and down (2D y-down => +y)
        o = Vector(origin)
        u, v = basis(d)
        def ring(c, r):
            return [tuple(c + r * (math.cos(math.radians(t)) * u + math.sin(math.radians(t)) * v)) for t in range(0, 360, 60)]
        fm = MB()
        fm.add(cone_pair(o, d, R, ring), "Flame")
        fm.add(cone(ring(o + d * 0.3, R * 0.42), tuple(o + d * 11.0)), "FlameCore")
        flames.append(fm.obj(f"Flame{side}", origin, fl_root))
    return jets, fl_root, flames


def cone_pair(o, d, R, ring):
    """Outer flame: short flared collar then a long faceted taper."""
    r0, r1 = ring(o, R * 0.6), ring(o + d * 4.5, R * 0.82)
    tip = tuple(o + d * 22.0)
    verts = r0 + r1 + [tip]
    faces = []
    for k in range(6):
        k2 = (k + 1) % 6
        faces.append([k, k2, 6 + k2, 6 + k])
        faces.append([6 + k, 6 + k2, 12])
    faces.append(list(range(6))[::-1])
    return verts, faces


# ------------------------------------------------------------------ arms: short bronze hex rods, hex joints
SHOULDER = (66.0, 68.5, 12.4)
ELBOW = (70.0, 76.8, 13.4)
HAND = (79.0, 79.6, 12.8)
JOB = dict(x=80.0, y=76.8, R=7.4, h=9.0)


def build_arm(root, sgn, name):
    sh = (SHOULDER[0], SHOULDER[1], sgn * SHOULDER[2])
    el = (ELBOW[0], ELBOW[1], sgn * ELBOW[2])
    hd = (HAND[0], HAND[1], sgn * HAND[2])
    b = MB()
    b.add(gem_z(sh[0], sh[1], sh[2], 2.5, 2.6, rot=0), "Joint")
    b.add(rod(sh, el, 1.45), "AUTO").add(rod(el, hd, 1.3), "AUTO")
    b.add(gem_z(el[0], el[1], el[2], 2.0, 3.2), "Joint")
    b.add(gem_z(hd[0], hd[1], hd[2], 3.0, 2.4, rot=0, k=0.75), "AUTO")
    b.add(gem_z(hd[0] + 0.6, hd[1] - 0.6, hd[2] + sgn * 1.25, 1.15, 0.5, rot=0, k=0.8), "HoneyGlow")
    ob = b.obj(name, sh, root)
    # carry pose: rigid rotation about the shoulder that brings the hand to the side of the Job cell
    target = (JOB["x"], JOB["y"], sgn * (JOB["R"] * C30 + 1.5))
    v0 = conv(hd) - conv(sh)
    v1 = conv(target) - conv(sh)
    q = v0.rotation_difference(v1)
    ob["carry_quat_gltf"] = [q.x, q.z, -q.y, q.w]   # Blender -> glTF axes (x, z, -y)
    ob["carry_quat_blender"] = [q.w, q.x, q.y, q.z]
    return ob, q


def build_job(root):
    j = JOB
    b = MB()
    y0, y1 = j["y"] - j["h"] / 2, j["y"] + j["h"] / 2
    b.add(loft([hex_xz(j["x"], y1, j["R"] * 0.86), hex_xz(j["x"], y1 - 1.0, j["R"]),
                hex_xz(j["x"], y0 + 1.0, j["R"]), hex_xz(j["x"], y0, j["R"] * 0.86)]), "Job")
    b.add(loft([hex_xz(j["x"], y0 + 0.3, j["R"] * 0.55), hex_xz(j["x"], y0 - 0.35, j["R"] * 0.55)]), "JobCore")
    return b.obj("Job", (j["x"], j["y"], 0), root)


# ------------------------------------------------------------------ assemble
root = empty("Buzz", (CX, CY, 0))
body = build_body(root)
head, eyes, mouths = build_head(root)
jets, flame, flames = build_jets(root)
armR, qR = build_arm(root, 1, "ArmR")
armL, qL = build_arm(root, -1, "ArmL")
job = build_job(root)
root["scale_note"] = "home.js units: comb cell R=0.5 (flat-to-flat 0.866); Buzz length ~0.86"
head["variants"] = "EyeL/EyeR/Mouth = neutral; *_Happy, *_Think are alternates (hide on load)"
for o in sc.objects:
    if o.type == "MESH":
        o.data.name = o.name

# report extents (glTF frame)
bpy.context.view_layer.update()
mn = Vector((1e9,) * 3); mx = Vector((-1e9,) * 3)
for o in sc.objects:
    if o.type != "MESH" or o.name.startswith("Flame") or o.name == "Job" or "_" in o.name:
        continue
    for v in o.data.vertices:
        w = o.matrix_world @ v.co
        mn = Vector(map(min, mn, w)); mx = Vector(map(max, mx, w))
print("EXTENT (blender m) min", tuple(round(c, 3) for c in mn), "max", tuple(round(c, 3) for c in mx))
print("SIZE (L x W x H)", round(mx.x - mn.x, 3), round(mx.y - mn.y, 3), round(mx.z - mn.z, 3))
hb = [head.matrix_world @ v.co for v in head.data.vertices]
print("HEAD length", round(max(p.x for p in hb) - min(p.x for p in hb), 3), "width", round(max(p.y for p in hb) - min(p.y for p in hb), 3))

# ------------------------------------------------------------------ export
GLB = os.path.join(OUT, "buzz.glb")
for o in sc.objects:
    o.select_set(True)
kw = dict(filepath=GLB, export_format="GLB", use_selection=False, export_yup=True, export_apply=True,
          export_extras=True, export_materials="EXPORT", export_animations=False, export_cameras=False,
          export_lights=False, export_texcoords=False, export_normals=True)
props = {p.identifier for p in bpy.ops.export_scene.gltf.get_rna_type().properties}
bpy.ops.export_scene.gltf(**{k: v for k, v in kw.items() if k in props})
print("GLB", GLB, os.path.getsize(GLB), "bytes")

if NOPNG:
    raise SystemExit

# ------------------------------------------------------------------ previews (lights = home.js rig)
engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
for attr, val in (("taa_render_samples", 96), ("use_shadows", True), ("use_raytracing", True), ("shadow_ray_count", 2)):
    try:
        setattr(sc.eevee, attr, val)
    except Exception:
        pass
sc.render.resolution_x, sc.render.resolution_y = 1400, 1000
sc.view_settings.view_transform = "Standard"
sc.view_settings.look = "None"

world = bpy.data.worlds.new("w"); sc.world = world; world.use_nodes = True
nt = world.node_tree; nt.nodes.clear()
outn = nt.nodes.new("ShaderNodeOutputWorld")
mix = nt.nodes.new("ShaderNodeMixShader")
lp = nt.nodes.new("ShaderNodeLightPath")
amb = nt.nodes.new("ShaderNodeBackground"); amb.inputs[0].default_value = (*srgb("#C9C2B4"), 1); amb.inputs[1].default_value = 0.2
bgc = nt.nodes.new("ShaderNodeBackground"); bgc.inputs[0].default_value = (*srgb("#0B0A08"), 1); bgc.inputs[1].default_value = 1.0
nt.links.new(lp.outputs["Is Camera Ray"], mix.inputs[0])
nt.links.new(amb.outputs[0], mix.inputs[1]); nt.links.new(bgc.outputs[0], mix.inputs[2])
nt.links.new(mix.outputs[0], outn.inputs[0])


def sun(name, three_pos, color, strength, angle=4.0):
    L = bpy.data.lights.new(name, "SUN"); L.color = srgb(color); L.energy = strength
    L.angle = math.radians(angle)
    o = bpy.data.objects.new(name, L); sc.collection.objects.link(o)
    x, y, z = three_pos
    d = Vector((x, -z, y)).normalized()            # three.js (Y up) -> Blender (Z up)
    o.rotation_euler = d.to_track_quat("Z", "Y").to_euler()
    return o


sun("Key", (-7, 9, 9), "#FFE9C2", 3.4)
sun("Rim", (9, -3, -7), "#F5A623", 4.5)
sun("Fill", (6, 2, 10), "#9AA6B8", 0.6)
sun("Top", (2, 10, 3), "#F2EDE4", 0.45, 20)

# bloom in the compositor (the site gets its glow from emissive + additive sprites)
try:
    if hasattr(sc, "compositing_node_group"):
        ng = bpy.data.node_groups.new("comp", "CompositorNodeTree")
        sc.compositing_node_group = ng
        rl = ng.nodes.new("CompositorNodeRLayers")
        gl = ng.nodes.new("CompositorNodeGlare")
        go = ng.nodes.new("NodeGroupOutput")
        ng.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        gl.inputs["Type"].default_value = "Bloom"
        gl.inputs["Quality"].default_value = "High"
        gl.inputs["Threshold"].default_value = 0.9
        gl.inputs["Strength"].default_value = 0.55
        gl.inputs["Size"].default_value = 0.55
        ng.links.new(rl.outputs["Image"], gl.inputs["Image"])
        ng.links.new(gl.outputs["Image"], go.inputs[0])
    else:
        sc.use_nodes = True
        t = sc.node_tree
        rl = t.nodes["Render Layers"]; cp = t.nodes["Composite"]
        gl = t.nodes.new("CompositorNodeGlare"); gl.glare_type = "FOG_GLOW"; gl.threshold = 0.9; gl.size = 7; gl.mix = -0.5
        t.links.new(rl.outputs["Image"], gl.inputs["Image"]); t.links.new(gl.outputs["Image"], cp.inputs["Image"])
except Exception as e:
    print("bloom setup failed:", e)

cam_data = bpy.data.cameras.new("cam"); cam = bpy.data.objects.new("cam", cam_data); sc.collection.objects.link(cam)
sc.camera = cam
tgt = bpy.data.objects.new("tgt", None); sc.collection.objects.link(tgt)
tc = cam.constraints.new("TRACK_TO"); tc.target = tgt; tc.track_axis = "TRACK_NEGATIVE_Z"; tc.up_axis = "UP_Y"

# a few comb cells for the scale shot (home.js geometry: R 0.5, gap 0.075, flat sides facing +-x)
comb_mat = mat("CombCell", "#2E2920", 0.5, 0.36)
comb_hi = mat("CombHoney", "#FFC93C", 0.3, 0.35, "#F5A623", 0.75)
cells = []
Rc, GAP = 0.5, 0.075
Dc = math.sqrt(3) * Rc + GAP
for q, r, h, honey in ((0, 0, 1.25, False), (1, 0, 0.9, False), (-1, 0, 0.95, False), (0, 1, 0.55, False),
                       (0, -1, 1.1, False), (1, -1, 0.75, True), (-1, 1, 0.6, False)):
    x3, z3 = Dc * (q + r / 2), Dc * (math.sqrt(3) / 2) * r     # three.js x, z
    bm = bmesh.new()
    ring0 = [bm.verts.new((Rc * math.sin(math.radians(a)), -Rc * math.cos(math.radians(a)), 0)) for a in range(0, 360, 60)]
    ring1 = [bm.verts.new((v.co.x, v.co.y, h)) for v in ring0]
    for k in range(6):
        bm.faces.new([ring0[k], ring0[(k + 1) % 6], ring1[(k + 1) % 6], ring1[k]])
    bm.faces.new(ring1); bm.faces.new(ring0[::-1])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new("cell"); bm.to_mesh(me); bm.free()
    me.materials.append(comb_hi if honey else comb_mat)
    o = bpy.data.objects.new("cell", me); sc.collection.objects.link(o)
    o.location = (x3, -z3, -1.6)
    o.hide_render = True
    cells.append(o)

variant_objs = [o for o in sc.objects if "_" in o.name and o.type == "MESH"]


def show(expr="neutral", carry=False, comb=False):
    for o in variant_objs:
        o.hide_render = True
    for n in ("EyeL", "EyeR", "Mouth"):
        sc.objects[n].hide_render = expr != "neutral"
        if expr != "neutral":
            sc.objects[f"{n}_{expr.capitalize()}"].hide_render = False
    job.hide_render = not carry
    for arm, q in ((armR, qR), (armL, qL)):
        arm.rotation_mode = "QUATERNION"
        arm.rotation_quaternion = q if carry else Quaternion()
    for c in cells:
        c.hide_render = not comb


def shot(name, cam_loc, target=(0, 0, 0), lens=50, pitch=5.0, root_loc=(0, 0, 0), yaw=0.0):
    root.rotation_euler = (0, -math.radians(pitch), math.radians(yaw))  # nose up (Blender -Y rot = pitch up)
    root.location = root_loc
    cam.location = cam_loc; tgt.location = target; cam_data.lens = lens
    sc.render.filepath = os.path.join(OUT, f"buzz-preview-{name}.png")
    if ONLY and name not in ONLY:
        return
    bpy.ops.render.render(write_still=True)
    print("PNG", sc.render.filepath)


show("neutral")
shot("34", (2.75, -1.75, 0.8), (0.03, 0, 0.03), lens=62)
shot("side", (0, -3.6, 0.12), (0, 0, 0.02), lens=58, pitch=4)
show("neutral", carry=True)
shot("carry", (2.7, -1.8, 0.45), (0.04, 0, -0.03), lens=62)
show("happy")
shot("happy", (2.3, -1.6, 0.5), (0.12, 0, 0.12), lens=85)
show("think")
shot("think", (2.3, -1.6, 0.5), (0.12, 0, 0.12), lens=85)
show("neutral", carry=True, comb=True)
shot("scale", (2.4, -5.4, 1.5), (0, 0, -0.5), lens=58, root_loc=(0.0, 0.0, 0.22), pitch=3, yaw=-8)
print("done")
