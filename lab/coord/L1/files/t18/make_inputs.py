"""Make a T19 source folder like the T18 job inputs: data/img with 20 000 small .ppm files (~3 KB each, 32x32 RGB)."""
import os, pathlib, sys
root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "t18src") / "data" / "img"
root.mkdir(parents=True, exist_ok=True)
hdr = b"P6\n32 32\n255\n"
for i in range(20000):
    (root / f"{i:05d}.ppm").write_bytes(hdr + os.urandom(32 * 32 * 3))
print("made", len(list(root.glob("*.ppm"))), "files in", root)
