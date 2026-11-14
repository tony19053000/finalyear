import time, pathlib, shutil, os, tempfile
src = sorted(pathlib.Path("data/img").glob("*.ppm"))
t=time.perf_counter(); [p.stat() for p in src]; st=time.perf_counter()-t
t=time.perf_counter(); [p.read_bytes() for p in src]; r1=time.perf_counter()-t
t=time.perf_counter(); [p.read_bytes() for p in src]; r2=time.perf_counter()-t
d=pathlib.Path(tempfile.mkdtemp()); t=time.perf_counter(); [shutil.copyfile(p, d/p.name) for p in src]; cp=time.perf_counter()-t
c=sorted(d.glob("*.ppm")); t=time.perf_counter(); [p.read_bytes() for p in c]; r3=time.perf_counter()-t
print(f"files={len(src)} stat={st:.1f}s read1={r1:.1f}s read2={r2:.1f}s copy_to_temp={cp:.1f}s read_copy={r3:.1f}s cwd={os.getcwd()}")
