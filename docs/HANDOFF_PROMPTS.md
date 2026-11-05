# Handoff 2026-11-05 — three sessions, three cold-start prompts

Written by the outgoing Lead for the team. Paste each prompt into a **new** Claude Code session in the folder named.
Sessions share no memory: the repo (docs, `lab/coord/`, branches `lab/L1..L4`, `lab/chat`) is the memory.

---

## 0. Where everything stands (read once)

**Product.** MeshForge v0.17.0 is released (GitHub release, .deb + Windows .exe). Real-laptop lab campaign is running
(D425 Wi-Fi only, D426 git coordination). Agent-side roadmap work is complete except V1.0 G6 (needs lab data).

**Laptops** (all joined to the fabric on v0.17.0; controller on L1 `https://192.168.31.13:47110/`, admin user `admin`,
password human-only):

| id | host | OS | IP | GPU | state |
|---|---|---|---|---|---|
| L1 | JARVISM3 | Linux | 192.168.31.13 | RTX 3050 4 GB, Docker | controller + agent. Lab agent = separate session in `~/mf-lab-L1` (branch `lab/L1`). Done V0.2–V0.8; V0.9 PARTIAL (rest deferred to an evening); now V0.10 part B. |
| L2 | AILESH | Win 11 | 192.168.31.155 | RTX 2050 4 GB | was on hold, back since 2026-11-05; did its pending V0.5–V0.8 parts via chat with L1 (secrets, browser, capture). Open: C8.10-g PowerShell completion BUG. |
| L3 | MOHIT (new laptop) | Win 11 | 192.168.31.71 | none | old L3 TIWARI_JI retired (backup branch `lab/L3-old-TIWARI_JI`). Re-joined as NEW node after the V0.7 revoke test (old node revoked on purpose). Last REPORT entries: V0.7 C7.12-c/d. Report is behind (30 h) - it must record the rejoin. |
| L4 | LAPTOP-7ORVM30G | Win 11 | 192.168.31.244 | RTX 3050 4 GB | V0.6 done, V0.7 clock done. Has workaround `controller_address = "192.168.31.13:47100"` in `C:\ProgramData\MeshForge\config\config.toml`. |

**Active workarounds on v0.17.0** (remove after upgrading to v0.17.1): L4 `controller_address` (F4); restart the agent
after changing Run jobs (F5); Windows peer firewall rule via `firewall-sync` admin command (F7, by design); L2/L4 owner
`[agent.envs] index_url = https://download.pytorch.org/whl/cu126` for CUDA torch in venvs (v0.17.1 replaces it with
`extra_index_urls`, D436).

