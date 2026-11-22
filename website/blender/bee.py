"""hive. mascot - a soft clay/vinyl-toy bee, rendered on a transparent background.

Run headless (Blender 4.2+ / 5.x):
  blender -b -P bee.py -- <out_dir> --pose idle|wave|carry|fly [--size 360] [--frames 8]

Writes <out_dir>/bee-<pose>.webp : a horizontal sprite sheet of N frames (SIZE x SIZE each),
and <out_dir>/_frames_<pose>/fNN.png (the single frames; frame 0 doubles as a still).

Character: big round yellow head, small body with thick dark stripes, happy closed eyes,
rosy cheeks, ball antennae, white rounded wings, stubby dark arms and legs, and a small
dark hexagon badge with a honey ">_" on the belly. One matte "clay" material family.
"""
import bpy, bmesh, math, os, sys
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = os.path.abspath(argv[0] if argv and not argv[0].startswith("--") else ".")
def arg(name, default):
    return argv[argv.index(name) + 1] if name in argv else default
POSE = arg("--pose", "idle")
SIZE = int(arg("--size", 360))
FRAMES = int(arg("--frames", 8))
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
sc.render.resolution_x = sc.render.resolution_y = SIZE
sc.render.film_transparent = True
sc.view_settings.view_transform = "Standard"
for attr, val in (("taa_render_samples", 64), ("use_raytracing", True), ("use_shadows", True)):
    try:
        setattr(sc.eevee, attr, val)
    except Exception:
        pass

world = bpy.data.worlds.new("w"); sc.world = world; world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs[0].default_value = (0.045, 0.038, 0.030, 1); bg.inputs[1].default_value = 1.0


def clay(name, rgb, rough=0.62, sss=0.18, sheen=0.25, emit=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = rough
    for k, v in (("Subsurface Weight", sss), ("Subsurface Scale", 0.04), ("Sheen Weight", sheen),
                 ("Specular IOR Level", 0.35)):
        try:
            b.inputs[k].default_value = v
        except Exception:
            pass
    try:
        b.inputs["Subsurface Radius"].default_value = (1.0, 0.45, 0.2)
    except Exception:
        pass
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1)
        b.inputs["Emission Strength"].default_value = emit
    return m, b


YEL = (1.0, 0.53, 0.040)       # warm bee yellow
DARK = (0.022, 0.018, 0.014)
WHITE = (0.88, 0.86, 0.82)
PINK = (1.0, 0.30, 0.22)
HONEY = (1.0, 0.36, 0.012)

head_mat, _ = clay("head", YEL)
dark_mat, _ = clay("dark", DARK, rough=0.55, sss=0.0, sheen=0.35)
wing_mat, _ = clay("wing", WHITE, rough=0.5, sss=0.25, sheen=0.2)
cheek_mat, _ = clay("cheek", PINK, rough=0.7, sss=0.1)
honey_mat, _ = clay("honey", HONEY, rough=0.35, sss=0.3, sheen=0.0, emit=0.35)
glyph_mat, _ = clay("glyph", (1.0, 0.62, 0.06), rough=0.4, sss=0.0, sheen=0.0, emit=2.0)

# body: thick dark stripes painted along the body's own Z
body_mat, bsdf = clay("body", YEL)
nt = body_mat.node_tree
tc = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ")
mr = nt.nodes.new("ShaderNodeMapRange"); mr.inputs["From Min"].default_value = -1; mr.inputs["From Max"].default_value = 1
ramp = nt.nodes.new("ShaderNodeValToRGB"); ramp.color_ramp.interpolation = "CONSTANT"
stops = [(0.0, DARK), (0.10, YEL), (0.22, DARK), (0.36, YEL), (0.62, DARK), (0.75, YEL)]
els = ramp.color_ramp.elements
els[0].position, els[0].color = stops[0][0], (*stops[0][1], 1)
els[1].position, els[1].color = stops[1][0], (*stops[1][1], 1)
for pos, col in stops[2:]:
    e = els.new(pos); e.color = (*col, 1)
nt.links.new(tc.outputs["Object"], sep.inputs[0]); nt.links.new(sep.outputs["Z"], mr.inputs["Value"])
nt.links.new(mr.outputs["Result"], ramp.inputs["Fac"]); nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

rig = bpy.data.objects.new("rig", None); bpy.context.collection.objects.link(rig)


def finish(o, mat, levels=2, parent=rig):
    if mat is not None:
        o.data.materials.append(mat)
    bpy.context.view_layer.objects.active = o
    o.select_set(True)
    try:
        bpy.ops.object.shade_smooth()
    except Exception:
        pass
    o.select_set(False)
    if levels:
        m = o.modifiers.new("sub", "SUBSURF"); m.levels = levels; m.render_levels = levels
    if parent is not None:
        o.parent = parent
    return o


def sphere(r, loc, mat, scale=(1, 1, 1), levels=2, parent=rig, seg=32, ring=16):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=loc, segments=seg, ring_count=ring)
    o = bpy.context.object; o.scale = scale
    return finish(o, mat, levels, parent)


