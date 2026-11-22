"""hive. mascot - design variants (preview only). Soft clay/vinyl-toy style.

  blender -b -P bee_variants.py -- <out_dir> --char A1|A2|A3|A4|A5 [--size 720]

A1 Bumble  - round chubby bee (head and body are one ball), open shiny eyes, 3 stripes, small round wings
A2 Zip     - slim flying bee seen from the side, happy closed eyes, long narrow wings, dotted flight trail
A3 Daisy   - big-head bee hugging a daisy, happy closed eyes, floating heart
A4 Honey   - sleepy/content bee holding a honey pot with a drip
A5 Byte    - tech worker bee with a headset and mic, open eyes, >_ badge on the belly, waving
Writes <out_dir>/<char>.png (transparent).
"""
import bpy, bmesh, math, os, sys
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = os.path.abspath(argv[0]) if argv else os.getcwd()
def arg(n, d): return argv[argv.index(n) + 1] if n in argv else d
CHAR = arg("--char", "A1"); SIZE = int(arg("--size", 720))
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
sc.render.resolution_x = sc.render.resolution_y = SIZE
sc.render.film_transparent = True
sc.view_settings.view_transform = "Standard"
for a, v in (("taa_render_samples", 64), ("use_raytracing", True)):
    try: setattr(sc.eevee, a, v)
    except Exception: pass
world = bpy.data.worlds.new("w"); sc.world = world; world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.045, 0.038, 0.030, 1)


def clay(name, rgb, rough=0.62, sss=0.18, sheen=0.25, emit=0.0, coat=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1); b.inputs["Roughness"].default_value = rough
    for k, v in (("Subsurface Weight", sss), ("Subsurface Scale", 0.04), ("Sheen Weight", sheen),
                 ("Specular IOR Level", 0.35), ("Coat Weight", coat)):
        try: b.inputs[k].default_value = v
        except Exception: pass
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1); b.inputs["Emission Strength"].default_value = emit
    return m


YEL = (1.0, 0.53, 0.040); DARK = (0.022, 0.018, 0.014); WHITE = (0.88, 0.86, 0.82)
M = {
    "yel": clay("yel", YEL), "dark": clay("dark", DARK, 0.55, 0, 0.35),
    "wing": clay("wing", WHITE, 0.5, 0.25, 0.2), "wingtech": clay("wingtech", (0.70, 0.80, 0.90), 0.45, 0.25, 0.2),
    "cheek": clay("cheek", (1.0, 0.30, 0.22), 0.7, 0.1), "eye": clay("eye", (0.004, 0.004, 0.004), 0.12, 0, 0, coat=1.0),
    "spark": clay("spark", (1, 1, 1), 0.3, 0, 0, emit=5.0), "honey": clay("honey", (1.0, 0.36, 0.012), 0.25, 0.3, 0, emit=0.35, coat=0.8),
    "glyph": clay("glyph", (1.0, 0.62, 0.06), 0.4, 0, 0, emit=2.0), "pink": clay("pink", (1.0, 0.25, 0.32), 0.55, 0.2),
    "petal": clay("petal", (0.95, 0.93, 0.86), 0.55, 0.25), "center": clay("center", (0.55, 0.20, 0.03), 0.7, 0.1),
    "green": clay("green", (0.10, 0.38, 0.08), 0.6, 0.1), "pot": clay("pot", (0.85, 0.36, 0.035), 0.55, 0.15),
    "potdark": clay("potdark", (0.45, 0.14, 0.01), 0.6, 0.1),
}


