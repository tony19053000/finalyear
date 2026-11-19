"""Demo: what is in this photo?  python guess.py photo.jpg   (or no argument = 5 held-out Tiny ImageNet photos)"""
import sys, numpy as np, torch
sys.path.insert(0, "/home/aayush/mf-eval/run2b/train")
from models import SmallCNN, to_tensor_images
ck = torch.load("/home/aayush/mf-eval/showcase/best_model.pt", map_location="cpu"); L = ck["labels"]
m = SmallCNN(ck["width"]); m.load_state_dict(ck["state_dict"]); m.eval()
if len(sys.argv) > 1:
    from PIL import Image
    imgs = np.stack([np.asarray(Image.open(p).convert("RGB").resize((64, 64))) for p in sys.argv[1:]]); names = sys.argv[1:]
else:
    D = "/home/aayush/mf-eval/run2b/data_tin"; vx = np.load(f"{D}/tin_val_x.npy"); vy = np.load(f"{D}/tin_val_y.npy")
    pick = np.random.default_rng(7).choice(len(vx), 5, replace=False); imgs = vx[pick]; names = [f"photo of a {L[int(vy[i])]}" for i in pick]
with torch.no_grad():
    p = torch.softmax(m(to_tensor_images(imgs, torch.device("cpu"))), 1)
for n, row in zip(names, p):
    top = row.topk(3); print(n, "->", ", ".join(f"{L[int(i)]} {float(v):.0%}" for v, i in zip(top.values, top.indices)))
