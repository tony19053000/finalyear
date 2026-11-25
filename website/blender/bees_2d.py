"""hive. mascot - flat 2D cartoon variants (preview only), drawn as plain SVG.

  python bees_2d.py <out_dir>

Style: thick dark outlines, flat warm yellow, one shade tone, white wings, happy faces,
rosy cheeks, hearts and a dotted flight trail. Same characters as the 3D set where possible:
B1 Bumble (round, open eyes)   B2 Zip (flying, side view, trail)   B3 Daisy (sunflower hug)
B4 Honey (sleepy, honey pot)   B5 Byte (headset, open eyes)        B6 hive (classic mascot, waving, >_ badge)
"""
import os, sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(OUT, exist_ok=True)

INK, Y, YS, W, WS, CHEEK, PINK, HONEY, ORANGE = "#2A1C0F", "#FFC93C", "#F2A12E", "#FFFFFF", "#E4ECF2", "#FF9E8A", "#FF6F8E", "#F5A623", "#FF9F1C"
ST = f'stroke="{INK}" stroke-linejoin="round" stroke-linecap="round" stroke-width="5"'
def STW(w): return ST.replace('stroke-width="5"', f'stroke-width="{w}"')


def svg(body, defs=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" width="480" height="480">'
            f'<defs>{defs}</defs>{body}</svg>\n')


def heart(x, y, s=1.0, fill=PINK):
    return (f'<path transform="translate({x} {y}) scale({s})" d="M0 6 C-2 -2 -14 -2 -14 7 C-14 15 -4 20 0 25 C4 20 14 15 14 7 C14 -2 2 -2 0 6Z" '
            f'fill="{fill}" {STW(round(4 / s, 2))}/>')


def eyes(kind, lx, rx, y, size=1.0):
    if kind == "happy":
        return "".join(f'<path d="M{x - 10 * size} {y} q{10 * size} {-12 * size} {20 * size} 0" fill="none" {ST}/>' for x in (lx, rx))
    if kind == "sleepy":
        return "".join(f'<path d="M{x - 10 * size} {y - 3} q{10 * size} {11 * size} {20 * size} 0" fill="none" {ST}/>' for x in (lx, rx))
    return "".join(f'<ellipse cx="{x}" cy="{y}" rx="{10 * size}" ry="{13 * size}" fill="{INK}"/>'
                   f'<circle cx="{x + 3.5 * size}" cy="{y - 5 * size}" r="{4 * size}" fill="#fff"/>'
                   f'<circle cx="{x - 3 * size}" cy="{y + 5 * size}" r="{1.8 * size}" fill="#fff"/>' for x in (lx, rx))


def bighead(eyes_kind="happy", arms="down", extra_front="", extra_back="", badge=False, over_head=""):
    """Big-head bee facing the viewer (the hive mascot proportions)."""
    defs = ('<clipPath id="bd"><ellipse cx="120" cy="176" rx="44" ry="40"/></clipPath>'
            '<clipPath id="hd"><circle cx="120" cy="98" r="54"/></clipPath>')
    b = extra_back
    # wings
    b += f'<ellipse cx="60" cy="118" rx="34" ry="23" transform="rotate(-28 60 118)" fill="{W}" {ST}/>'
    b += f'<ellipse cx="180" cy="118" rx="34" ry="23" transform="rotate(28 180 118)" fill="{W}" {ST}/>'
    b += f'<ellipse cx="70" cy="150" rx="22" ry="15" transform="rotate(12 70 150)" fill="{W}" {ST}/>'
    b += f'<ellipse cx="170" cy="150" rx="22" ry="15" transform="rotate(-12 170 150)" fill="{W}" {ST}/>'
    # antennae
    b += f'<path d="M104 50 Q96 28 80 22" fill="none" {ST}/><circle cx="78" cy="21" r="7.5" fill="{INK}"/>'
    b += f'<path d="M136 50 Q144 28 160 22" fill="none" {ST}/><circle cx="162" cy="21" r="7.5" fill="{INK}"/>'
    # feet
    b += f'<ellipse cx="104" cy="216" rx="11" ry="8" fill="{INK}"/><ellipse cx="136" cy="216" rx="11" ry="8" fill="{INK}"/>'
    # body
    b += f'<ellipse cx="120" cy="176" rx="44" ry="40" fill="{Y}"/>'
    b += (f'<g clip-path="url(#bd)"><ellipse cx="134" cy="186" rx="44" ry="40" fill="{YS}"/><ellipse cx="116" cy="170" rx="44" ry="40" fill="{Y}"/>'
          f'<rect x="70" y="160" width="100" height="13" fill="{INK}"/><rect x="70" y="190" width="100" height="14" fill="{INK}"/></g>')
    b += f'<ellipse cx="120" cy="176" rx="44" ry="40" fill="none" {ST}/>'
    if badge:
        b += (f'<polygon points="120,165 133,172.5 133,187.5 120,195 107,187.5 107,172.5" fill="{INK}"/>'
              f'<path d="M112 175 l5 5 l-5 5 M120 186 h8" fill="none" stroke="{Y}" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>')
    # arms
    if arms == "down":
        b += f'<ellipse cx="80" cy="176" rx="10" ry="15" transform="rotate(24 80 176)" fill="{Y}" {ST}/><ellipse cx="160" cy="176" rx="10" ry="15" transform="rotate(-24 160 176)" fill="{Y}" {ST}/>'
    elif arms == "wave":
        b += f'<ellipse cx="80" cy="176" rx="10" ry="15" transform="rotate(24 80 176)" fill="{Y}" {ST}/><ellipse cx="170" cy="148" rx="10" ry="17" transform="rotate(-38 170 148)" fill="{Y}" {ST}/>'
    # head
    b += f'<circle cx="120" cy="98" r="54" fill="{Y}"/>'
    b += f'<g clip-path="url(#hd)"><circle cx="134" cy="110" r="54" fill="{YS}"/><circle cx="116" cy="92" r="54" fill="{Y}"/></g>'
    b += f'<circle cx="120" cy="98" r="54" fill="none" {ST}/>'
    b += f'<ellipse cx="94" cy="66" rx="12" ry="7" transform="rotate(-30 94 66)" fill="#fff" opacity=".55"/>'
    b += eyes(eyes_kind, 98, 142, 100)
    b += f'<ellipse cx="84" cy="116" rx="9" ry="6" fill="{CHEEK}"/><ellipse cx="156" cy="116" rx="9" ry="6" fill="{CHEEK}"/>'
    b += f'<path d="M111 118 q9 9 18 0" fill="none" {ST}/>'
    b += over_head + extra_front
    if arms == "hug":
        b += f'<ellipse cx="86" cy="178" rx="10" ry="15" transform="rotate(-30 86 178)" fill="{Y}" {ST}/><ellipse cx="154" cy="178" rx="10" ry="15" transform="rotate(30 154 178)" fill="{Y}" {ST}/>'
    return svg(b, defs)


