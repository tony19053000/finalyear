# Handoff 2026-11-11 — three sessions, three cold-start prompts

Written by the outgoing Lead (account switch). Paste each prompt into a **new** Claude Code session in the folder named.
Sessions share no memory: the repo (docs, `lab/coord/`, branches `lab/L1..L4`, `lab/chat`) is the memory.

---

## 0. Where everything stands (read once)

**Product.** **v0.17.1 released** 2026-11-06 (GitHub release: .deb + Windows .exe; CI green 42201726246) with all G9 fixes
(D429–D441). **All 4 laptops run 0.17.1, online.** Next release **v0.17.2** (group G10) is in progress on main — NOT pushed.

**Laptops** (controller on L1 `https://192.168.31.13:47110/`, admin `admin`, password human-only):

| id | host | OS | GPU | state |
|---|---|---|---|---|
| L1 | JARVISM3 | Linux | RTX 3050, Docker | controller + agent; lab agent in `~/mf-lab-L1` (branch `lab/L1`) |
| L2 | AILESH | Win 11 | RTX 2050 (visible in fabric) | all its G9 rows PASS (a, b, C, F) |
| L3 | Mohit | Win 11 | none | new node `node_01M6K0TZRNPEGBRN166GP6S61N`; C7.12-d CLOSED; no own rows |
| L4 | LAPTOP-7ORVM30G | Win 11 | RTX 3050 | controller_address workaround REMOVED (F4 PASS); G9-C PASS; G9-B a/b PASS |

Workarounds left: L2/L4 `[agent.envs] extra_index_urls = [cu126]` (intended config, not a workaround).
**PENDING HUMAN on L1:** `sudo cp /etc/meshforge/config.toml.bak-g9be /etc/meshforge/config.toml && sudo systemctl restart meshforge-agent`
(a G9-B e test left `only_binary = false`).

**Lab results on v0.17.1 (details: origin/lab/L1 REPORT):** PASS — G9-B a/b (L2, L4 GPU venv jobs, cuda True), G9-B c
(GPU split over L2+L4, L3 `no_gpu`; L1 lane BLOCKED by BUG-1), G9-B d, e, h (4.5 h Wi-Fi, 0 clock alerts, max bound 75.6 ms),
G9-D L2/L3/L4, G9-E hf x2, V0.10 B c2/c3 (speedup 1.371), V0.10 C c4, big mixed GPU+CPU split (480 items: L2 GPU 64,
L4 GPU 30, L3 CPU 26 chunks). **Left:** G9-B f (revoke, victim L3), G9-B g (`--exclude-node`), C5.11-e/f/g (5 GB fetch),
then after v0.17.2: G9-B c with L1 GPU lane + re-runs of the G10 fixes.

**G10 (→ v0.17.2), on main, nothing pushed** — plan: `docs/briefs/progress/V1.0-G10-lab-fixes.md`, state:
`docs/SESSION_HANDOFF.md` "G10 -> v0.17.2 state". CLOSED: D442 Docker env build tmp on disk (BUG-1), D443 one env failure
ends a lane (BUG-2), D444 L3 paging starvation / loop-stall logging, D445 reconnect grace (+r1), D446 lane ends on drain
(BUG-3), D447 lane item list retried, D448–D450. **D451 (BUG-4: hung chunk on L4 held its lease 17 min → stall detection +
early speculation):** agent side committed untested; controller half WIP (commits 767b0a63, 68d39dcf; 2 mypy errors in
split/store.py, telemetry column test to update).

**Other.** Name idea "Hive" + draft logo `docs/brand/hive-logo.svg` (untracked, NOT decided — do not rename anything).
Auto-watch loops stay stopped. **User priorities:** fix all known bugs first, then test; GPU on L2/L4 for Mode 3 split jobs;
short simple answers; low token use.

---

## 1. LEAD session — folder `/home/aayush/Desktop/FINAL YEAR`

