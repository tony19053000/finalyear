# CONTINUE HERE — Hive (MeshForge) project handover

Written 2026-11-25, when the project was paused after the final-year evaluation.
**If you are a new Claude session or a person picking this project up again, read this file first.**
It explains what the project is, what was achieved, where everything is, what is unfinished, and how to restart.

---

## 1. What this project is

**Hive** turns a few ordinary laptops (Linux and Windows) on the same Wi-Fi into one shared pool of compute.
For every job it answers one question:

> *Given this workload and the machines available right now, what is the fastest practical way to run it?*

A **controller** on one laptop plans and coordinates; an **agent** on every laptop runs work. For each job the
planner compares three options and picks the fastest:

| Option (thesis wording) | Old internal name | What happens |
|---|---|---|
| **Local execution** | Local | runs on your own laptop |
| **Job offloading** | Mode 1 | the whole job runs on one other laptop, only the output comes back |
| **Task splitting** | Mode 3 | the input data is split into chunks, several laptops each run the same program on their chunks, results are combined in order |

Two further modes were designed and partly built but **not lab-tested** and are reported only as *future work*:
**Remote GPU** (old "Mode 2") and **Combined GPU** (old "Mode 4", the former "ModelPool").

Team: **Aayush Kumar (23UCS095), Rishu Raj (23UCS085), Abhay Singh (23UCS097), Aditya Kumar Tiwari (23UCS099)**,
B.Tech CSE, NIT Agartala. Supervisor: **Dr. Awnish Kumar**, Assistant Professor, CSE.
All code was written by AI coding agents (Claude Code) under the team's direction; the team made design decisions,
set up the laptops and ran every multi-laptop test.

## 2. Naming (important)

- **Public name: Hive** (thesis, presentation, website, dashboard). Wordmark "hive." (honey dot).
- **Internal / code name: MeshForge.** Python package `meshforge`, CLI binaries `meshforge-controller`,
  `meshforge-agent`, `compute`, package files `meshforge-*.deb`, `MeshForge-Agent-Setup-*.exe`, Windows service
  `MeshForgeAgent`, data dirs `/var/lib/meshforge`, `/etc/meshforge`, `/opt/meshforge`.