def striped(name, radius, axis, darks):
    """Body material: dark bands at normalised positions (u0, u1) in [-1, 1] along an object axis."""
    m = clay(name, YEL); nt = m.node_tree; b = nt.nodes["Principled BSDF"]
    tc = nt.nodes.new("ShaderNodeTexCoord"); sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    mr = nt.nodes.new("ShaderNodeMapRange"); mr.inputs["From Min"].default_value = -radius; mr.inputs["From Max"].default_value = radius
    ramp = nt.nodes.new("ShaderNodeValToRGB"); ramp.color_ramp.interpolation = "CONSTANT"
    stops = [(0.0, YEL)]
    for a, c in sorted(darks):
        stops += [((a + 1) / 2, DARK), ((c + 1) / 2, YEL)]
    if stops[1][0] <= 0.0:
        stops = stops[1:]; stops[0] = (0.0, DARK)
    els = ramp.color_ramp.elements
    els[0].position, els[0].color = stops[0][0], (*stops[0][1], 1)
    els[1].position, els[1].color = stops[1][0], (*stops[1][1], 1)
    for p, col in stops[2:]:
        e = els.new(p); e.color = (*col, 1)
    nt.links.new(tc.outputs["Object"], sep.inputs[0]); nt.links.new(sep.outputs[axis], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], ramp.inputs["Fac"]); nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
    return m


rig = bpy.data.objects.new("rig", None); bpy.context.collection.objects.link(rig)


def fin(o, mat, lv=2):
    if mat is not None: o.data.materials.append(mat)
    bpy.context.view_layer.objects.active = o; o.select_set(True)
    try: bpy.ops.object.shade_smooth()
    except Exception: pass
    o.select_set(False)
    if lv:
        s = o.modifiers.new("s", "SUBSURF"); s.levels = s.render_levels = lv
    o.parent = rig
    return o


def sphere(r, loc, mat, scale=(1, 1, 1), lv=2, seg=32, ring=16):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=loc, segments=seg, ring_count=ring)
    o = bpy.context.object; o.scale = scale; return fin(o, mat, lv)


def tube(pts, bev, mat, sharp=False):
    cu = bpy.data.curves.new("t", "CURVE"); cu.dimensions = "3D"; cu.bevel_depth = bev; cu.bevel_resolution = 6; cu.use_fill_caps = True
    sp = cu.splines.new("BEZIER"); sp.bezier_points.add(len(pts) - 1)
    for bp, p in zip(sp.bezier_points, pts):
        bp.co = p; bp.handle_left_type = bp.handle_right_type = "VECTOR" if sharp else "AUTO"
    o = bpy.data.objects.new("t", cu); bpy.context.collection.objects.link(o); cu.materials.append(mat); o.parent = rig
    return o


def capsule(root, length, radius, mat, rot):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, segments=24, ring_count=12)
    o = bpy.context.object; bm = bmesh.new(); bm.from_mesh(o.data)
    bmesh.ops.scale(bm, vec=(radius, radius, length / 2), verts=bm.verts)
    bmesh.ops.translate(bm, vec=(0, 0, -length / 2 + radius * 0.4), verts=bm.verts)
    bm.to_mesh(o.data); bm.free(); o.location = root; o.rotation_euler = rot
    return fin(o, mat)


def surf(C, R, x, z, out=0.0):
    """Point on the front (-Y) of an ellipsoid with center C and radii R."""
    dx, dz = (x - C.x) / R.x, (z - C.z) / R.z
    t = max(0.0, 1 - dx * dx - dz * dz)
    return Vector((x, C.y - R.y * math.sqrt(t) - out, z))


