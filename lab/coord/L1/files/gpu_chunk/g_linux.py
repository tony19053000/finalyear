import sys, time, runpy
t0 = time.perf_counter()
import torch; t1 = time.perf_counter()
torch.zeros(1, device="cuda"); torch.cuda.synchronize(); t2 = time.perf_counter()
print(f"import torch {t1-t0:.1f}s  cuda init {t2-t1:.1f}s")
script = sys.argv[1]; sys.argv = [script] + sys.argv[2:]
runpy.run_path(script, run_name="__main__"); torch.cuda.synchronize()
print(f"chunk work {time.perf_counter()-t2:.1f}s  total {time.perf_counter()-t0:.1f}s")
