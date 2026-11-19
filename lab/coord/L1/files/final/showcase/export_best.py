"""After the showcase: pick the best of the 270 models, re-train it exactly (same seed + settings) on L1's GPU, save it, report accuracy."""
import json, sys, time, glob
import numpy as np, torch, torch.nn as nn
sys.path.insert(0, "/home/aayush/mf-eval/run2b/train")
from models import SmallCNN, to_tensor_images
D = "/home/aayush/mf-eval/run2b/data_tin"; STEPS, BS = 300, 128   # same as the job (train/run_chunk.py)
res = json.load(open(sys.argv[1]))                                # results.json from `compute get`
it, best = max(res.items(), key=lambda kv: kv[1]["acc"]); it = int(it)
print(f"best of {len(res)} models: model {it} lr {best['lr']} width {best['width']} acc {best['acc']} (trained on {best['device']})")
dev = torch.device("cuda")
x = np.load(f"{D}/tin_train_x.npy"); y = torch.from_numpy(np.load(f"{D}/tin_train_y.npy").astype(np.int64))
vx = np.load(f"{D}/tin_val_x.npy")[:2000]; vy = torch.from_numpy(np.load(f"{D}/tin_val_y.npy")[:2000].astype(np.int64))
torch.manual_seed(it); g = torch.Generator().manual_seed(it)
m = SmallCNN(best["width"]).to(dev); opt = torch.optim.AdamW(m.parameters(), best["lr"], weight_decay=5e-4); t = time.time(); m.train()
for s in range(STEPS):
    idx = torch.randint(0, len(x), (BS,), generator=g).numpy()
    loss = nn.functional.cross_entropy(m(to_tensor_images(x[idx], dev)), y[idx].to(dev)); opt.zero_grad(); loss.backward(); opt.step()
m.eval(); ok = 0
with torch.no_grad():
    for i in range(0, len(vx), 500): ok += (m(to_tensor_images(vx[i:i + 500], dev)).argmax(1).cpu() == vy[i:i + 500]).sum().item()
torch.save({"state_dict": m.state_dict(), "width": best["width"], "lr": best["lr"], "model": it, "acc": ok / len(vx),
            "labels": json.load(open("/home/aayush/mf-eval/showcase/labels.json"))}, "/home/aayush/mf-eval/showcase/best_model.pt")
print(f"re-trained in {time.time()-t:.0f} s, held-out accuracy {ok/len(vx):.3f} -> saved ~/mf-eval/showcase/best_model.pt")