files = {}

# B1 Bumble: one round ball, open shiny eyes, three stripes
d = '<clipPath id="bb"><circle cx="120" cy="132" r="74"/></clipPath>'
b = f'<ellipse cx="78" cy="62" rx="27" ry="20" transform="rotate(-24 78 62)" fill="{W}" {ST}/><ellipse cx="162" cy="62" rx="27" ry="20" transform="rotate(24 162 62)" fill="{W}" {ST}/>'
b += f'<path d="M104 62 Q94 36 80 30" fill="none" {ST}/><circle cx="78" cy="29" r="7.5" fill="{INK}"/><path d="M136 62 Q146 36 160 30" fill="none" {ST}/><circle cx="162" cy="29" r="7.5" fill="{INK}"/>'
b += f'<ellipse cx="100" cy="210" rx="12" ry="8" fill="{INK}"/><ellipse cx="140" cy="210" rx="12" ry="8" fill="{INK}"/>'
b += f'<ellipse cx="48" cy="140" rx="11" ry="16" transform="rotate(30 48 140)" fill="{Y}" {ST}/><ellipse cx="192" cy="140" rx="11" ry="16" transform="rotate(-30 192 140)" fill="{Y}" {ST}/>'
b += f'<circle cx="120" cy="132" r="74" fill="{Y}"/>'
b += (f'<g clip-path="url(#bb)"><circle cx="138" cy="150" r="74" fill="{YS}"/><circle cx="114" cy="124" r="72" fill="{Y}"/>'
      f'<rect x="40" y="152" width="160" height="15" fill="{INK}"/><rect x="40" y="180" width="160" height="15" fill="{INK}"/><rect x="40" y="208" width="160" height="15" fill="{INK}"/></g>')
b += f'<circle cx="120" cy="132" r="74" fill="none" {ST}/><ellipse cx="86" cy="86" rx="14" ry="8" transform="rotate(-30 86 86)" fill="#fff" opacity=".55"/>'
b += eyes("open", 98, 142, 114) + f'<ellipse cx="80" cy="134" rx="9" ry="6" fill="{CHEEK}"/><ellipse cx="160" cy="134" rx="9" ry="6" fill="{CHEEK}"/>'
b += f'<path d="M110 132 q5 6 10 0 q5 6 10 0" fill="none" {ST}/>'
files["B1"] = svg(b, d)