def face(C, R, eyes, ex=0.34, ez=None, scale=1.0, cheeks=True):
    ez = C.z + 0.10 * R.z / 0.88 if ez is None else ez
    for s in (-1, 1):
        x = C.x + ex * s * scale
        w = 0.14 * scale
        if eyes == "happy":
            tube([surf(C, R, x - w, ez - 0.03 * scale, .01), surf(C, R, x, ez + 0.07 * scale, .01), surf(C, R, x + w, ez - 0.03 * scale, .01)], 0.04 * scale, M["dark"])
        elif eyes == "sleepy":
            tube([surf(C, R, x - w, ez + 0.03 * scale, .01), surf(C, R, x, ez - 0.05 * scale, .01), surf(C, R, x + w, ez + 0.03 * scale, .01)], 0.04 * scale, M["dark"])
        else:  # open, shiny
            p = surf(C, R, x, ez, -0.02)
            sphere(0.13 * scale, p, M["eye"], scale=(1, 0.45, 1.3))
            sphere(0.042 * scale, p + Vector((0.035 * scale, -0.06 * scale, 0.07 * scale)), M["spark"], lv=0)
            sphere(0.02 * scale, p + Vector((-0.04 * scale, -0.06 * scale, -0.06 * scale)), M["spark"], lv=0)
        if cheeks:
            sphere(0.13 * scale, surf(C, R, C.x + 0.56 * s * scale, ez - 0.28 * scale, -0.03), M["cheek"], scale=(1, 0.32, 0.7), lv=1)
    my = ez - 0.25 * scale
    tube([surf(C, R, C.x - 0.12 * scale, my + 0.03 * scale), surf(C, R, C.x, my - 0.04 * scale), surf(C, R, C.x + 0.12 * scale, my + 0.03 * scale)], 0.033 * scale, M["dark"])


def antennae(base_z, spread=0.26, height=0.5, tip=0.11, y=-0.05):
    for s in (-1, 1):
        tube([Vector((spread * s, y, base_z)), Vector(((spread + 0.14) * s, y - 0.07, base_z + height * 0.7)), Vector(((spread + 0.36) * s, y - 0.13, base_z + height))], 0.035, M["dark"])
        sphere(tip, ((spread + 0.38) * s, y - 0.13, base_z + height + 0.01), M["dark"])


def wing(root, sx, sz, ang, side, mat, tilt=-12):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=1, segments=32, ring_count=16)
    w = bpy.context.object; bm = bmesh.new(); bm.from_mesh(w.data)
    bmesh.ops.scale(bm, vec=(sx, 0.07, sz), verts=bm.verts)
    bmesh.ops.translate(bm, vec=(sx * 0.9 * side, 0, 0), verts=bm.verts)
    bm.to_mesh(w.data); bm.free()
    w.location = (root[0] * side, root[1], root[2]); w.rotation_euler = (math.radians(tilt), math.radians(-ang * side), 0)
    return fin(w, mat)


def hexbadge(center, r, glyph=True):
    bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=r, depth=0.06, location=center, rotation=(math.radians(90), math.radians(30), 0))
    b = bpy.context.object; bv = b.modifiers.new("b", "BEVEL"); bv.width = 0.02; bv.segments = 3; fin(b, M["dark"], 0)
    if glyph:
        k = r / 0.25; y = center[1] - 0.038; cx, cz = center[0], center[2]
        tube([Vector((cx - .13 * k, y, cz + .085 * k)), Vector((cx - .045 * k, y, cz + .01 * k))], .026 * k, M["glyph"], True)
        tube([Vector((cx - .045 * k, y, cz + .01 * k)), Vector((cx - .13 * k, y, cz - .065 * k))], .026 * k, M["glyph"], True)
        tube([Vector((cx - .005 * k, y, cz - .065 * k)), Vector((cx + .12 * k, y, cz - .065 * k))], .026 * k, M["glyph"], True)


def heart(loc, size, mat):
    for s in (-1, 1):
        sphere(0.16 * size, (loc[0] + 0.11 * size * s, loc[1], loc[2]), mat, lv=1)
    bpy.ops.mesh.primitive_cone_add(radius1=0.255 * size, depth=0.36 * size, location=(loc[0], loc[1], loc[2] - 0.17 * size), rotation=(math.radians(180), 0, 0), vertices=32)
    c = bpy.context.object; c.scale = (1, 0.62, 1); fin(c, mat, 1)


