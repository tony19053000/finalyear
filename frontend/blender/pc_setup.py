"""hive. node Hardware tab hero: an original compact black PC tower: one three-quarter still + live light layers.

Run headless (Blender 5.x; EEVEE), ~1 min:
  blender -b --factory-startup --python pc_setup.py
Options: --yaw DEG (-36: the glass side towards the left-front)
         --w 1100 --h 700 --samples 64 --quality 80 --work DIR (default ./_work)

The tower (own design, minimal): matte warm-black body, a floating front slab with shadow gaps,
fine top slots, a frameless tempered-glass side with a black frit border, four rubber feet.
Inside (seen through the glass): micro-ATX board, a round downdraft CPU cooler (fan faces the
glass), four RAM sticks, an M.2 SSD, a GPU on a vertical mount (three fans face the glass), a
PSU shroud along the bottom, intake fans behind the front, an exhaust fan at the rear. Nothing
glows; floor = just a soft contact shadow.

Output (../public/pc-setup/, served statically by the dashboard): tower.webp (RGBA, transparent
background) and tower-{cpu,ram0..ram3,gpu}.webp, one light layer per part, same size, transparent
where unlit, screen-blended over the base by the page. Review sheet: ./renders/stills.png.

House style (website/blender/buzz3d.py): warm key from the top left, honey rim, cool weak fill,
Standard view transform; no bloom, no neon.
"""
import bpy, bmesh, math, os, sys
import numpy as np
from mathutils import Vector, Matrix, Euler

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "public", "pc-setup"))
REVIEW = os.path.join(HERE, "renders")
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def opt(name, default, cast=str):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return cast(argv[i + 1])
    return default


def vec3(s):
    return tuple(float(v) for v in s.split(","))


