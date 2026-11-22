#!/bin/sh
# Renders all mascot loops into ../assets/img/bee (run from this folder). ~3-4 min on a laptop.
# Only after the design is approved; then set HIVE.bee3d = true in assets/js/site.js.
BL="${BLENDER:-/c/Program Files/Blender Foundation/Blender 5.3/blender.exe}"
OUT="${OUT:-$(cd .. && pwd)/assets/img/bee}"
mkdir -p "$OUT"
for pose in idle wave carry fly; do
  "$BL" -b -P bee.py -- "$OUT" --pose $pose --size 360 --frames 8 2>&1 | grep -i "WROTE\|Error\|Traceback"
done
rm -rf "$OUT"/_frames_*