def standard_bee(eyes, wings_mat="wing", stripes=((-0.66, -0.25), (0.25, 0.48)), arms="idle"):
    HC, HR = Vector((0, 0, 1.30)), Vector((0.98, 0.90, 0.88))
    sphere(1.0, HC, M["yel"], scale=tuple(HR), seg=48, ring=24)
    face(HC, HR, eyes)
    antennae(2.08)
    body = sphere(0.68, (0, 0.10, 0.12), striped("body", 0.68 * 1.0, "Z", stripes), scale=(1, 0.95, 1.05), seg=48, ring=24)
    for s in (-1, 1):
        wing((0.40, 0.50, 0.92), 0.56, 0.38, 42, s, M[wings_mat]); wing((0.40, 0.55, 0.56), 0.40, 0.27, 2, s, M[wings_mat])
        capsule((0.24 * s, -0.02, -0.38), 0.42, 0.13, M["dark"], (0, math.radians(6 * s), 0))
    rots = {"idle": [(0, math.radians(28), 0), (0, math.radians(-28), 0)],
            "hug": [(math.radians(-70), math.radians(-30), 0), (math.radians(-70), math.radians(30), 0)],
            "wave": [(0, math.radians(28), 0), (0, math.radians(-128), 0)]}[arms]
    for i, s in enumerate((-1, 1)):
        waving = arms == "wave" and s == 1
        capsule((0.60 * s, -0.18 if waving else -0.10, 0.40), 0.62 if waving else 0.44, 0.13, M["yel"], rots[i])
    return HC, HR


cam_loc, cam_tgt, cam_lens, yaw = (0.0, -9.0, 2.2), (0.0, 0.0, 0.95), 82, 13

if CHAR == "A1":  # Bumble: one round ball, open eyes, 3 stripes
    C, R = Vector((0, 0, 0.95)), Vector((1.18, 1.08, 1.12))
    sphere(1.0, C, striped("body", 1.0, "Z", ((-0.86, -0.66), (-0.5, -0.3), (-0.14, 0.02))), scale=tuple(R), seg=48, ring=24)
    face(C, R, "open", ex=0.36, ez=1.30, scale=1.0)
    antennae(2.0, spread=0.22, height=0.42)
    for s in (-1, 1):
        wing((0.55, 0.45, 1.55), 0.42, 0.36, 30, s, M["wing"])
        capsule((1.02 * s, -0.25, 0.85), 0.36, 0.13, M["yel"], (0, math.radians(40 * s), 0))
        capsule((0.32 * s, -0.25, -0.05), 0.30, 0.15, M["dark"], (0, 0, 0))
    bpy.ops.mesh.primitive_cone_add(radius1=0.13, depth=0.3, location=(0, 1.12, 0.6), rotation=(math.radians(-90), 0, 0), vertices=24); fin(bpy.context.object, M["dark"], 1)
    cam_tgt = (0, 0, 1.05); yaw = 10