# B2 Zip: flying side view with a dotted trail and a heart
d = '<clipPath id="zb"><ellipse cx="110" cy="142" rx="52" ry="34"/></clipPath><clipPath id="zh"><circle cx="172" cy="128" r="35"/></clipPath>'
b = f'<path d="M14 168 C 34 128, 52 186, 74 150" fill="none" stroke="{INK}" stroke-width="5" stroke-linecap="round" stroke-dasharray="1 12"/>'
b += heart(40, 104, 0.9)
b += f'<ellipse cx="112" cy="84" rx="18" ry="36" transform="rotate(-20 112 84)" fill="{WS}" {ST}/>'
b += f'<path d="M64 136 L46 142 L64 149 Z" fill="{INK}" {ST}/>'
b += f'<path d="M96 170 l-6 14 M114 174 l-4 14 M132 172 l-2 14" fill="none" {ST}/>'
b += f'<ellipse cx="110" cy="142" rx="52" ry="34" fill="{Y}"/>'
b += (f'<g clip-path="url(#zb)"><ellipse cx="118" cy="152" rx="52" ry="34" fill="{YS}"/><ellipse cx="106" cy="136" rx="52" ry="33" fill="{Y}"/>'
      f'<rect x="78" y="100" width="13" height="90" fill="{INK}"/><rect x="104" y="100" width="13" height="90" fill="{INK}"/><rect x="130" y="100" width="12" height="90" fill="{INK}"/></g>')
b += f'<ellipse cx="110" cy="142" rx="52" ry="34" fill="none" {ST}/>'
b += f'<path d="M168 96 Q170 74 190 66" fill="none" {ST}/><circle cx="192" cy="65" r="7" fill="{INK}"/>'
b += f'<circle cx="172" cy="128" r="35" fill="{Y}"/><g clip-path="url(#zh)"><circle cx="182" cy="138" r="35" fill="{YS}"/><circle cx="168" cy="124" r="35" fill="{Y}"/></g><circle cx="172" cy="128" r="35" fill="none" {ST}/>'
b += eyes("happy", 170, 192, 124, 0.8) + f'<ellipse cx="164" cy="140" rx="7" ry="5" fill="{CHEEK}"/>'
b += f'<path d="M180 140 q6 6 12 0" fill="none" {ST}/>'
b += f'<ellipse cx="132" cy="80" rx="20" ry="40" transform="rotate(16 132 80)" fill="{W}" {ST}/>'
files["B2"] = svg(b, d)

# B3 Daisy: hugging a sunflower, hearts
flower = ""
import math
for i in range(12):
    a = i * 30
    x = 120 + math.cos(math.radians(a)) * 30; y = 184 + math.sin(math.radians(a)) * 30
    flower += f'<ellipse cx="{x:.1f}" cy="{y:.1f}" rx="16" ry="9" transform="rotate({a} {x:.1f} {y:.1f})" fill="{ORANGE}" {STW(4)}/>'
flower += f'<circle cx="120" cy="184" r="21" fill="#B8601A" {ST}/>'
flower += "".join(f'<circle cx="{120 + dx}" cy="{184 + dy}" r="2.6" fill="{INK}"/>' for dx, dy in ((-8, -6), (0, -9), (8, -6), (-10, 3), (0, 0), (10, 3), (-5, 10), (5, 10)))
files["B3"] = bighead("happy", arms="hug", extra_front=flower + heart(198, 40, 1.0) + heart(34, 64, 0.7))

# B4 Honey: sleepy, honey pot, little z
pot = (f'<path d="M80 176 C66 206 86 232 120 232 C154 232 174 206 160 176 Z" fill="#E8962A" {ST}/>'
       f'<path d="M150 182 C160 206 148 224 126 228 C148 216 154 200 150 182 Z" fill="#C9761C"/>'
       f'<rect x="86" y="164" width="68" height="16" rx="8" fill="#E8962A" {ST}/>'
       f'<path d="M92 166 q28 -10 56 0" fill="none" stroke="{HONEY}" stroke-width="6" stroke-linecap="round"/>'
       f'<path d="M100 168 q0 22 -4 30 q-2 6 3 6 q5 0 3 -6 q-2 -10 2 -28" fill="{HONEY}" {STW(4)}/>'
       f'<path d="M184 34 h12 l-12 14 h12 M204 18 h9 l-9 10 h9" fill="none" {STW(4)}/>')
files["B4"] = bighead("sleepy", arms="hug", extra_front=pot)

# B5 Byte: headset + mic, open eyes, badge
headset = (f'<path d="M66 100 C66 34 174 34 174 100" fill="none" stroke="{INK}" stroke-width="10" stroke-linecap="round"/>'
           f'<rect x="54" y="84" width="22" height="38" rx="10" fill="{INK}"/><rect x="164" y="84" width="22" height="38" rx="10" fill="{INK}"/>'
           f'<rect x="60" y="92" width="10" height="22" rx="5" fill="{Y}"/><rect x="170" y="92" width="10" height="22" rx="5" fill="{Y}"/>'
           f'<path d="M66 120 q6 22 34 20" fill="none" stroke="{INK}" stroke-width="5" stroke-linecap="round"/><circle cx="103" cy="140" r="6" fill="{HONEY}" {STW(3.5)}/>')
files["B5"] = bighead("open", arms="down", badge=True, over_head=headset)

# B6 hive: the classic mascot, waving, >_ badge, heart
files["B6"] = bighead("happy", arms="wave", badge=True, extra_front=heart(200, 52, 0.85))

for k, v in files.items():
    with open(os.path.join(OUT, k + ".svg"), "w", encoding="utf-8") as f:
        f.write(v)
    print("WROTE", k)