def tube(points, bevel, mat, parent=rig, name="tube", sharp=False):
    cu = bpy.data.curves.new(name, "CURVE"); cu.dimensions = "3D"
    cu.bevel_depth = bevel; cu.bevel_resolution = 6; cu.use_fill_caps = True
    sp = cu.splines.new("BEZIER"); sp.bezier_points.add(len(points) - 1)
    for bp, p in zip(sp.bezier_points, points):
        bp.co = p; bp.handle_left_type = bp.handle_right_type = ("VECTOR" if sharp else "AUTO")
    o = bpy.data.objects.new(name, cu); bpy.context.collection.objects.link(o)
    cu.materials.append(mat); o.parent = parent
    return o


def limb(root, length, radius, mat, rot):
    """Capsule hanging down from `root` (its origin), so rotating it swings the limb."""
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0, 0, 0), segments=24, ring_count=12)
    o = bpy.context.object
    bm = bmesh.new(); bm.from_mesh(o.data)
    bmesh.ops.scale(bm, vec=(radius, radius, length / 2), verts=bm.verts)
    bmesh.ops.translate(bm, vec=(0, 0, -length / 2 + radius * 0.4), verts=bm.verts)
    bm.to_mesh(o.data); bm.free()
    o.location = root; o.rotation_euler = rot
    return finish(o, mat, 2)


# ---------- head + face ----------
HC = Vector((0, 0, 1.30)); HR = Vector((0.98, 0.90, 0.88))
sphere(1.0, HC, head_mat, scale=tuple(HR), seg=48, ring=24)


def on_head(x, z, out=0.0):
    """Point on the head's front surface at (x, z), pushed `out` along -Y."""
    dz = (z - HC.z) / HR.z; dx = x / HR.x
    t = max(0.0, 1 - dx * dx - dz * dz)
    return Vector((x, -HR.y * math.sqrt(t) - out, z))


for side in (-1, 1):
    ex, ez = 0.34 * side, 1.40
    tube([on_head(ex - 0.14, ez - 0.03, 0.01), on_head(ex, ez + 0.07, 0.01), on_head(ex + 0.14, ez - 0.03, 0.01)], 0.04, dark_mat, name="eye")
    c = on_head(0.56 * side, 1.12, -0.03)
    sphere(0.13, c, cheek_mat, scale=(1, 0.32, 0.7), levels=1)
    tube([Vector((0.26 * side, -0.05, 2.10)), Vector((0.40 * side, -0.12, 2.45)), Vector((0.62 * side, -0.18, 2.62))], 0.035, dark_mat, name="ant")
    sphere(0.11, (0.64 * side, -0.18, 2.63), dark_mat)
tube([on_head(-0.12, 1.15, 0.0), on_head(0.0, 1.08, 0.0), on_head(0.12, 1.15, 0.0)], 0.033, dark_mat, name="smile")

# ---------- body ----------
body = sphere(0.68, (0, 0.10, 0.12), body_mat, scale=(1.0, 0.95, 1.05), seg=48, ring=24)
bpy.ops.mesh.primitive_cone_add(radius1=0.12, depth=0.3, location=(0, 0.78, -0.12), rotation=(math.radians(-100), 0, 0), vertices=24)
finish(bpy.context.object, dark_mat, 1)

# belly badge: dark hexagon with a honey ">_"
bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=0.25, depth=0.06, location=(0, -0.64, 0.10), rotation=(math.radians(90), math.radians(30), 0))
badge = bpy.context.object
bv = badge.modifiers.new("bev", "BEVEL"); bv.width = 0.02; bv.segments = 3
finish(badge, dark_mat, 0)
GY = -0.678  # just in front of the badge face
tube([Vector((-0.13, GY, 0.185)), Vector((-0.045, GY, 0.11))], 0.026, glyph_mat, name="chev1", sharp=True)
tube([Vector((-0.045, GY, 0.11)), Vector((-0.13, GY, 0.035))], 0.026, glyph_mat, name="chev2", sharp=True)
tube([Vector((-0.005, GY, 0.035)), Vector((0.12, GY, 0.035))], 0.026, glyph_mat, name="under", sharp=True)

# ---------- wings (white, rounded, two lobes per side) ----------
wings = []
for side in (-1, 1):
    for (sx, sz, root, ang) in ((0.56, 0.38, (0.40, 0.50, 0.92), 42), (0.40, 0.27, (0.40, 0.55, 0.56), 2)):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1, location=(0, 0, 0), segments=32, ring_count=16)
        w = bpy.context.object
        bm = bmesh.new(); bm.from_mesh(w.data)
        bmesh.ops.scale(bm, vec=(sx, 0.07, sz), verts=bm.verts)
        bmesh.ops.translate(bm, vec=(sx * 0.9 * side, 0, 0), verts=bm.verts)
        bm.to_mesh(w.data); bm.free()
        w.location = (root[0] * side, root[1], root[2])
        w.rotation_euler = (math.radians(-12), math.radians(-ang * side), 0)
        finish(w, wing_mat, 2)
        wings.append((w, side, w.rotation_euler.copy()))