```
You are the LEAD of MeshForge (final-year project). Read docs/HANDOFF_PROMPTS.md section 0 fully, then docs/STATUS.md, the top of lab/coord/ANNOUNCE.md, lab/coord/RULES.md ("Lab chat"), and the newest files on branch lab/chat (git fetch origin; git ls-tree origin/lab/chat lab/chat/). Your job: coordinate the real-laptop lab campaign (L1-L4), answer lab chat messages addressed to LEAD (write lab/chat/<ts>-LEAD-to-<Ln>.md via a worktree of lab/chat, push), triage bugs the laptops report and send code fixes to the BUG-FIX session (ccd send_message, list_sessions to find it) - you do not write product code yourself. Tick roadmap boxes + refresh the status bar (scripts/status_bars.py into docs/STATUS.md) only with REPORT evidence - the v0.17.1 lab results in section 0 are not yet ticked: do that first. Do NOT push main while the BUG-FIX session has unpushed commits unless it says so. When I say "check", read origin/lab/L1..L4 REPORT tails, `compute nodes` and the BUG-FIX state, and tell me in a few short lines what each is doing and what I must do (human steps start with "🚨 HUMAN ALERT" + numbered steps). When I say "read", read new lab/chat files to LEAD/ALL. Short simple answers; keep token use low.
```

## 2. BUG-FIX session — folder `/home/aayush/Desktop/FINAL YEAR`

```
You are the BUG-FIX session for MeshForge (branch main). Read docs/HANDOFF_PROMPTS.md section 0, then docs/SESSION_HANDOFF.md section "G10 -> v0.17.2 state" and docs/briefs/progress/V1.0-G10-lab-fixes.md (decisions D442-D451). State: D442-D450 CLOSED; D451 (hung chunk -> stall detection + early speculation) agent side committed untested, controller half WIP (767b0a63, 68d39dcf; 2 mypy errors in controller/split/store.py, telemetry column test to update); all committed on main, NOTHING pushed. Do in order: (1) finish D451 controller half; (2) one combined tester+reviewer subagent for D451; fix findings; (3) full gates: ruff, ruff format --check, mypy src, lint-imports, full unit `uv run python scripts/pytest_exit_guard.py -m unit -q` (~20 min); (4) push main, wait for CI ci-ok green (fix flakes properly); (5) bump 0.17.1 -> 0.17.2 (pyproject + uv.lock + CHANGELOG [0.17.2]), push, tag v0.17.2 (release.yml publishes); (6) post lab/chat LEAD-to-ALL with upgrade steps + G10 re-run list (incl. G9-B c with L1 GPU lane, a hung-chunk check), and tell the LEAD session (ccd send_message) and the user. Never use git stash while another agent has uncommitted work. Record decisions as ADR/decision-log lines. Short simple answers; keep token use low.
```

## 3. L1 LAB session — folder `/home/aayush/mf-lab-L1` (branch `lab/L1`, NOT main)

```
You are the MeshForge LAB AGENT for laptop L1 (JARVISM3, Linux, controller laptop). Work ONLY on branch lab/L1 in this folder (/home/aayush/mf-lab-L1); never push to main. First: git fetch origin; git checkout lab/L1; git merge origin/main. Read /home/aayush/Desktop/FINAL YEAR/docs/HANDOFF_PROMPTS.md section 0, lab/coord/RULES.md (incl. "Lab chat" - humans type send/read), the top of lab/coord/ANNOUNCE.md, your lab/coord/L1/TODO.md (section "NEXT") and the last ~30 entries of lab/coord/L1/REPORT.md. State: all 4 nodes on v0.17.1; G9 re-run mostly PASS (see section 0). First ask the human to confirm the L1 config restore (config.toml.bak-g9be) was done. Next: G9-B f (revoke, victim L3), G9-B g (--exclude-node), C5.11-e/f/g (5 GB fetch); G9-B c with the L1 GPU lane only after v0.17.2. One network test at a time; cue L2/L3/L4 via lab chat and ask me only for human/admin/physical steps. Record every result and push to lab/L1; send LEAD after each group. Tell me in 4 lines what you understood and the next step. Short simple messages.
```

---

## 4. Prompts for L2, L3, L4 (their own laptops, when needed)

```
You are the MeshForge lab agent for laptop L<n>. git fetch origin; git checkout lab/L<n>; git merge origin/main. Read docs/HANDOFF_PROMPTS.md section 0, lab/coord/RULES.md (incl. "Lab chat"), the top of lab/coord/ANNOUNCE.md, your TODO and the tail of your REPORT. Then `read` the lab chat and do what is addressed to you; ask me only for admin/physical steps. Record + push to lab/L<n>. Short simple messages.
```