elif CHAR == "A2":  # Zip: slim flyer, side view, long wings, dotted trail
    body = sphere(0.62, (-0.55, 0, 0.95), striped("body", 0.62, "X", ((-1.0, -0.72), (-0.42, -0.14), (0.16, 0.42))), scale=(1.6, 0.92, 0.95), seg=48, ring=24)
    HC, HR = Vector((0.72, 0, 1.18)), Vector((0.62, 0.60, 0.60))
    sphere(1.0, HC, M["yel"], scale=tuple(HR), seg=48, ring=24)
    # face on the side facing the camera (-Y), looking forward (+X)
    for k, dx in enumerate((0.08, 0.36)):
        p = Vector((HC.x + dx, HC.y - math.sqrt(max(0, 1 - (dx / HR.x) ** 2 - 0.02)) * HR.y - 0.005, HC.z + 0.12))
        tube([p + Vector((-0.08, 0, -0.02)), p + Vector((0, -0.01, 0.05)), p + Vector((0.08, 0, -0.02))], 0.028, M["dark"])
    sphere(0.09, (HC.x + 0.05, HC.y - 0.55, HC.z - 0.1), M["cheek"], scale=(1, 0.3, 0.7), lv=1)
    tube([Vector((HC.x + 0.22, -0.56, 0.98)), Vector((HC.x + 0.30, -0.575, 0.94)), Vector((HC.x + 0.38, -0.555, 0.985))], 0.024, M["dark"])
    for s in (-1, 1):
        tube([Vector((HC.x + 0.15, 0.18 * s, HC.z + 0.5)), Vector((HC.x + 0.35, 0.25 * s, HC.z + 0.85)), Vector((HC.x + 0.62, 0.30 * s, HC.z + 0.98))], 0.03, M["dark"])
        sphere(0.085, (HC.x + 0.64, 0.30 * s, HC.z + 0.99), M["dark"])
    for (y, ang, size) in ((-0.25, -22, 1.0), (0.25, 34, 0.85)):
        bpy.ops.mesh.primitive_uv_sphere_add(radius=1, segments=32, ring_count=16)
        w = bpy.context.object; bm = bmesh.new(); bm.from_mesh(w.data)
        bmesh.ops.scale(bm, vec=(0.26 * size, 0.05, 0.85 * size), verts=bm.verts); bmesh.ops.translate(bm, vec=(0, 0, 0.8 * size), verts=bm.verts)
        bm.to_mesh(w.data); bm.free(); w.location = (-0.25, y, 1.42); w.rotation_euler = (math.radians(ang), math.radians(-28), 0); fin(w, M["wing"])
    for i in range(3):
        capsule((-0.2 - 0.32 * i, -0.15, 0.48), 0.38, 0.07, M["dark"], (0, math.radians(35 + 6 * i), 0))
    bpy.ops.mesh.primitive_cone_add(radius1=0.12, depth=0.32, location=(-1.6, 0, 0.9), rotation=(0, math.radians(-90), 0), vertices=24); fin(bpy.context.object, M["dark"], 1)
    for i in range(7):  # dotted flight trail
        t = i / 6
        sphere(0.085 * (1 - 0.55 * t), (-1.85 - 0.36 * i, 0.0, 0.85 + 0.45 * math.sin(t * math.pi * 1.2)), M["honey"], lv=1)
    cam_loc, cam_tgt, cam_lens, yaw = (1.2, -9.5, 3.4), (-0.75, 0, 1.25), 74, -8

elif CHAR == "A3":  # Daisy: hugging a flower, happy eyes, heart
    standard_bee("happy", arms="hug")
    fc = Vector((0.0, -0.98, 0.18))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.22, depth=0.14, location=fc, rotation=(math.radians(90), 0, 0), vertices=32)
    cbody = bpy.context.object; bv = cbody.modifiers.new("b", "BEVEL"); bv.width = 0.06; bv.segments = 4; fin(cbody, M["center"], 1)
    for i in range(12):
        a = i / 12 * 2 * math.pi
        p = fc + Vector((math.cos(a) * 0.42, 0.04, math.sin(a) * 0.42))
        o = sphere(0.26, p, M["petal"], scale=(1, 0.28, 0.42), lv=1); o.rotation_euler = (0, -a, 0)
    tube([fc + Vector((0.0, 0.05, -0.2)), Vector((0.05, -0.85, -0.45)), Vector((0.12, -0.75, -0.85))], 0.05, M["green"])
    leaf = sphere(0.22, (0.28, -0.8, -0.6), M["green"], scale=(1, 0.25, 0.45), lv=1); leaf.rotation_euler = (0, math.radians(-35), 0)
    heart((-1.2, -0.3, 2.35), 0.9, M["pink"]); heart((1.25, -0.2, 2.0), 0.55, M["pink"])
    cam_tgt = (0, 0, 0.95)