# ---------- arms + legs ----------
arm_rot = {
    "idle": ((0, math.radians(28), 0), (0, math.radians(-28), 0)),
    "wave": ((0, math.radians(28), 0), (0, math.radians(-128), 0)),
    "carry": ((math.radians(-62), math.radians(-22), 0), (math.radians(-62), math.radians(22), 0)),
    "fly": ((math.radians(30), math.radians(30), 0), (math.radians(30), math.radians(-30), 0)),
}[POSE]
arms = []
for i, side in enumerate((-1, 1)):
    waving = POSE == "wave" and side == 1
    arms.append(limb((0.60 * side, -0.18 if waving else -0.10, 0.40), 0.62 if waving else 0.44, 0.13, head_mat, arm_rot[i]))
leg_x = math.radians(50) if POSE == "fly" else 0
for side in (-1, 1):
    limb((0.24 * side, -0.02, -0.38), 0.42, 0.13, dark_mat, (leg_x, math.radians(6 * side), 0))

if POSE == "carry":
    bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=0.40, depth=0.26, location=(0, -0.88, 0.02), rotation=(math.radians(90), 0, math.radians(30)))
    chunk = bpy.context.object
    bv = chunk.modifiers.new("bev", "BEVEL"); bv.width = 0.06; bv.segments = 5
    finish(chunk, honey_mat, 0)
    bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=0.16, depth=0.04, location=(0.0, -1.02, 0.02), rotation=(math.radians(90), 0, math.radians(30)))
    cell = bpy.context.object
    bv = cell.modifiers.new("bev", "BEVEL"); bv.width = 0.02; bv.segments = 3
    finish(cell, glyph_mat, 0)

# ---------- pose the whole rig (3/4 turn toward screen right) ----------
rig.rotation_euler = (math.radians(14 if POSE == "fly" else 0), 0, math.radians(13))

# ---------- lights: soft studio + warm rim ----------
def area(loc, energy, size, color, target=(0, 0, 1.0)):
    bpy.ops.object.light_add(type="AREA", location=loc)
    L = bpy.context.object; L.data.energy = energy; L.data.size = size; L.data.color = color
    L.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return L

area((-3.5, -5.0, 5.0), 700, 6.0, (1.0, 0.96, 0.90))     # key
area((4.5, -4.0, 2.0), 220, 6.0, (0.95, 0.92, 0.88))     # fill
area((0.5, 4.5, 4.5), 650, 3.5, (1.0, 0.72, 0.30))       # warm rim from behind
area((0.0, -3.0, -3.5), 90, 5.0, (1.0, 0.85, 0.6))       # bounce from below

# ---------- camera ----------
bpy.ops.object.camera_add(location=(0.0, -9.0, 2.2))
cam = bpy.context.object; sc.camera = cam
cam.data.lens = 82
tgt = Vector((0.0, 0.0, 0.95))
cam.rotation_euler = (tgt - cam.location).to_track_quat("-Z", "Y").to_euler()

# ---------- frames ----------
tmp = os.path.join(OUT, "_frames_" + POSE); os.makedirs(tmp, exist_ok=True)
frames = []
for f in range(FRAMES):
    ph = f / FRAMES * 2 * math.pi
    flap = math.sin(ph * 2)                       # two flutters per loop
    for (w, side, base) in wings:
        w.rotation_euler = (base.x, base.y - side * math.radians(16) * flap, base.z)
    if POSE == "wave":
        arms[1].rotation_euler = (0, math.radians(-128) + math.radians(22) * math.sin(ph), 0)
    p = os.path.join(tmp, "f%02d.png" % f)
    sc.render.filepath = p
    sc.render.image_settings.file_format = "PNG"; sc.render.image_settings.color_mode = "RGBA"
    bpy.ops.render.render(write_still=True)
    frames.append(p)

W, H = SIZE * FRAMES, SIZE
sheet = np.zeros((H, W, 4), dtype=np.float32)
for i, p in enumerate(frames):
    img = bpy.data.images.load(p)
    px = np.empty(SIZE * SIZE * 4, dtype=np.float32); img.pixels.foreach_get(px)
    sheet[:, i * SIZE:(i + 1) * SIZE, :] = px.reshape(SIZE, SIZE, 4)
    bpy.data.images.remove(img)
out_img = bpy.data.images.new("sheet", W, H, alpha=True)
out_img.pixels.foreach_set(sheet.ravel())
out_path = os.path.join(OUT, "bee-%s.webp" % POSE)
out_img.filepath_raw = out_path; out_img.file_format = "WEBP"
try:
    out_img.save(filepath=out_path, quality=88)
except TypeError:
    out_img.save()
print("WROTE", out_path)