**Coordination tools.**
- `lab/coord/RULES.md` (lab agents' rules), `CAMPAIGN.md`, `ANNOUNCE.md` (Lead broadcasts, newest on top),
  `lab/coord/Ln/TODO.md` + `REPORT.md` on branch `lab/Ln`.
- **Lab chat**: branch `lab/chat`, one file per message `lab/chat/YYYYMMDD-HHMMSS-FROM-to-TO.md`. Humans type `send <to>`
  and `read` in a laptop's Claude window. The Lead reads/writes messages to/from `LEAD` (use a worktree of `lab/chat`).
- Auto-watch loops were tried and **stopped** (human decision). Do not restart them. No frequent polling.

**Fixed on main** (pushed): F4 (D427), F5 (D428, "look only, act later"), F6; CI green run 41694928760.
**G9 lab-fix group** (committed on main, **NOT pushed**; main is ahead of origin by ~34 commits): see
`docs/briefs/progress/V1.0-G9-lab-fixes.md`. G9-A closed (D429 staging retry, D430 DSCP CS1, D431 max_rate_mbit);
G9-B closed (cfb47d52: CLI failure/revoke detail, D432 exec-torch, D436 extra_index_urls, venv GPU + Mode 3 GPU lanes
on GPU nodes only, D433 env-policy fingerprint, `--exclude-node`, D434 clock alert); UI Cancel fix done;
**G9-C implemented, NOT yet tested** (7ddc7bd3..0a479577, Windows CPU meter, `meshforge-agent doctor cpu`).

**New lab finding not yet triaged:** L1 V0.10 part B (C10.10-c): "L2: a long single chunk loses its lease forever" -
split runs correct, but single-laptop comparison for L2-L4 INCONCLUSIVE. Needs the bug-fix session.

**User priorities:** GPU on L2/L4 must work for Mode 3 split jobs (independent pieces: inference over many items,
several trainings). Short simple answers. Keep token use low (few agents; combine tester+reviewer for small fixes).

---

## 1. LEAD session — folder `/home/aayush/Desktop/FINAL YEAR`

```
You are the LEAD of MeshForge (final-year project). Read docs/HANDOFF_PROMPTS.md section 0 fully, then docs/STATUS.md, the top of lab/coord/ANNOUNCE.md, lab/coord/RULES.md ("Lab chat"), and the newest files on branch lab/chat (git fetch origin; git ls-tree origin/lab/chat lab/chat/). Your job: coordinate the real-laptop lab campaign (L1-L4), answer lab chat messages addressed to LEAD (write lab/chat/<ts>-LEAD-to-<Ln>.md via a worktree of lab/chat, push), post broadcasts in lab/coord/ANNOUNCE.md, triage bugs the laptops report (send code fixes to the BUG-FIX session via lab/chat or ccd send_message - you do not write product code yourself), and tick roadmap boxes + refresh the status bar (scripts/status_bars.py into docs/STATUS.md) only with REPORT evidence. Do NOT push main while the BUG-FIX session has unpushed commits unless it says so. First: when I say "check", read origin/lab/L1..L4 REPORT.md tails and `compute nodes`, and tell me in a few short lines what each laptop is doing and what I must do. Short simple answers; keep token use low.
```

## 2. BUG-FIX session — folder `/home/aayush/Desktop/FINAL YEAR`

```
You are the BUG-FIX session for MeshForge (branch main). Read docs/HANDOFF_PROMPTS.md section 0, then docs/briefs/progress/V1.0-G9-lab-fixes.md (plan, root causes, decisions D429-D436). State: G9-A and G9-B CLOSED, UI Cancel fix done, G9-C (Windows CPU meter, commits 7ddc7bd3..0a479577) IMPLEMENTED but NOT tested - all committed on main, NOTHING pushed. Do in order: (1) one combined tester+reviewer subagent for G9-C; fix findings; (2) triage the new lab finding "a long single chunk loses its lease forever" (L1 REPORT on origin/lab/L1, step C10.10-c) - root-cause, fix with implementer -> combined tester+reviewer; (3) full gates: ruff, ruff format --check, mypy src, lint-imports, full unit `uv run python scripts/pytest_exit_guard.py -m unit -q` (~19 min; xdist not installed); (4) push main, wait for CI ci-ok green (fix flakes properly); (5) bump pyproject 0.17.0 -> 0.17.1 + uv.lock + CHANGELOG [0.17.1], push, tag v0.17.1 (release.yml publishes); (6) post a lab/chat message LEAD-style to ALL with the upgrade steps and the G9 re-run list from the plan file (G9-B a-h, G9-C L4 doctor cpu), and tell the user. Never use git stash while another agent has uncommitted work. Record decisions as ADR/decision-log lines. Short simple answers; keep token use low.
```

## 3. L1 LAB session — folder `/home/aayush/mf-lab-L1` (branch `lab/L1`, NOT main)

```
You are the MeshForge LAB AGENT for laptop L1 (JARVISM3, Linux, controller laptop). Work ONLY on branch lab/L1 in this folder (/home/aayush/mf-lab-L1); never push to main. First: git fetch origin; git checkout lab/L1; git merge origin/main. Read docs/HANDOFF_PROMPTS.md section 0, lab/coord/RULES.md (incl. "Lab chat" - humans type send/read), lab/coord/CAMPAIGN.md, the top of lab/coord/ANNOUNCE.md, your lab/coord/L1/TODO.md and the last ~40 entries of your lab/coord/L1/REPORT.md. State: V0.2-V0.8 done, V0.9 PARTIAL (rest deferred to an evening), now V0.10 part B (C10.10-c: split runs OK; single-laptop runs for L2-L4 inconclusive because of the lease bug - reported to LEAD for the bug-fix session). L2 is back, L3 is a re-joined NEW node (check `compute nodes`), L4 uses the controller_address workaround. GPU on L2/L4 is a user priority (owner index_url cu126 workaround is GO; v0.17.1 brings extra_index_urls). Tell me in 4 lines what you understood and the next step, then continue your TODO step by step; send cues to other laptops via the lab chat (`send <Ln>` style message files) and ask me only for human/admin/physical steps. Record every result and push to lab/L1. Short simple messages.
```

---

## 4. Prompts for L2, L3, L4 (their own laptops, when needed)

```
You are the MeshForge lab agent for laptop L<n>. git fetch origin; git checkout lab/L<n>; git merge origin/main. Read docs/HANDOFF_PROMPTS.md section 0, lab/coord/RULES.md (incl. "Lab chat"), the top of lab/coord/ANNOUNCE.md, your TODO and the tail of your REPORT. Then `read` the lab chat and do what is addressed to you; ask me only for admin/physical steps. Record + push to lab/L<n>. Short simple messages.
```
(L3 first: record the re-join as the new node and finish C7.12-d.)