elif CHAR == "A4":  # Honey: sleepy, holding a honey pot
    standard_bee("sleepy", arms="hug")
    pc = Vector((0.0, -0.98, -0.05))
    sphere(0.55, pc, M["pot"], scale=(1, 0.95, 0.88), seg=48, ring=24)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.53, minor_radius=0.045, location=(pc.x, pc.y, pc.z - 0.06)); fin(bpy.context.object, M["potdark"], 1)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.36, depth=0.16, location=(pc.x, pc.y, pc.z + 0.47), vertices=40); fin(bpy.context.object, M["pot"], 1)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.38, minor_radius=0.08, location=(pc.x, pc.y, pc.z + 0.56)); fin(bpy.context.object, M["pot"], 1)
    sphere(0.36, (pc.x, pc.y, pc.z + 0.58), M["honey"], scale=(1, 1, 0.38), lv=2)
    capsule((pc.x - 0.22, pc.y - 0.3, pc.z + 0.55), 0.34, 0.07, M["honey"], (math.radians(-12), 0, 0))
    sphere(0.085, (pc.x - 0.22, pc.y - 0.37, pc.z + 0.2), M["honey"], lv=1)
    cam_tgt = (0, 0, 0.9)

elif CHAR == "A5":  # Byte: tech worker bee, headset, open eyes, >_ badge, waving
    HC, HR = standard_bee("open", wings_mat="wingtech", stripes=((-0.70, -0.52), (-0.32, -0.16), (0.30, 0.46)), arms="wave")
    hexbadge((0, -0.64, 0.08), 0.24)
    tube([Vector((-1.0, 0.02, 1.28)), Vector((-0.8, 0.02, 1.95)), Vector((0, 0.02, 2.26)), Vector((0.8, 0.02, 1.95)), Vector((1.0, 0.02, 1.28))], 0.06, M["dark"])
    for s in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.27, depth=0.18, location=(1.0 * s, 0.0, 1.26), rotation=(0, math.radians(90), 0), vertices=40)
        cup = bpy.context.object; bv = cup.modifiers.new("b", "BEVEL"); bv.width = 0.06; bv.segments = 4; fin(cup, M["dark"], 1)
        bpy.ops.mesh.primitive_torus_add(major_radius=0.17, minor_radius=0.035, location=(1.10 * s, 0.0, 1.26), rotation=(0, math.radians(90), 0)); fin(bpy.context.object, M["glyph"], 1)
    tube([Vector((-1.0, -0.18, 1.12)), Vector((-0.78, -0.72, 0.92)), Vector((-0.32, -0.92, 0.94))], 0.03, M["dark"])
    sphere(0.075, (-0.28, -0.93, 0.95), M["glyph"], lv=1)
    cam_tgt = (0, 0, 1.0)

rig.rotation_euler = (0, 0, math.radians(yaw))


def area(loc, energy, size, color, target=(0, 0, 1.0)):
    bpy.ops.object.light_add(type="AREA", location=loc)
    L = bpy.context.object; L.data.energy = energy; L.data.size = size; L.data.color = color
    L.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()

area((-3.5, -5.0, 5.0), 700, 6.0, (1.0, 0.96, 0.90)); area((4.5, -4.0, 2.0), 220, 6.0, (0.95, 0.92, 0.88))
area((0.5, 4.5, 4.5), 650, 3.5, (1.0, 0.72, 0.30)); area((0.0, -3.0, -3.5), 90, 5.0, (1.0, 0.85, 0.6))
bpy.ops.object.camera_add(location=cam_loc); cam = bpy.context.object; sc.camera = cam; cam.data.lens = cam_lens
cam.rotation_euler = (Vector(cam_tgt) - cam.location).to_track_quat("-Z", "Y").to_euler()
sc.render.filepath = os.path.join(OUT, CHAR + ".png")
sc.render.image_settings.file_format = "PNG"; sc.render.image_settings.color_mode = "RGBA"
bpy.ops.render.render(write_still=True)
print("WROTE", sc.render.filepath)
