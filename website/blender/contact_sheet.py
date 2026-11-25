"""Compose transparent PNGs side by side on the brand background (for design review).

  blender -b -P contact_sheet.py -- <out.png> <img1.png> <img2.png> ...
"""
import bpy, sys
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:]
out, paths = argv[0], argv[1:]
BG = np.array([0x0B, 0x0A, 0x08], dtype=np.float32) / 255.0  # #0B0A08 (sRGB values, byte image)
imgs = []
for p in paths:
    im = bpy.data.images.load(p)
    w, h = im.size
    px = np.empty(w * h * 4, dtype=np.float32); im.pixels.foreach_get(px)
    imgs.append(px.reshape(h, w, 4))
H = max(i.shape[0] for i in imgs)
W = sum(i.shape[1] for i in imgs)
sheet = np.zeros((H, W, 4), dtype=np.float32)
sheet[..., :3] = BG; sheet[..., 3] = 1
x = 0
for i in imgs:
    h, w = i.shape[:2]
    a = i[..., 3:4]
    sheet[:h, x:x + w, :3] = i[..., :3] * a + BG * (1 - a)
    x += w
o = bpy.data.images.new("sheet", W, H, alpha=True)
o.pixels.foreach_set(sheet.ravel())
o.filepath_raw = out; o.file_format = "PNG"; o.save()
print("WROTE", out)
