"""Lab G9-B c: train a small MLP on CUDA per item (seed = item); write results.json."""
import json, os, sys, time
os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
import torch

items_file, chunk, out = sys.argv[1:4]
items = json.load(open(items_file))
cuda = torch.cuda.is_available()
dev = torch.device("cuda" if cuda else "cpu")
import platform
name = torch.cuda.get_device_name(0) if cuda else "CPU " + platform.node()
res = {}
for it in items:
    torch.manual_seed(int(it))
    x = torch.randn(4096*8, 64, device=dev); y = x.sum(1, keepdim=True)
    m = torch.nn.Sequential(torch.nn.Linear(64, 256), torch.nn.ReLU(), torch.nn.Linear(256, 1)).to(dev)
    opt = torch.optim.Adam(m.parameters(), 1e-3); t = time.time()
    for _ in range(300):
        opt.zero_grad(); loss = torch.nn.functional.mse_loss(m(x), y); loss.backward(); opt.step()
    if cuda: torch.cuda.synchronize()
    res[str(it)] = {"loss": round(loss.item(), 6), "gpu": name, "s": round(time.time() - t, 2)}
    print(f"chunk {chunk} item {it} loss {loss.item():.6f} on {name}", flush=True)
os.makedirs(out, exist_ok=True)
json.dump(res, open(os.path.join(out, "results.json"), "w"))
