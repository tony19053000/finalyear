import numpy as np, time, os
def w(n, k): return (np.frombuffer(os.urandom(n), dtype=np.uint8).astype(np.int64) % 16 - 8)[:n].reshape(k, -1)
W=32; w1=w(27*W,27); b1=np.zeros(W,np.int64); w2=w(9*W*W,9*W); b2=np.zeros(W,np.int64); w3=w(W*10,W)
def im2col(x):
    s=x.shape[1]; pad=np.pad(x,((0,0),(1,1),(1,1),(0,0)))
    return np.concatenate([pad[:,dy:dy+s,dx:dx+s,:] for dy in range(3) for dx in range(3)],axis=-1)
def pool(x): return (x[:,0::2,0::2]+x[:,1::2,0::2]+x[:,0::2,1::2]+x[:,1::2,1::2])>>2
imgs=np.random.randint(0,256,(64,32,32,3)).astype(np.int64)
t=time.perf_counter()
for _ in range(20000//64):
    h=im2col(imgs).reshape(-1,27)@w1+b1; h=(np.maximum(h,0)>>4).reshape(64,32,32,W); p=pool(h)
    h2=im2col(p).reshape(-1,9*W)@w2+b2; h2=(np.maximum(h2,0)>>6).reshape(64,16,16,W); (h2.sum(axis=(1,2))@w3).argmax(axis=1)
print(f"20000 images CNN: {time.perf_counter()-t:.1f} s")
