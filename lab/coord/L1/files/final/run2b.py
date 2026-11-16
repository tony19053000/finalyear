"""Run 2B driver (L1): GPU Mode 3 timed jobs - every job x size x laptop set x repeat, recorded to results.jsonl.

Usage:  python3 run2b.py plan            # print the run list + count
        python3 run2b.py run [--only JOB] [--reps N] [--warmup]
Laptop sets (RUN_PLAN, LEAD review): alone = L1 pinned; split2 = L1+L4; split3 = L1+L4+L2; all4 = all (doubled).
Each run writes one JSON line: job, size, set, rep, job_id, state, real_s, predicted_s, mode, chunks per node,
devices used, output digest. Already-recorded (job,size,set,rep) are skipped, so a stopped run can be resumed.
"""
import argparse, hashlib, json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "final_2b.jsonl"   # FINAL Run 2B (2026-11-16 plan v2)
STOP = HERE / "STOP"            # touch this file to stop after the current job
L1, L2, L4, L3 = ("node_01M6BMFVHJVZXC7AET3CFRE3AV", "node_01M7FJY2AK0XVZZ9EDEV4CM0W7",
                  "node_01M6GGJP3FW5VHBDFKYQ6SF318", "node_01M79X8WTANK64QH74W9YMHBG5")
ALL = [L1, L2, L4, L3]
SETS = {"alone": [L1], "split2": [L1, L2], "split3": [L1, L2, L4]}   # plan v2: no L3 (no GPU)
REPS = {"alone": 3, "split2": 3, "split3": 3}
JOBS = {  # job folder: (named input ref, mount, sizes = items, chunk items, item_seconds guess on a 3050)
    "train": ("tin20k@1", "data/tin", [8, 12, 16, 20, 24, 32, 40, 48], 2, 21.0),
    "image": ("tin20k@1", "data/tin", [24, 36, 48, 72, 96, 120, 144, 168], 12, 3.3),
    "text":  ("imdb50k@1", "data/imdb", [128, 192, 256, 400, 512, 640, 768, 896], 64, 0.6),
}


def sh(*a, check=True):
    p = subprocess.run(["compute", *a], capture_output=True, text=True)
    if check and p.returncode not in (0,):
        raise RuntimeError(f"compute {' '.join(a)} -> {p.returncode}: {p.stderr.strip()[-400:]}")
    return p.stdout


def manifest(job, size, setname):
    ref, mount, _, chunk, sec = JOBS[job]
    k = len(SETS[setname])
    lines = [f"name: run2b-{job}", "entrypoint: [python, run_chunk.py, '{items_file}', '{chunk_index}', '{out}']",
             "env:", "  base: python-cpu", "  requirements: requirements.txt",
             "resources:", "  gpu: preferred", "  vram_gb: 1",
             "inputs:", f"  - ref: {ref}", f"    mount: {mount}",
             "split:", "  over:", f"    range: {{start: 0, stop: {size}, step: 1}}", f"  chunk: {{items: {chunk}}}",
             f"  item_seconds: {sec}", "  combine: merge-json"]
    if k >= 2:
        lines += [f"  min_nodes: {k}", f"  max_nodes: {k}"]
    lines += ["limits:", "  timeout: 3h"]
    (HERE / job / "compute.yaml").write_text("\n".join(lines) + "\n")


def runs():
    for job, (_, _, sizes, _, _) in JOBS.items():
        for size in sizes:
            for setname, reps in REPS.items():
                for rep in range(1, reps + 1):
                    yield job, size, setname, rep


def done_keys():
    if not OUT.exists():
        return set()
    return {(r["job"], r["size"], r["set"], r["rep"]) for r in map(json.loads, OUT.read_text().splitlines()) if r.get("state") == "succeeded"}


def one(job, size, setname, rep):
    manifest(job, size, setname)
    args = ["run"]
    if setname == "alone":
        args += ["--node", L1]
    else:
        args += sum((["--exclude-node", n] for n in ALL if n not in SETS[setname]), [])
    t0 = time.time()
    jid = next(w for w in sh(*args, str(HERE / job)).split() if w.startswith("job_"))
    try:
        while True:
            js = json.loads(sh("jobs", jid, "--json"))
            js = js.get("job", js)
            if js["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(5)
    except KeyboardInterrupt:   # interrupted job: cancel it, record nothing -> re-run on restart
        sh("cancel", jid, check=False); print("interrupted - cancelled", jid, "(not counted)", flush=True); raise SystemExit(130)
    real = time.time() - t0
    exp = json.loads(sh("explain", jid, "--json", check=False) or "{}").get("decisions", [{}])
    pred, mode = None, None
    if exp and exp[0].get("candidates"):
        ch = [c for c in exp[0]["candidates"] if c.get("reason") == "chosen" or c.get("chosen")] or exp[0]["candidates"][:1]
        pred = ch[0].get("predicted_s") or ch[0].get("score"); mode = ch[0].get("mode")
    lanes = {a["node_id"]: a["state"] for a in js.get("attempts", []) if a.get("role") == "lane"} or \
            {a["node_id"]: a["state"] for a in js.get("attempts", [])}
    rec = {"job": job, "size": size, "set": setname, "rep": rep, "job_id": jid, "state": js["state"],
           "real_s": round(real, 1), "predicted_s": pred, "mode": mode, "lanes": lanes,
           "failure": js.get("failure_type"), "output": js.get("output_digest"), "at": time.strftime("%H:%M:%S")}
    if js["state"] == "succeeded":
        dest = HERE / "outputs" / jid
        sh("get", jid, "--dest", str(dest), "--force", check=False)
        f = dest / "results.json"
        if f.exists():
            r = json.loads(f.read_text())
            rec["devices"] = sorted({v.get("device") for v in r.values()})
            rec["items"] = len(r)
            rec["acc_mean"] = round(sum(v.get("acc", 0) for v in r.values()) / max(1, len(r)), 4)
    with OUT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["plan", "run"])
    ap.add_argument("--only"); ap.add_argument("--warmup", action="store_true")
    a = ap.parse_args()
    todo = [r for r in runs() if not a.only or r[0] == a.only]
    if a.cmd == "plan":
        from collections import Counter
        print(len(todo), "runs"); print(Counter((r[0], r[2]) for r in todo)); return
    if a.warmup:   # untimed: smallest size of every job, pinned to every GPU laptop (venv + data + caches), not recorded
        for node in (L1, L2, L4):
            for job in ([a.only] if a.only else JOBS):
                manifest(job, JOBS[job][2][0], "alone")
                p = subprocess.run(["compute", "run", "-f", "--node", node, "--no-progress", str(HERE / job)], capture_output=True, text=True)
                print("warm-up", job, node[-4:], "rc", p.returncode, flush=True)
        return
    skip = done_keys()
    for r in todo:
        if r in skip:
            continue
        if STOP.exists():
            print("STOP file found - stopped before", r, "(remove", STOP, "and run again to resume)", flush=True); return
        one(*r)


if __name__ == "__main__":
    main()
