"""Writes swarm-bg.svg (dark hex-prism field in the hero material) and char.html."""
import math, os, random
from gen import OUT, hexpts, poly, P, half

random.seed(7)
GLOW = "#FFE27A"


def swarm_bg(W=1200, H=520, r=42):
    sy = .72
    dx, dy = 1.5 * r, math.sqrt(3) * r * sy
    cells = []
    for col in range(-1, int(W / dx) + 2):
        for row in range(-1, int(H / dy) + 3):
            cx = col * dx
            cy = row * dy + (dy / 2 if col % 2 else 0)
            cells.append((cy, cx))
    cells.sort()
    honey = {(5, 3), (11, 6)}
    seams = {(4, 3), (6, 3), (5, 2), (5, 4), (10, 6), (12, 6), (11, 5), (8, 2), (14, 4), (2, 5)}
    body = []
    for cy, cx in cells:
        col = round(cx / dx)
        row = round((cy - (dy / 2 if col % 2 else 0)) / dy)
        lift = random.uniform(-3, 3)
        top = hexpts(cx, cy + lift, r * .9, flat=True, sy=sy)
        depth = 18 + random.uniform(0, 8)
        key = (col, row)
        if key in seams or key in honey:
            body.append(poly(hexpts(cx, cy + lift + 6, r * 1.02, flat=True, sy=sy), "#FFC93C",
                             ' filter="url(#sb)" opacity=".55"'))
        if key in honey:
            cols = ("#F5A623", "#D9971F", "#9A6510")
            tc = ("#FFC93C", GLOW)
        else:
            t = min(1, max(0, (cx / W) * .7 + (1 - cy / H) * .3))
            tc = (mix("#17150D", "#2A2618", t * t), mix("#1C1A10", "#3A3520", t * t))
            cols = ("#12100A", "#0E0D08", "#0B0A07")
        for i, c in zip(range(3), cols):
            p, q = top[i], top[i + 1]
            body.append(poly([p, q, (q[0], q[1] + depth), (p[0], p[1] + depth)], c))
        mid = cx
        body.append(poly(half(top, mid, True), tc[0]))
        body.append(poly(half(top, mid, False), tc[1]))
        if key in seams:
            body.append(f'<polyline points="{P(top[3:] + top[:2])}" fill="none" stroke="{GLOW}" stroke-width="1.2" opacity=".75"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" preserveAspectRatio="xMidYMid slice">\n'
           f'<title>hive hero-material hex field</title>\n'
           f'<defs><filter id="sb" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="7"/></filter>'
           f'<linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0B0A08" stop-opacity=".7"/>'
           f'<stop offset=".5" stop-color="#0B0A08" stop-opacity=".15"/><stop offset="1" stop-color="#0B0A08" stop-opacity=".6"/></linearGradient></defs>\n'
           f'<rect width="{W}" height="{H}" fill="#0B0A08"/>\n' + "\n".join(body) +
           f'\n<rect width="{W}" height="{H}" fill="url(#fade)"/>\n</svg>\n')
    with open(os.path.join(OUT, "swarm-bg.svg"), "w", encoding="utf-8") as f:
        f.write(svg)


def mix(a, b, t):
    a = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


TAKES = [("c1", "C1 Buzz", "the P1 courier grown into a character: bigger head module, 3/4 visor face, belly jets"),
         ("c2", "C2 Chibi", "head as big as the body: the most mascot, softest expressions, twin hover jets"),
         ("c3", "C3 Helm", "taller helmet with a wide visor band, longer body, rear jets for forward flight"),
         ("c4", "C4 Ace", "sleek, low brow on the visor for quiet confidence, stinger becomes the main jet")]

# swarm: (left %, top %, width px, rotate deg, flip, opacity, pose)
SWARM = [(6, 14, 70, -6, 0, .55, ""), (17, 52, 128, 4, 0, 1, "-carry"), (31, 8, 46, -10, 1, .45, ""),
         (40, 40, 92, -3, 1, .85, "-happy"), (52, 66, 58, 8, 0, .6, "-carry"), (58, 16, 150, -5, 0, 1, ""),
         (71, 54, 76, 6, 1, .7, "-think"), (80, 22, 52, -8, 0, .5, "-carry"), (86, 60, 104, -2, 1, .9, ""),
         (93, 10, 40, 10, 1, .4, "")]


