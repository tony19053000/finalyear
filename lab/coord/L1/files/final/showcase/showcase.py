"""SHOWCASE (human 2026-11-19): one big GPU job - AI hyperparameter search, 270 models on Tiny ImageNet.
Run A: L1+L2+L4 (3 GPUs) first, then Run B: L1 alone. Same job, same data, same seeds. Resumable; STOPS stops after the current run."""
import sys, re, run2b as R
R.OUT = R.HERE / "showcase.jsonl"; STOP = R.HERE / "STOPS"
SIZE = 270
done = R.done_keys()
for setname in ("split3", "alone"):          # split3 = L1+L2+L4, alone = L1
    if ("train", SIZE, setname, 1) in done: continue
    if STOP.exists(): print("STOPS found - stopped", flush=True); sys.exit(0)
    orig = R.manifest
    def manifest(job, size, sn, _o=orig):    # same manifest, longer timeout (L1 alone ~3 h)
        _o(job, size, sn); p = R.HERE / job / "compute.yaml"
        p.write_text(re.sub(r"timeout: \S+", "timeout: 6h", p.read_text()))
    R.manifest = manifest
    R.one("train", SIZE, setname, 1)
    R.manifest = orig
