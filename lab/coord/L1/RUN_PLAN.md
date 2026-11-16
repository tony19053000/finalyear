# Lab run plan - FINAL v2 (LEAD chat 2026-11-16 16:32-17:03, human decisions) - PREP DONE, start on the human's "go"

Status: preparation done 2026-11-16 ~17:30 (files in lab/coord/L1/files/final/). No planner fix now (future task). history_runs 20.
Order: Run 1 -> Part E (+ A1/B/C/D) -> 2B -> 2A + kill -> Run 3 -> ML top-up -> ML train + compare.
WARM-UP RULE: before EVERY run and after any reboot - one untimed job per job type on every laptop the run uses (not counted).

| # | Run | File | Job types + sizes | Laptops / ways | Runs | Est. real |
|---|---|---|---|---|---|---|
| 1 | Run 1 exam | run1.yaml | cpu.primes limit 400M x repeats 13..35 (12 steps; family max limit is 400M, so size grows via repeats); image_classify 9k-20k (family max 20k) | L1 local, mode1 L2/L3/L4, image also mode3 all 4 | 324 (24 groups), ref 9.0 h | ~5-7 h |
| 2 | Part E | partE.yaml (E2a local + mode1 L2) + partE_b.yaml (E2b mode3 all 4) + 5+5 planner-choice submits | csv_aggregate 55k-80k rows (max ~81k: 8 MB file cap); hash_files 2048-4000 files x16 KB x256 rounds (64 MB cap -> ~10-15 s jobs) | L1 / mode1 L2 / mode3 all 4 | 72+36+10, ref 1.0 h | ~1 h |
| 3 | Run 2B GPU split | run2b.py (final_2b.jsonl) | train 8-48, image AI 24-168, text AI 128-896 (Tiny ImageNet / IMDB) | L1 / L1+L2 / L1+L2+L4 (no L3) | 216 | ~19-21 h |
| 4 | Run 2A CPU split medium | run2a.yaml + run2a_extra.yaml (all-4 doubling) | image 13k-20k, montecarlo 14k-21k, csv 45k-80k | L1 / L1+L2 / L1+L2+L4 / all 4 (x2) + 1 kill run (cue humans first) | 288+72+1, ref 9.4 h | ~5-8 h |
| 5 | Run 3 GPU offload | run3.py (final_run3.jsonl) | heavy items, fewer per job: train 1-6, image 12-48, text 64-256 | L1 / sent to L2 / sent to L4 | 162 | ~4.5-5 h |
| 6 | ML top-up | topup.yaml (CPU 324, all 6 CPU types, 3 small-medium sizes) + GPU script (~200, to write) | | local, mode1 L2/L3/L4, splits up to all 4; GPU L1/L2/L4, L1+L2, L2+L4, L1+L2+L4 | ~524 | ~6-8 h |
| 7 | ML train + compare + live check | - | post-v0.17.5 runs only | L1 | - | 2-3 h |

Pause/resume: campaigns `compute bench pause|resume <camp>` (tested camp_01M7J38CTGQFKTV4NCK3BEAF8J: finished runs kept);
2B: `touch ~/mf-eval/run2b/STOP` stops after the current job, Ctrl-C cancels the current job (not counted); rerun = resumes (tested).
Run 3: same with STOP3 (tested: stop before job 2, restart ran only job 2).
Campaign rule: repetitions must be >= 3 (tool minimum) -> Part E E2 uses 3 reps, not 2.