def html():
    cards = []
    for cid, name, line in TAKES:
        cards.append(f'''  <section class="card">
    <div class="hero"><div class="big"><img src="{cid}.svg" alt="{name}"><small>neutral</small></div>
      <div class="poses"><div class="pose"><img src="{cid}-carry.svg" alt=""><small>carry</small></div>
      <div class="pose"><img src="{cid}-happy.svg" alt=""><small>happy</small></div>
      <div class="pose"><img src="{cid}-think.svg" alt=""><small>thinking</small></div></div></div>
    <div class="row"><img class="m" src="{cid}.svg" alt=""><img class="s" src="{cid}.svg" alt=""><span class="sep"></span><img class="m" src="{cid}-carry.svg" alt=""><img class="s" src="{cid}-carry.svg" alt=""><small>64 · 32</small></div>
    <div class="name"><b>{name}</b><span>{line}</span></div>
  </section>''')
    radios, labels, layers, rules = [], [], [], []
    for i, (cid, name, _) in enumerate(TAKES):
        radios.append(f'<input type="radio" name="take" id="t{i}"{" checked" if i == 0 else ""}>')
        labels.append(f'<label for="t{i}">{name}</label>')
        imgs = "".join(
            f'<img src="{cid}{pose}.svg" alt="" style="left:{l}%;top:{t}%;width:{w / 12:.2f}%;opacity:{o};'
            f'transform:translate(-50%,0) rotate({rot}deg){" scaleX(-1)" if fl else ""}{";filter:blur(.6px)" if w < 50 else ""}">'
            for (l, t, w, rot, fl, o, pose) in SWARM)
        layers.append(f'<div class="layer l{i}">{imgs}</div>')
        rules.append(f'#t{i}:checked ~ .stage .l{i} {{ display:block; }} #t{i}:checked ~ .tabs label[for=t{i}] {{ color:var(--bg); background:var(--honey); }}')
    doc = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hive. assistant characters</title>
<style>
  :root {{ --bg:#0B0A08; --card:#16140F; --line:#2A251C; --honey:#F5A623; --white:#F2EDE4; --muted:#A8A091; }}
  * {{ box-sizing:border-box; margin:0; }}
  body {{ background:var(--bg); color:var(--white); font:14px/1.45 ui-sans-serif, "Segoe UI", system-ui, sans-serif; padding:40px 32px 56px; }}
  header {{ display:flex; align-items:baseline; gap:14px; margin-bottom:24px; flex-wrap:wrap; }}
  h1 {{ font-size:22px; font-weight:700; letter-spacing:-.01em; }}
  h1 span, h2 span {{ color:var(--honey); }}
  h2 {{ font-size:16px; font-weight:700; margin:0 0 12px; }}
  header p, .note {{ color:var(--muted); }}
  .swarm {{ margin-bottom:28px; }}
  .swarm input {{ position:absolute; opacity:0; pointer-events:none; }}
  .tabs {{ display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap; align-items:center; }}
  .tabs label {{ cursor:pointer; padding:4px 10px; border:1px solid var(--line); border-radius:4px; color:var(--muted); font-size:12px; }}
  .stage {{ position:relative; width:100%; aspect-ratio:1200/520; border-radius:6px; overflow:hidden; border:1px solid var(--line);
           background:#0B0A08 url(swarm-bg.svg) center/cover no-repeat; }}
  .layer {{ display:none; position:absolute; inset:0; }}
  .layer img {{ position:absolute; height:auto; }}
  {" ".join(rules)}
  .grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:20px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:20px; display:flex; flex-direction:column; gap:14px; }}
  .hero {{ display:grid; grid-template-columns:2fr 1fr; gap:10px; }}
  .big, .pose {{ background:var(--bg); border-radius:4px; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:8px 0; gap:2px; }}
  .big img {{ width:300px; height:300px; max-width:100%; }}
  .poses {{ display:grid; grid-template-rows:repeat(3, 1fr); gap:10px; }}
  .pose img {{ width:96px; height:96px; }}
  small {{ color:var(--muted); font-size:12px; }}
  .row {{ display:flex; align-items:center; gap:16px; background:var(--bg); border-radius:4px; padding:10px 16px; }}
  .row img.m {{ width:64px; height:64px; }}
  .row img.s {{ width:32px; height:32px; }}
  .row .sep {{ width:1px; height:40px; background:var(--line); margin:0 6px; }}
  .row small {{ margin-left:auto; }}
  .name {{ display:flex; gap:10px; align-items:baseline; }}
  .name b {{ color:var(--honey); font-weight:700; white-space:nowrap; }}
  .name span {{ color:var(--muted); }}
  @media (max-width:1200px) {{ .grid {{ grid-template-columns:1fr; }} }}
  @media (max-width:720px) {{ body {{ padding:24px 16px; }} .hero {{ grid-template-columns:1fr; }} .poses {{ grid-template-columns:repeat(3,1fr); grid-template-rows:none; }} }}
</style>
</head>
<body>
<header><h1>hive<span>.</span> assistant characters</h1><p>Four character takes on P1, cut from the hero's material: bronze hex prisms lit by honey light from inside the seams</p></header>
<section class="swarm">
  <h2>Swarm test <span>·</span> <span class="note" style="font-weight:400">ten couriers over the hero material, mixed sizes, angles and poses</span></h2>
  {"".join(radios)}
  <div class="tabs">{"".join(labels)}</div>
  <div class="stage">{"".join(layers)}</div>
</section>
<main class="grid">
{chr(10).join(cards)}
</main>
</body>
</html>
'''
    with open(os.path.join(OUT, "char.html"), "w", encoding="utf-8") as f:
        f.write(doc)


if __name__ == "__main__":
    swarm_bg()
    html()
    print("ok")
