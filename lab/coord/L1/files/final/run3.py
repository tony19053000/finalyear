"""FINAL Run 3 - GPU offload (Mode 1): each job runs whole on ONE GPU laptop: L1 (local), sent to L2, sent to L4.
6 sizes x 3 reps x 3 laptops x 3 jobs = 162 runs. Resumable (skips recorded runs), STOP file stops after the current job.
  python3 run3.py plan | run | run --warmup"""
import argparse, json, subprocess, sys, time
import run2b as R
OUT = R.HERE / "final_run3.jsonl"; STOP = R.HERE / "STOP3"
NODES = {"L1": R.L1, "L2": R.L2, "L4": R.L4}
SIZES = {"train": [1, 2, 3, 4, 5, 6], "image": [12, 18, 24, 30, 36, 48], "text": [64, 96, 128, 160, 192, 256]}   # heavy items, fewer per job (human 2026-11-16)
REPS = 3


def runs():
    for job, sizes in SIZES.items():
        for size in sizes:
            for rep in range(1, REPS + 1):
                for name in NODES:
                    yield job, size, name, rep


def done_keys():
    if not OUT.exists():
        return set()
    return {(r["job"], r["size"], r["node"], r["rep"]) for r in map(json.loads, OUT.read_text().splitlines()) if r.get("state") == "succeeded"}


def one(job, size, name, rep):
    R.manifest(job, size, "alone")        # 1-node manifest (no min/max_nodes)
    t0 = time.time()
    jid = next(w for w in R.sh("run", "--node", NODES[name], str(R.HERE / job)).split() if w.startswith("job_"))
    try:
        while True:
            js = json.loads(R.sh("jobs", jid, "--json")); js = js.get("job", js)
            if js["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        R.sh("cancel", jid, check=False); print("interrupted - cancelled", jid, "(not counted)", flush=True); raise SystemExit(130)
    rec = {"job": job, "size": size, "node": name, "rep": rep, "job_id": jid, "state": js["state"], "real_s": round(time.time() - t0, 1),
           "lanes": sorted({a["node_id"][-4:] for a in js.get("attempts", [])}), "at": time.strftime("%H:%M:%S")}
    with OUT.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["plan", "run"]); ap.add_argument("--warmup", action="store_true")
    a = ap.parse_args(); todo = list(runs())
    if a.cmd == "plan":
        print(len(todo), "runs"); return
    if a.warmup:
        for name, node in NODES.items():
            for job, sizes in SIZES.items():
                R.manifest(job, sizes[0], "alone")
                p = subprocess.run(["compute", "run", "-f", "--node", node, "--no-progress", str(R.HERE / job)], capture_output=True, text=True)
                print("warm-up", job, name, "rc", p.returncode, flush=True)
        return
    skip = done_keys()
    for r in todo:
        if r in skip:
            continue
        if STOP.exists():
            print("STOP3 file found - stopped before", r, flush=True); return
        one(*r)


if __name__ == "__main__":
    main()
