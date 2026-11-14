import ctypes, sys, time, runpy
t0 = time.perf_counter()
if "qos" in sys.argv:
    k = ctypes.windll.kernel32; k.GetCurrentProcess.restype = ctypes.c_void_p
    class PT(ctypes.Structure): _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
    s = PT(1, 1, 0); print("qos set:", k.SetProcessInformation(ctypes.c_void_p(k.GetCurrentProcess()), 4, ctypes.byref(s), ctypes.sizeof(s)))
import torch; t1 = time.perf_counter()
torch.zeros(1, device="cuda"); torch.cuda.synchronize(); t2 = time.perf_counter()
print(f"import torch {t1-t0:.1f}s  cuda init {t2-t1:.1f}s")
script = sys.argv[1]; sys.argv = [script] + [a for a in sys.argv[2:] if a != "qos"]
runpy.run_path(script, run_name="__main__"); torch.cuda.synchronize()
print(f"chunk work {time.perf_counter()-t2:.1f}s  total {time.perf_counter()-t0:.1f}s")