- **Done:** visible UI rename MeshForge → Hive (PR #14: dashboard, agent console, logo, favicon; PR #15 console styles).
- **Not done:** the full rename of packages / code / services to Hive (planned names: `hive-agent`, `hive-controller`).
  This is the first item of "future work" below. `README.md` still says MeshForge and uses mode numbers — that is the
  old engineering README; the thesis and presentation use the wording in the table above.
- In anything written for evaluators: **never use "Mode 1/2/3/4"**; say local execution, job offloading, task splitting.

## 3. Status at pause (Nov 2026)

- **Version:** 0.17.5 (`main` at `b0e68699`, which includes PR #13 new Hive UI, #14 visible rename, #15 console style fix).
- **Lab-tested and working:** joining with one-time invite codes + certificates (mTLS), heartbeats and live
  CPU/GPU/RAM monitoring, job submission (CLI + dashboard), job offloading, task splitting with a pull-based chunk
  queue, leases, fencing tokens, speculative copies of stragglers, in-order combine, content-addressed file cache,
  environment caching (venv / Docker), the planner (cost model + calibration + gated ML), dashboard, Windows
  installer and service, Linux .deb packages, benchmark campaigns and telemetry.
- **Evaluation:** thesis submitted (58 pages), presentation given on **23 Nov 2026** (10 min talk + video + Q&A).
- **Roadmap:** V1.0 items 1.0.1–1.0.3 were deferred on 2026-11-15 (evaluation-baseline decision). See
  `docs/DEVELOPMENT_ROADMAP.md`, `docs/STATUS.md`, `docs/SESSION_HANDOFF.md` for the last engineering state.

## 4. Architecture in one page

- **Controller** (one laptop, Ubuntu 24.04): FastAPI REST API + React dashboard (HTTPS 47110), gRPC session listener
  for agents (47100), enrolment listener (47101), PostgreSQL 16 bundled (127.0.0.1:47150). Holds all state: nodes,
  jobs, attempts, chunk queue, timings, planner decisions, calibration and ML model versions, cache locations, audit.
- **Agent** (every laptop, Ubuntu or Windows 10/11 service): one long-lived gRPC stream to the controller (protobuf
  messages over HTTP/2 + mTLS over TCP): Hello with hardware, heartbeats every 2 s, live resource reports, chunk
  leases, logs, results. Files move over HTTPS with one-time tokens; the agent keeps a small SQLite cache index.
- **Security:** the controller runs its own private CA (ECDSA P-256). A join code = controller address + one-time
  token + CA fingerprint pin; the laptop sends a CSR and gets a 30-day node certificate. Revocation is permanent.
- **Discovery:** mDNS first, then the last-known controller address; certificates prove identity regardless of IP.
- **Planner:** candidates (local / each single laptop / fastest-first laptop sets) → feasibility → cost model
  `T = queue + transfer + env + compute + output` (compute = median of the last 20 runs, else scaled by the hardware
  score) → calibration (median actual/predicted over the last 200 runs, shrunk with n/(n+5), clamped 0.2–5, held-out
  check) → per-term ML correction (kNN for env, LightGBM for combine) only when it passes the conformal gate →
  risk term → split margin test (a split must be ≥10 % faster than the best single). Decisions are replayable
  (`compute explain`).
- **ML truth (say this honestly):** trained on 9,533 rows from 3,889 runs; offline kNN env error 0.44 vs calibration
  2.57, LightGBM combine 0.75 vs 2.85. **Live, the gate keeps ML off** (conformal intervals wider than a factor of 2),
  so the live planner uses calibration. Fix branch `fix/ml-per-term-gate` (D462, per-term gate) is built, tested,
  reviewed, **not merged** (parked by the team).

## 5. Key results (real, from the thesis)

- CPU task splitting, 1/2/3/4 laptops: Monte Carlo simulation 156/83/61/52 s (**2.97×**), CSV 35/24/20/16 s,
  Image AI on CPU 44/40/40/29 s.
- GPU, 1/2/3 GPUs (L3 has no GPU): AI training 48 models 1934/987/677 s (2.86×), Image AI 609/331/224 s,
  Text AI (IMDB) 586/283/184 s (**3.17×**).
- 270-model hyperparameter search (SmallCNN, Tiny ImageNet): 2 h 48 min on L1 → 58 min on L1+L2+L4 (2.89×), same best config.
- Planner: chosen option within 15 % of the best measured option in all 36 test groups; p99 decision 5.95 ms.
- Reliability: no failed job in the campaigns; identical output after an agent was stopped mid-job; 140 models
  completed with lids closed at random.
- Windows fixes: power throttling (EcoQoS) → mark job processes HighQoS: 164 s → 48 s; Defender exclusion for the
  agent data dir: reading 20k small files 104 s → 1.2 s; `.contiguous()` after permute: ~94 s → 18.8 s per model.

## 6. Known bugs / open items (fix when resuming)

1. Full MeshForge → Hive rename of packages, services, data dirs, docs (see §2).
2. Merge-or-drop decision for `fix/ml-per-term-gate` (D462) so ML can be used live.
3. Agent reconnect loop seen on L4 (session ONLINE → BACKOFF every ~6 s, `execute_batch INTERNAL` on send; fixed by
   a service restart) — investigate on the controller side.
4. Console/dashboard `index.html` should be served with `Cache-Control: no-cache` (old assets after upgrade).
5. Stuck lane after a cancelled speculative chunk; a split job with `min_nodes` > feasible nodes runs unsplit; a
   laptop that rejoins does not take more chunks of a running split job; `compute planner ml compare` needs ML extras.
6. Windows L2 upgrade: an `icacls` timeout once left the service stopped (started by hand).
7. Peer-to-peer file transfer is built but off by default and never lab-tested.
8. Not yet: backup/standby controller, battery/charger awareness, Remote GPU and Combined GPU lab validation.

## 7. Where things are

- **Code + docs:** this repo. Read order for engineering: `CLAUDE.md` → `docs/SESSION_HANDOFF.md` → `docs/STATUS.md` →
  `docs/DEVELOPMENT_ROADMAP.md` → `docs/ARCHITECTURE.md` + `docs/adr/` (26+ ADRs) → `docs/PROJECT_BRIEF.md`.
  Plain-language overview: `docs/PROJECT_SUMMARY.md`. Lab procedures: `docs/LAB.md`, `lab/coord/RULES.md`.
- **Branches:** `main` (current product); `lab/chat` (lab coordination chat between laptops, one file per message in
  `lab/chat/`); `fix/ml-per-term-gate` (D462, unmerged); `ci/green`, `spike/sp1-windows-service`;
  **`archive/*`** — every local branch from the main laptop pushed on 2026-11-25 before the laptop was cleaned
  (old agent worktrees, `release-v0.17.0`, `v1.0-brief`, `fix/v1.0-ci-round4`, `ci-fix-g4-firstrun`), and
  **`archive/l1-leftovers`** (uncommitted website assets and `docs/brand/hive-logo.svg`).
- **Website:** `website/` (static landing page with Windows/Ubuntu download pages; newest assets on `archive/l1-leftovers`).
- **Kept on the main laptop (not in git), Desktop of L1:** final thesis `Super Final.pdf` (58 pages) and
  `FINAL PDF/FINAL PDF.pdf` (thesis + AI score + plagiarism score), thesis LaTeX source `Hive-Thesis-OLD/`,
  change logs `changes/`, presentation material `presentation/`, video work `Hive-Video-Work/` (brief, script,
  handoff), `Hive-Video-Script-Aayush.pdf`, practice PDFs. Turnitin: AI score below 20 % (shown as "*%"),
  similarity 2 %.
- **Presentation decks (claude.ai artifacts):** main dark deck https://claude.ai/artifact/FwTeCKYGo5ZzyFco3KJrAp ,
  white script deck https://claude.ai/artifact/4wW6X92VE5s3A3yMDi2kD6 .

## 8. Machines at pause

- **L1** (Ubuntu, JARVISM3, i7-12650H, RTX 3050, was controller + agent): Hive **uninstalled** on 2026-11-25
  (`apt purge`, `/var/lib|etc|opt/meshforge` removed), repo folder deleted. The controller database is gone.
- **L2, L3, L4** (Windows 11) and **DEV**: asked to remove all Hive files, installers, datasets and project memory, and
  to restore Balanced power plan, normal sleep, vendor performance mode and remove Defender/firewall exceptions.
- Node ids, join codes and certificates from the lab are **obsolete**. A fresh start needs a new controller install
  (`meshforge-controller fabric create`), new invites, and agents joining again.

## 9. How to resume

1. `git clone https://github.com/tony19053000/finalyear && cd finalyear`
2. Tooling: Python 3.12 + `uv`, Node 20+. `uv sync --all-extras` and `cd frontend && npm ci`.
3. Build packages: `uv run python scripts/build_deb.py --build-bundle --role controller --revision N --out <dir>`
   (and `--role agent`, each with its own bundle); Windows installer: `scripts/build_windows_installer.py`.
4. Install on a fresh Ubuntu controller, create the fabric, invite laptops, run `compute run` on an example in
   `workloads/examples/` or `examples/`.
5. Work model (from `CLAUDE.md`): the main session is the lead; every code task goes implementer → tester → reviewer
   (agents in `.claude/agents/`); nothing is merged until the owner says "merge"; docs are the memory
   (`docs/SESSION_HANDOFF.md`, `docs/STATUS.md`) and must be updated every session.

### Starter prompt for a new Claude Code session
```
You are the lead engineer for Hive (code name MeshForge). Read CONTINUE_HERE.md fully, then CLAUDE.md,
docs/SESSION_HANDOFF.md, docs/STATUS.md and the current version in docs/DEVELOPMENT_ROADMAP.md.
Summarise the project state in 10 short lines, list the open items from CONTINUE_HERE.md §6, and propose
what to do first. Keep answers short and simple. Do not change code until I agree.
```