BASE_YAW = opt("--yaw", -36.0, float)
W = opt("--w", 1100, int)
H = opt("--h", 700, int)
WORK = opt("--work", os.path.join(HERE, "_work"))
SAMPLES = opt("--samples", 64, int)
QUALITY = opt("--quality", 80, int)
DIM = opt("--dim", 0.82, float)  # base brightness (a CSS filter on the base would break the lights' screen blend)
os.makedirs(WORK, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene


def srgb(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)


# ------------------------------------------------------------------ materials
MATS = {}


def blended(m):
    for attr, val in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND"), ("use_backface_culling", False)):
        try:
            setattr(m, attr, val)
        except Exception:
            pass


def mat(name, base, metal=0.5, rough=0.4, alpha=1.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*srgb(base), 1)
    b.inputs["Metallic"].default_value = metal
    b.inputs["Roughness"].default_value = rough
    if alpha < 1:
        b.inputs["Alpha"].default_value = alpha
        blended(m)
    MATS[name] = m
    return m


mat("Case", "#1d1a15", 0.35, 0.5)            # matte warm-black powder coat
mat("CaseSatin", "#221e17", 0.55, 0.36)      # front slab: a touch more sheen for its edges
mat("CaseHi", "#3d3422", 0.65, 0.34)         # tiny bronze accents (brackets, hex mark)
mat("Gap", "#060505", 0.0, 0.9)              # shadow gaps, frit border
mat("Rubber", "#0b0a09", 0.0, 0.85)
mat("Board", "#0f0e0c", 0.2, 0.72)
mat("Heatsink", "#2a2419", 0.65, 0.38)
mat("Ram", "#1c1812", 0.6, 0.36)
mat("GpuBody", "#1a1712", 0.4, 0.44)
mat("GpuWell", "#080706", 0.2, 0.6)
mat("FanBlade", "#131110", 0.3, 0.5)
mat("FanFrame", "#16130f", 0.35, 0.5)
mat("Glass", "#0b0a08", 0.0, 0.03, alpha=0.16)

# light-up parts: one material per part, emission off in the base render (a faint unlit diffuser tint)
PARTS = [  # (key, emit colour, strength when on, off-state colour)
    ("cpu", "#F5A623", 1.1, "#262117"),
    ("ram0", "#4FD1C5", 1.0, "#1b1f1d"),
    ("ram1", "#4FD1C5", 1.0, "#1b1f1d"),
    ("ram2", "#4FD1C5", 1.0, "#1b1f1d"),
    ("ram3", "#4FD1C5", 1.0, "#1b1f1d"),
    ("gpu", "#FF7A59", 1.0, "#261b17"),
]
EMIT = {}
SPILL = {k: [] for k, *_ in PARTS}
for key, col, strength, off in PARTS:
    m = mat("L_" + key, off, 0.0, 0.35)
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Emission Color"].default_value = (*srgb(col), 1)
    b.inputs["Emission Strength"].default_value = 0.0
    EMIT[key] = (b, strength)
mat("L_ssd", "#2a2620", 0.0, 0.35)  # the SSD LED stays an unlit dot


def shadow_mat(name, half, radius, strength):
    """Black, alpha = soft rounded-rectangle falloff around the object's origin (a fake contact shadow)."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    tc = nt.nodes.new("ShaderNodeTexCoord")
    ab = nt.nodes.new("ShaderNodeVectorMath"); ab.operation = "ABSOLUTE"
    sub = nt.nodes.new("ShaderNodeVectorMath"); sub.operation = "SUBTRACT"
    sub.inputs[1].default_value = (half[0], half[1], 10.0)
    mx = nt.nodes.new("ShaderNodeVectorMath"); mx.operation = "MAXIMUM"
    mx.inputs[1].default_value = (0.0, 0.0, 0.0)
    ln = nt.nodes.new("ShaderNodeVectorMath"); ln.operation = "LENGTH"
    mr = nt.nodes.new("ShaderNodeMapRange"); mr.interpolation_type = "SMOOTHSTEP"
    mr.inputs["From Min"].default_value = 0.0
    mr.inputs["From Max"].default_value = radius
    mr.inputs["To Min"].default_value = strength
    mr.inputs["To Max"].default_value = 0.0
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    em = nt.nodes.new("ShaderNodeEmission"); em.inputs["Color"].default_value = (0, 0, 0, 1)
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(tc.outputs["Object"], ab.inputs[0])
    nt.links.new(ab.outputs[0], sub.inputs[0])
    nt.links.new(sub.outputs[0], mx.inputs[0])
    nt.links.new(mx.outputs[0], ln.inputs[0])
    nt.links.new(ln.outputs["Value"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], mix.inputs[0])
    nt.links.new(tr.outputs[0], mix.inputs[1])
    nt.links.new(em.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    blended(m)
    MATS[name] = m
    return m


# ------------------------------------------------------------------ geometry helpers
def rotm(rot):
    if rot is None:
        return Matrix()
    return Euler(tuple(math.radians(a) for a in rot)).to_matrix().to_4x4()


def add_box(bm, size, loc=(0, 0, 0), rot=None):
    vs = bmesh.ops.create_cube(bm, size=1.0)["verts"]
    bmesh.ops.transform(bm, matrix=Matrix.Translation(loc) @ rotm(rot) @ Matrix.Diagonal((*size, 1)), verts=vs)


def add_cyl(bm, r, depth, loc=(0, 0, 0), rot=None, seg=40):
    vs = bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=seg, radius1=r, radius2=r, depth=depth)["verts"]
    bmesh.ops.transform(bm, matrix=Matrix.Translation(loc) @ rotm(rot), verts=vs)


def add_tube(bm, r_out, r_in, depth, loc=(0, 0, 0), rot=None, seg=56):
    """Open ring (a fan shroud): outer and inner walls plus the two annular faces, axis = local Z."""
    M = Matrix.Translation(loc) @ rotm(rot)
    def ring(r, z):
        return [bm.verts.new(M @ Vector((r * math.cos(2 * math.pi * i / seg), r * math.sin(2 * math.pi * i / seg), z))) for i in range(seg)]
    of, ob, inf, inb = ring(r_out, depth / 2), ring(r_out, -depth / 2), ring(r_in, depth / 2), ring(r_in, -depth / 2)
    for i in range(seg):
        j = (i + 1) % seg
        bm.faces.new([of[i], ob[i], ob[j], of[j]])
        bm.faces.new([inf[i], inf[j], inb[j], inb[i]])
        bm.faces.new([of[i], of[j], inf[j], inf[i]])
        bm.faces.new([ob[i], inb[i], inb[j], ob[j]])


def add_torus(bm, R, r, loc=(0, 0, 0), rot=None, seg=56, segr=6):
    M = Matrix.Translation(loc) @ rotm(rot)
    rows = []
    for i in range(seg):
        a = 2 * math.pi * i / seg
        rows.append([bm.verts.new(M @ Vector(((R + r * math.cos(b)) * math.cos(a), (R + r * math.cos(b)) * math.sin(a), r * math.sin(b))))
                     for b in (2 * math.pi * j / segr for j in range(segr))])
    for i in range(seg):
        for j in range(segr):
            bm.faces.new([rows[i][j], rows[(i + 1) % seg][j], rows[(i + 1) % seg][(j + 1) % segr], rows[i][(j + 1) % segr]])


def add_hex_ring(bm, Ro, Ri, t, loc=(0, 0, 0), rot=None):
    """Flat hex outline in the local XY plane (pointy top), thickness t along local Z."""
    M = Matrix.Translation(loc) @ rotm(rot)
    def ring(R, z):
        return [bm.verts.new(M @ Vector((R * math.cos(math.radians(a + 30)), R * math.sin(math.radians(a + 30)), z))) for a in range(0, 360, 60)]
    of, inf, ob, inb = ring(Ro, t / 2), ring(Ri, t / 2), ring(Ro, -t / 2), ring(Ri, -t / 2)
    for k in range(6):
        k2 = (k + 1) % 6
        bm.faces.new([of[k], of[k2], inf[k2], inf[k]])
        bm.faces.new([ob[k], inb[k], inb[k2], ob[k2]])
        bm.faces.new([of[k], ob[k], ob[k2], of[k2]])
        bm.faces.new([inf[k], inf[k2], inb[k2], inb[k]])


def add_blades(bm, n, r_hub, r_tip, chord, pitch=30, sweep=14, z=0.0):
    for k in range(n):
        M = Euler((0, 0, math.radians(360 / n * k))).to_matrix().to_4x4() @ Matrix.Translation(((r_hub + r_tip) / 2, 0, z)) @ \
            Euler((math.radians(pitch), 0, math.radians(sweep))).to_matrix().to_4x4()
        vs = bmesh.ops.create_cube(bm, size=1.0)["verts"]
        bmesh.ops.transform(bm, matrix=M @ Matrix.Diagonal((r_tip - r_hub, chord, 0.0011, 1)), verts=vs)


def obj(name, build, material, parent, loc=(0, 0, 0), rot=None, bevel=0.0, smooth=False, segments=2):
    bm = bmesh.new()
    build(bm)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    if smooth:
        for p in me.polygons:
            p.use_smooth = True
        try:
            me.set_sharp_from_angle(angle=math.radians(40))
        except Exception:
            pass
    me.materials.append(MATS[material] if isinstance(material, str) else material)
    ob = bpy.data.objects.new(name, me)
    sc.collection.objects.link(ob)
    ob.parent = parent
    ob.location = loc
    if rot is not None:
        ob.rotation_euler = Euler(tuple(math.radians(a) for a in rot))
    if bevel > 0:
        md = ob.modifiers.new("bevel", "BEVEL")
        md.width = bevel
        md.segments = segments
        md.limit_method = "ANGLE"
    return ob


def box(name, size, loc, material, parent, bevel=0.0, segments=2):
    return obj(name, lambda bm: add_box(bm, size), material, parent, loc, None, bevel, segments=segments)


def empty(name, parent=None, loc=(0, 0, 0), rot=(0, 0, 0)):
    ob = bpy.data.objects.new(name, None)
    sc.collection.objects.link(ob)
    ob.parent = parent
    ob.location = loc
    ob.rotation_euler = Euler(tuple(math.radians(a) for a in rot))
    return ob


def point_light(name, parent, loc, color, energy, radius=0.005, part=None):
    L = bpy.data.lights.new(name, "POINT")
    L.color = srgb(color)
    L.energy = energy
    L.shadow_soft_size = radius
    ob = bpy.data.objects.new(name, L)
    sc.collection.objects.link(ob)
    ob.parent = parent
    ob.location = loc
    if part is not None:
        ob.hide_render = True
        SPILL[part].append(ob)
    return ob


def fan(name, parent, loc, axis, size=0.12, thick=0.025):
    """Case fan (square frame, round throat, 7 blades); local Z turned onto `axis`."""
    rot = {"X": (0, 90, 0), "-Y": (90, 0, 0)}[axis]
    g = empty(name, parent, loc, rot)
    s, b = size / 2, 0.006

    def frame(bm):
        for (sx, sy, x, y) in ((size, b, 0, s - b / 2), (size, b, 0, -s + b / 2), (b, size, s - b / 2, 0), (b, size, -s + b / 2, 0)):
            add_box(bm, (sx, sy, thick), (x, y, 0))
        add_tube(bm, s - 0.002, s * 0.94, thick)
    obj(name + "_frame", frame, "FanFrame", g, bevel=0.001, smooth=True)

    def rotor(bm):
        add_cyl(bm, s * 0.3, thick * 0.7)
        add_blades(bm, 7, s * 0.28, s * 0.9, s * 0.38)
    obj(name + "_rotor", rotor, "FanBlade", g, smooth=True)
    return g


# ------------------------------------------------------------------ the tower
# local frame: origin at the floor under the tower's centre; +X = front, -X = rear, glass side = -Y
root = empty("Root")
T = empty("Tower", root)
L, Wd, Hh, zb = 0.40, 0.21, 0.44, 0.012
z0, z1 = zb, zb + Hh
xr, xf, yb, yg = -L / 2, L / 2, Wd / 2, -Wd / 2
mid = (z0 + z1) / 2

# shell: bottom, top (fine slots), back, rear, a front bulkhead and a floating front slab
box("CaseBottom", (L, Wd, 0.010), (0, 0, z0 + 0.005), "Case", T, 0.003)
box("CaseTop", (L, Wd, 0.012), (0, 0, z1 - 0.006), "Case", T, 0.004, 3)
def top_slots(bm):
    for k in range(14):
        add_box(bm, (0.0032, 0.15, 0.0012), (-0.17 + k * 0.0105, 0.0, z1 + 0.0001))
obj("TopSlots", top_slots, "Gap", T)
box("CaseBack", (L, 0.008, Hh), (0, yb - 0.004, mid), "Case", T, 0.003)
box("CaseRear", (0.010, Wd, Hh), (xr + 0.005, 0, mid), "Case", T, 0.003)
box("Bulkhead", (0.008, Wd - 0.004, Hh - 0.004), (xf - 0.004, 0, mid), "Case", T, 0.002)
box("FrontStandoffs", (0.006, Wd - 0.05, Hh - 0.05), (xf + 0.003, 0, mid), "Gap", T)
FRONT_T = 0.016
box("FrontSlab", (FRONT_T, Wd + 0.002, Hh + 0.002), (xf + 0.006 + FRONT_T / 2, 0, mid), "CaseSatin", T, 0.0045, 3)
def front_grooves(bm):
    for y in (-0.074, 0.074):
        add_box(bm, (0.0012, 0.0022, Hh - 0.10), (xf + 0.006 + FRONT_T + 0.0001, y, mid + 0.01))
obj("FrontGrooves", front_grooves, "Gap", T)
obj("FrontHex", lambda bm: add_hex_ring(bm, 0.0105, 0.0078, 0.0012), "CaseHi", T,
    (xf + 0.006 + FRONT_T + 0.0004, 0, z0 + 0.05), rot=(0, 90, 0))
obj("PowerButton", lambda bm: add_cyl(bm, 0.0055, 0.003, rot=(0, 90, 0), seg=32), "CaseHi", T,
    (xf + 0.006 + FRONT_T + 0.0012, 0, z1 - 0.035), smooth=True)

# frameless glass side with a black frit border printed on the inside
glass = box("Glass", (L - 0.002, 0.005, Hh - 0.002), (0, yg - 0.0005, mid), "Glass", T, 0.0015)
def frit(bm):
    f, y = 0.013, yg + 0.0025
    add_box(bm, (L - 0.004, 0.001, f), (0, y, z1 - f / 2 - 0.001))
    add_box(bm, (L - 0.004, 0.001, f), (0, y, z0 + f / 2 + 0.001))
    add_box(bm, (f, 0.001, Hh - 0.004), (xr + f / 2 + 0.001, y, mid))
    add_box(bm, (f, 0.001, Hh - 0.004), (xf - f / 2 - 0.001, y, mid))
obj("Frit", frit, "Gap", T)
for x in (-0.16, 0.16):
    for y in (-0.074, 0.074):
        obj(f"Foot{x}{y}", lambda bm: add_cyl(bm, 0.017, zb, seg=32), "Rubber", T, (x, y, zb / 2), bevel=0.003, smooth=True)

# soft, dim interior light under the top (warm white, no colour)
IL = bpy.data.lights.new("CaseLight", "AREA")
IL.shape, IL.size, IL.size_y, IL.energy, IL.color = "RECTANGLE", 0.30, 0.10, 1.4, srgb("#FFE9C2")
il = bpy.data.objects.new("CaseLight", IL)
sc.collection.objects.link(il)
il.parent = T
il.location = (0.0, -0.03, z1 - 0.016)

# micro-ATX board + details
BX0, BX1, BZ0, BZ1 = -0.188, 0.056, 0.194, 0.438
box("Board", (BX1 - BX0, 0.002, BZ1 - BZ0), ((BX0 + BX1) / 2, 0.088, (BZ0 + BZ1) / 2), "Board", T)
CX, CZ = -0.100, 0.356
box("VrmTop", (0.075, 0.022, 0.016), (CX, 0.076, 0.428), "Heatsink", T, 0.002)
box("VrmLeft", (0.016, 0.022, 0.08), (CX - 0.066, 0.076, CZ), "Heatsink", T, 0.002)
box("Chipset", (0.042, 0.008, 0.034), (0.022, 0.083, 0.232), "Heatsink", T, 0.002)
box("Atx24", (0.010, 0.016, 0.052), (0.047, 0.079, 0.36), "Board", T, 0.001)
def grommets(bm):
    for z in (0.25, 0.33, 0.41):
        add_box(bm, (0.014, 0.004, 0.05), (0.082, 0.086, z))
obj("Grommets", grommets, "Rubber", T, bevel=0.002)

# round downdraft cooler: square finned block, round fan shroud facing the glass, honey ring + hub hex
box("CoolerBlock", (0.088, 0.032, 0.088), (CX, 0.068, CZ), "Heatsink", T, 0.003)
def block_fins(bm):
    for k in range(13):
        add_box(bm, (0.090, 0.0008, 0.0016), (CX, 0.0515, CZ - 0.042 + k * 0.007))
obj("CoolerFins", block_fins, "Board", T)
obj("CoolerShroud", lambda bm: add_tube(bm, 0.047, 0.041, 0.018, rot=(90, 0, 0)), "Heatsink", T, (CX, 0.043, CZ), smooth=True)
obj("CoolerRotor", lambda bm: (add_cyl(bm, 0.013, 0.008), add_blades(bm, 9, 0.012, 0.039, 0.016)), "FanBlade", T,
    (CX, 0.040, CZ), rot=(90, 0, 0), smooth=True)
obj("CoolerHub", lambda bm: add_cyl(bm, 0.0135, 0.004, seg=48), "CaseHi", T, (CX, 0.035, CZ), rot=(90, 0, 0), smooth=True)
obj("CpuRing", lambda bm: add_torus(bm, 0.044, 0.0017), MATS["L_cpu"], T, (CX, 0.0335, CZ), rot=(90, 0, 0), smooth=True)
obj("CpuHex", lambda bm: add_hex_ring(bm, 0.0085, 0.0062, 0.001), MATS["L_cpu"], T, (CX, 0.0327, CZ), rot=(90, 0, 0))
point_light("CpuSpill", T, (CX, 0.012, CZ), "#F5A623", 0.03, 0.01, part="cpu")

# RAM: four sticks right of the cooler, light bars on the edge facing the glass
RAM_X = [-0.034, -0.024, -0.014, -0.004]
for k, x in enumerate(RAM_X):
    box(f"Ram{k}", (0.0068, 0.038, 0.133), (x, 0.068, CZ), "Ram", T, 0.0012)
    box(f"RamBar{k}", (0.0056, 0.005, 0.124), (x, 0.0465, CZ), MATS[f"L_ram{k}"], T, 0.0012)
    point_light(f"RamSpill{k}", T, (x, 0.034, CZ), "#4FD1C5", 0.008, 0.004, part=f"ram{k}")

# M.2 SSD (heatsink + status LED) between the cooler and the GPU
M2X, M2Z = CX + 0.002, 0.266
box("M2", (0.08, 0.006, 0.022), (M2X, 0.084, M2Z), "Heatsink", T, 0.0012)
def m2_ridges(bm):
    for k in range(4):
        add_box(bm, (0.064, 0.0008, 0.0012), (M2X - 0.005, 0.0808, M2Z - 0.0075 + k * 0.005))
obj("M2Ridges", m2_ridges, "Board", T)
obj("SsdLed", lambda bm: add_cyl(bm, 0.0019, 0.0016, rot=(90, 0, 0), seg=16), MATS["L_ssd"], T,
    (M2X + 0.033, 0.0806, M2Z), smooth=True)

# GPU on a vertical mount across the middle: three fans face the glass, coral rings + an edge strip
GX, GZ, GL, GH = -0.040, 0.153, 0.27, 0.12
box("GpuBackplate", (GL, 0.004, GH), (GX, -0.012, GZ), "Heatsink", T, 0.0015)
box("GpuBody", (GL, 0.036, GH), (GX, -0.032, GZ), "GpuBody", T, 0.004, 3)
def gpu_trim(bm):
    for sz in (-1, 1):
        add_box(bm, (GL - 0.02, 0.0015, 0.0025), (GX, -0.0505, GZ + sz * 0.055))
obj("GpuTrim", gpu_trim, "CaseHi", T)
box("GpuBracket", (0.010, 0.03, GH + 0.006), (GX - GL / 2 - 0.006, -0.03, GZ), "CaseHi", T, 0.0015)
for k, dx in enumerate((-0.088, 0.0, 0.088)):
    fx = GX + dx
    obj(f"GpuWell{k}", lambda bm: add_cyl(bm, 0.0405, 0.004, rot=(90, 0, 0), seg=56), "GpuWell", T, (fx, -0.0515, GZ), smooth=True)
    obj(f"GpuRotor{k}", lambda bm: (add_cyl(bm, 0.011, 0.006, seg=32), add_blades(bm, 9, 0.010, 0.037, 0.014)), "FanBlade", T,
        (fx, -0.0545, GZ), rot=(90, 0, 0), smooth=True)
    obj(f"GpuRing{k}", lambda bm: add_torus(bm, 0.0415, 0.0015), MATS["L_gpu"], T, (fx, -0.0535, GZ), rot=(90, 0, 0), smooth=True)
    point_light(f"GpuSpill{k}", T, (fx, -0.075, GZ), "#FF7A59", 0.012, 0.01, part="gpu")
box("GpuEdge", (GL - 0.07, 0.005, 0.0018), (GX, -0.045, GZ + GH / 2 + 0.0009), MATS["L_gpu"], T, 0.0007)

# PSU shroud along the bottom (the PSU lives inside it), a small bronze hex on its face
SH_X0, SH_X1, SH_Z1 = xr + 0.010, 0.140, 0.086
box("Shroud", (SH_X1 - SH_X0, Wd - 0.012, SH_Z1 - z0 - 0.010), ((SH_X0 + SH_X1) / 2, 0.002, (z0 + 0.010 + SH_Z1) / 2), "Case", T, 0.003)
box("ShroudSeam", (SH_X1 - SH_X0 - 0.01, 0.0015, 0.0015), ((SH_X0 + SH_X1) / 2, yg + 0.0125, SH_Z1 - 0.012), "Gap", T)
obj("ShroudHex", lambda bm: add_hex_ring(bm, 0.0085, 0.0064, 0.001), "CaseHi", T, (0.105, yg + 0.0115, (z0 + SH_Z1) / 2 + 0.002), rot=(90, 0, 0))

# fans: three intakes behind the front, one exhaust at the rear
for k, z in enumerate((0.098, 0.226, 0.354)):
    fan(f"FrontFan{k}", T, (xf - 0.022, 0.0, z), "X")
fan("RearFan", T, (xr + 0.0225, 0.02, 0.37), "X")

# floor: two soft shadows (tight contact under the footprint + a wide soft one), nothing else
def add_plane(bm, sx, sy):
    vs = bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5)["verts"]
    bmesh.ops.transform(bm, matrix=Matrix.Diagonal((sx, sy, 1, 1)), verts=vs)


cs = obj("ContactShadow", lambda bm: add_plane(bm, 0.9, 0.7), shadow_mat("ShadowTight", (0.195, 0.098), 0.03, 0.92), T, (0, 0, 0.0004))
ss = obj("SoftShadow", lambda bm: add_plane(bm, 1.6, 1.4), shadow_mat("ShadowSoft", (0.16, 0.07), 0.26, 0.5), None, (0.03, 0.04, 0.0))
for o in (cs, ss, glass):
    try:
        o.visible_shadow = False
    except Exception:
        pass

# ------------------------------------------------------------------ world, lights, camera
engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
for attr, val in (("taa_render_samples", SAMPLES), ("use_shadows", True), ("use_raytracing", True),
                  ("shadow_ray_count", 3), ("shadow_step_count", 8), ("use_fast_gi", True)):
    try:
        setattr(sc.eevee, attr, val)
    except Exception:
        pass
sc.render.resolution_x, sc.render.resolution_y = W, H
sc.render.resolution_percentage = 100
sc.render.film_transparent = True
sc.view_settings.view_transform = "Standard"
sc.view_settings.look = "None"

world = bpy.data.worlds.new("w")
sc.world = world
world.use_nodes = True
wn = world.node_tree
wn.nodes.clear()
wout = wn.nodes.new("ShaderNodeOutputWorld")
amb = wn.nodes.new("ShaderNodeBackground")
amb.inputs[0].default_value = (*srgb("#C9C2B4"), 1)
amb.inputs[1].default_value = opt("--amb", 0.10, float)
wn.links.new(amb.outputs[0], wout.inputs[0])

# glass sheen: a soft diagonal band (a studio card's reflection) so the pane reads as glass
gm = MATS["Glass"]
gnt = gm.node_tree
gb = gnt.nodes["Principled BSDF"]
gb.inputs["Emission Color"].default_value = (*srgb("#E8DCC8"), 1)
gtc = gnt.nodes.new("ShaderNodeTexCoord")
gsep = gnt.nodes.new("ShaderNodeSeparateXYZ")
gmul = gnt.nodes.new("ShaderNodeMath"); gmul.operation = "MULTIPLY_ADD"
gmul.inputs[1].default_value = 0.75
gabs = gnt.nodes.new("ShaderNodeMath"); gabs.operation = "ABSOLUTE"
goff = gnt.nodes.new("ShaderNodeMath"); goff.operation = "SUBTRACT"
goff.inputs[1].default_value = 0.06
gmr = gnt.nodes.new("ShaderNodeMapRange"); gmr.interpolation_type = "SMOOTHSTEP"
gmr.inputs["From Min"].default_value, gmr.inputs["From Max"].default_value = 0.0, 0.055
gmr.inputs["To Min"].default_value, gmr.inputs["To Max"].default_value = opt("--sheen", 0.16, float), 0.0
gnt.links.new(gtc.outputs["Object"], gsep.inputs[0])
gnt.links.new(gsep.outputs["X"], gmul.inputs[0])
gnt.links.new(gsep.outputs["Z"], gmul.inputs[2])
gnt.links.new(gmul.outputs[0], goff.inputs[0])
gnt.links.new(goff.outputs[0], gabs.inputs[0])
gnt.links.new(gabs.outputs[0], gmr.inputs["Value"])
gnt.links.new(gmr.outputs["Result"], gb.inputs["Emission Strength"])


def aim(ob, target):
    d = Vector(target) - ob.location
    ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def studio(name, loc, target, color, energy, size, size_y=None):
    Ld = bpy.data.lights.new(name, "AREA")
    Ld.color = srgb(color)
    Ld.energy = energy
    if size_y:
        Ld.shape, Ld.size, Ld.size_y = "RECTANGLE", size, size_y
    else:
        Ld.size = size
    o = bpy.data.objects.new(name, Ld)
    sc.collection.objects.link(o)
    o.location = loc
    aim(o, target)
    return o


KEYE = opt("--key", 150.0, float)
studio("Key", (-1.3, -1.1, 1.5), (0.0, 0.0, 0.2), "#FFE9C2", KEYE, 1.4, 1.0)
studio("Rim", (1.2, 1.3, 0.95), (0.0, 0.0, 0.25), "#F5A623", KEYE * 0.5, 0.8)
studio("Fill", (1.4, -1.2, 0.35), (0.0, 0.0, 0.22), "#9AA6B8", KEYE * 0.12, 1.2)
studio("Top", (0.0, 0.3, 2.0), (0.0, 0.0, 0.0), "#F2EDE4", KEYE * 0.25, 2.0, 1.4)

cam_data = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_data)
sc.collection.objects.link(cam)
sc.camera = cam
cam.location = Vector(opt("--cam", (0.0, -1.34, 0.60), vec3))
aim(cam, opt("--tgt", (0.0, 0.0, 0.225), vec3))
cam_data.lens = opt("--lens", 50.0, float)
cam_data.sensor_width = 36


# ------------------------------------------------------------------ render: the base + one layer per light
def set_part(key, on):
    b, strength = EMIT[key]
    b.inputs["Emission Strength"].default_value = strength if on else 0.0
    for o in SPILL[key]:
        o.hide_render = not on


def render_to(path):
    s = sc.render.image_settings
    s.file_format, s.color_mode, s.color_depth = "PNG", "RGBA", "8"
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


def load_np(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    a = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(a)
    bpy.data.images.remove(img)
    return np.flipud(a.reshape(h, w, 4)).copy()


def save_np(arr, path, fmt="WEBP", quality=QUALITY):
    h, w, c = arr.shape
    img = bpy.data.images.new("tmp_out", w, h, alpha=(c == 4))
    px = np.ones((h, w, 4), np.float32)
    px[..., :c] = np.clip(arr, 0, 1)
    img.pixels.foreach_set(np.flipud(px).ravel())
    s = sc.render.image_settings
    s.file_format = fmt
    s.color_mode = "RGBA" if c == 4 else "RGB"
    s.color_depth = "8"
    if fmt == "WEBP":
        s.quality = quality
    img.save_render(path, scene=sc)
    bpy.data.images.remove(img)


ANGLES = {"centre": BASE_YAW}
KEYS = [k for k, *_ in PARTS]
for k in KEYS:
    set_part(k, False)
for name, yaw in ANGLES.items():
    if "--post-only" in argv:  # re-encode from the PNGs already in WORK
        break
    root.rotation_euler = (0, 0, math.radians(yaw))
    render_to(os.path.join(WORK, f"s_{name}.png"))
    for k in KEYS:
        set_part(k, True)
        render_to(os.path.join(WORK, f"s_{name}_{k}.png"))
        set_part(k, False)
    print("RENDERED", name, flush=True)

# ------------------------------------------------------------------ post: one shared crop, base RGBA + layers on black
# layer L per part: screen(base, L) == lit, i.e. L = (lit - base) / (1 - base) on premultiplied colour
bases = {n: load_np(os.path.join(WORK, f"s_{n}.png")) for n in ANGLES}
alpha = np.maximum.reduce([b[..., 3] for b in bases.values()])
ys, xs = np.where(alpha > 1 / 255)
PAD = 16
y0, y1 = max(0, ys.min() - PAD), min(H, ys.max() + PAD + 1)
x0, x1 = max(0, xs.min() - PAD), min(W, xs.max() + PAD + 1)
y0 -= (y1 - y0) % 2  # even size
x0 -= (x1 - x0) % 2
os.makedirs(OUT, exist_ok=True)
for f in os.listdir(OUT):
    if f.endswith(".webp") or f.endswith(".json"):
        os.remove(os.path.join(OUT, f))
sheet = []
for n, base in bases.items():
    crop = base[y0:y1, x0:x1].copy()
    p_full = crop[..., :3] * crop[..., 3:4]  # undimmed, premultiplied: what the lit renders differ from
    crop[..., :3] *= DIM  # the body a touch darker; each light still adds its full energy on top
    save_np(crop, os.path.join(OUT, "tower.webp"))
    pb = crop[..., :3] * crop[..., 3:4]
    lit_all = pb.copy()
    for k in KEYS:
        lit = load_np(os.path.join(WORK, f"s_{n}_{k}.png"))[y0:y1, x0:x1]
        diff = np.clip(lit[..., :3] * lit[..., 3:4] - p_full, 0, 1)
        layer = np.clip(diff / np.maximum(1.0 - pb, 1e-3), 0, 1)  # screen(pb, layer) == pb + diff
        layer[layer.max(axis=2) < 2.5 / 255] = 0
        # RGBA, transparent where unlit: alpha = brightness, colour un-premultiplied, so a browser's
        # screen blend over an opaque base gives exactly screen(base, layer) and nothing shows elsewhere
        a = layer.max(axis=2, keepdims=True)
        save_np(np.concatenate([np.where(a > 0, layer / np.maximum(a, 1e-6), 0), a], axis=2),
                os.path.join(OUT, f"tower-{k}.webp"))
        lit_all = 1 - (1 - lit_all) * (1 - layer)
    bg = np.array([11, 10, 8], np.float32) / 255
    sheet.append(np.concatenate([pb + bg * (1 - crop[..., 3:4]), lit_all + bg * (1 - crop[..., 3:4])], axis=0))
os.makedirs(REVIEW, exist_ok=True)
save_np(np.concatenate(sheet, axis=1), os.path.join(REVIEW, "stills.png"), "PNG")
total = sum(os.path.getsize(os.path.join(OUT, f)) for f in os.listdir(OUT))
print(f"DONE crop {x1 - x0}x{y1 - y0} total {total / 1e3:.0f} KB in {OUT}")

