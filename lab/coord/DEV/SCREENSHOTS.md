# MeshForge v0.17.5 dashboard - screenshot index (for the UI redesign)

Source: LEAD, Run 2B real fabric, 2026-11-17 15:37-15:58.
Fabric: L1 = Linux controller + node (JARVISM3, RTX 3050), L2/L3/L4 = Windows nodes (L2 RTX 3050 A), 5 revoked old nodes.
Files: `screenshots/06-lead-controller/` (local copy) = `lab/screenshots/LEAD-controller/` on lab/chat.
DEV's own shots (WSL controller, 1 node, no GPU): `screenshots/02-controller`, `04-controller-after-join`, `05-revoke-rejoin-all`.

## Whole-app notes (apply everywhere)
- Layout: left nav (Overview, Nodes, Jobs, Benchmarks, Planner, Settings) + top bar (nodes online, Live, user, Light, Sign out). Dark theme. Clean, consistent cards and tables.
- Biggest problem: raw ids everywhere (`node_01M6BMF...`, `job_...`, `usr_...`, sha256). Nodes have names (L1, JARVISM3) but most pages show ids. Show names, ids on hover/copy.
- Dense tables, no visual hierarchy: everything is the same small text. Pages read like a debug console, not a product.
- Progress bars are red even when 100 % done (Benchmarks, campaign) - red reads as error.
- Top bar "4 of 9 nodes online" (orange dot) while Overview says "All 4 nodes online" (green) - revoked nodes counted in one place, not the other.
- No GPU story at fabric level: GPU only appears as a column / a sparkline.
- Empty space: many pages use a fraction of the width (Submit job, Settings) or leave big blank areas.

## Per screenshot

| # | File | Page | Shows | Good | Improve |
|---|------|------|-------|------|---------|
| 01 | 01-overview.png | Overview | Fabric health (4 online, controller OK), Nodes 9/4/5, Active jobs 1, Utilisation (CPU 8 %, mem 55 %, GPU mean of 3 = 33 %, GPU mem 24 %), Alerts | Clear health headline; live utilisation bars | Alerts = wall of identical `clock_uncertain` raw JSON - group/collapse, human text, severity. No per-node or per-GPU view, no recent jobs list. Big empty cards (Nodes, Active jobs). |
| 02 | 02-nodes.png | Nodes list | 9 rows: name, hostname, short id, status, OS, agent, CPU/mem/GPU bars, heartbeat; filters All/online/revoked | Filter chips; inline load bars; sortable | Revoked rows shown by default and mixed in - hide or put last. L2 GPU = "none" although it has RTX 3050 A (bug/display gap). Same name used twice (two "L2", four "L3"). GPU column shows "1 x 100%" without model. |
| 03 | 03-node-L1-live.png | Node detail L1 - Live | Summary (status, heartbeat, agent, address, cert, node id, RTT) + sparklines CPU / mem / GPU 0 (100 %) | Compact summary; live sparklines | Sparklines tiny (1/8 of width); no axes/scale; GPU name missing on this tab. Cert fingerprint takes prime space. |
| 04 | 04-jobs.png | Jobs list | 50 run2b-image jobs: state, placement (offload/auto + node id), priority, created/updated, job id; filters | Good filters (state, node, owner, dates); Submit job button | Every row same name - no way to tell jobs apart; placement shows raw node id; no duration, no nodes-used, no progress for running. Owner filter needs a raw `usr_` id. |
| 05 | 05-benchmarks.png | Benchmarks list | 20 campaigns: name+id, state, done %, runs, ETA, infra failures, started | Clear name + state | 100 % bars in red; "cancelled" campaigns also show 100 % done; badge + text duplicated ("done complete"). |
| 06 | 06-planner-calibration.png | Planner - Calibration & evaluation | Calibration factors v151: term / key level / node id / family / factor / N / ratio | Full transparency | Expert-only table of raw ids; needs a summary on top ("planner is X % accurate, last updated ..."), node names, maybe a chart. |
| 07 | 07-planner-models.png | Planner - Models | Model in use (none - calibration only), Train now, 3 rejected model sets, per-term detail | Honest "no model yet" + reason | Wall of jargon (coverage, held-out log error). Needs plain status line first, details collapsed. |
| 08 | 08-node-L2-live.jpg | Node detail L2 - Live | Same as 03 for L2, GPU 0 100 % 2.3/4.0 GiB | Consistent with L1 | Same as 03. Small screenshot (941 px) - low resolution. |
| 09 | 09-node-L2-hardware.jpg | Node L2 - Hardware | CPU i7-12650H 10/16, 15.6 GiB, GPUs table (RTX 3050 A, 4 GiB, driver, CC 8.6), capabilities chips, disks | All the facts are here; capability chips | GPU should be a hero card, not a table row. Proves the Nodes-list "none" is a bug. |
| 10 | 10-node-L2-network.jpg | Node L2 - Network | Peer links (bandwidth/RTT per direction, "not measured"), controller-to-node 9.2 MiB/s | Real peer-link data | Half the rows "not measured / unknown". A small graph/map of the mesh would explain this far better than a table. |
| 11 | 11-node-L2-caches-policy.jpg | Node L2 - Caches & policy | Caches (blobs 269k items 1.6 GiB, python envs, project trees), owner policy key/values (read-only) | Read-only clearly marked | Policy shown as raw keys (`max_vram_bytes`, `allow_unsandboxed`) - needs labels and plain values. |
| 12 | 12-job-finished-logs.jpg | Job detail (finished split job) - Logs | Summary (state, placement auto, policy, project tree / output sha, named inputs) + log "combine merge-json: 4 parts -> 1 files" | Tabs cover everything; Download full log | Summary is hashes; which nodes ran it and how long it took are not in the summary. Log for a split job shows only the combine line. |
| 13 | 13-job-finished-chunks.jpg | Job finished - Chunks | Progress 100 % (48/48 items, 4/4 chunks), elapsed 1m19s vs predicted 1m37s, chunk grid, Nodes (3 lanes) | Elapsed vs predicted is great; chunk grid | Again red 100 % bar. Chunk grid tiny. Lanes table uses ids. This is the best place to show "3 laptops worked together" visually. |
| 14 | 14-job-finished-plan.jpg | Job finished - Plan | Planner decision: ranked candidates (split over 3 nodes chosen vs local vs offload), margin test, transfer/queue/env costs | Explains why the planner chose split - core value of the product | Very wide table, cut off at right; ids instead of names; needs a simple "chose split on L1+L2+L4, expected 97 s vs 163 s alone" headline. |
| 15 | 15-benchmark-campaign-overview.jpg | Campaign lab-run1-exam-middle - Overview | Progress, failures, budget & throttle, campaign facts | Well grouped | Many zero values with equal weight; node ids list. |
| 16 | 16-benchmark-campaign-spec.jpg | Campaign - spec (scrolled overview) | Spec digest + YAML spec | Shows the exact spec | Fine for experts; OK as collapsible. |
| 17 | 17-settings-invites.jpg | Settings - Invites | Invites: id, state (used/expired/revoked), created by (raw `user:usr_...`), created/expires, used by (node id) | Create invite button; states as badges | Created by = raw user id (same bug DEV saw); used by = node id instead of node name; no "copy code" flow visible. |
| 18 | 18-job-running-logs.jpg | Job detail RUNNING - Logs | Live log stream (chunk batches "labelled on NVIDIA GeForce RTX 3050 Laptop GPU"), Pause, Cancel job | Live badge, Pause, Cancel | Good. Could show which node/GPU per line. |
| 19 | 19-job-running-chunks.jpg | Job RUNNING - Chunks | 16 %, 12/72 items, 1/6 chunks, rate 0.272 items/s, ETA 3m40s, chunk grid | Live ETA and rate | Blue bar here vs red when done - colour meaning inconsistent. |
| 20 | 20-job-running-attempts.jpg | Job RUNNING - Attempts | 1 attempt, node id, state running | Simple | Mostly empty page; could merge into Timeline. |
| 21 | 21-job-running-timeline.jpg | Job RUNNING - Timeline | submitted -> planning -> queued -> preparing -> running with durations | Very readable | Could be a horizontal stepper in the job header. |
| 22 | 22-job-running-outputs.jpg | Job RUNNING - Outputs | Empty state "No outputs" | Good empty state with explanation | - |
| 23 | 23-job-manifest.jpg | Job - Manifest | Raw JSON manifest (env, split, chunk, combine, min_nodes, speculative) | Complete | Raw JSON only; syntax colour + collapse would help. |
| 24 | 24-campaign-runs.jpg | Campaign - Runs | 324 runs (100 shown): group, family, config, node, strategy, rep, state, tries, job | State filter | All ids truncated (`grp_..`, `cfg_..`, `node_..FRE3AV`) - unreadable. |
| 25 | 25-campaign-labels.jpg | Campaign - Labels | 24 groups: family, params, label "best", best config, margin, planner agrees | Shows planner correctness ("planner agrees: yes") | Best config text uses node id; margin could be a bar. |
| 26 | 26-campaign-charts.jpg | Campaign - Charts | Runtime vs parameter line chart per node, strategy comparison bar chart | Only real charts in the app - good | Charts small (left third), legend = node ids; no colour per node used elsewhere. |
| 27 | 27-campaign-quality.jpg | Campaign - Quality | Criteria met, run-record counts, configurations with flags (high CV, missing fields) | Flags as badges | 60 of 108 flagged - should be summarised, not just listed. |
| 28 | 28-campaign-download.jpg | Campaign - Download | Download records / labels (Parquet) + pyarrow snippet | Clear, with CLI equivalent | - |
| 29 | 29-planner-calibration-versions.jpg | Planner - calibration factors (rest) + Calibration versions | v146-v152, held-out error, active/superseded | Version history | A trend chart of held-out error would say more than the table. |
| 30 | 30-planner-evaluation-report.jpg | Planner - Evaluation reports | Run evaluation form, 2 reports, verdicts C1 PASS / C2 not_separable / C3 PASS, paired table | Verdict badges | Very technical; needs plain-language verdict at top. |
| 31 | 31-submit-job.jpg | Submit job | 1 Project (folder or zip), 2 Options (name, priority, placement, node), 3 Upload and run | Clear 3-step form | Uses 60 % width, rest empty; no preview of what the planner will do; no drag-and-drop. |
| 32 | 32-settings-account.jpg | Settings - Account | User, role, session id, session ends, idle timeout; controller address/status; owner policy explanation + CLI | Explains where policy lives | Session id shown in clear (fine, but not needed). |
| 33 | 33-settings-api-tokens.jpg | Settings - API tokens | Tokens & browser sessions: name, kind, state, created/last used/expires, id, Revoke | "this session" tag; Every user toggle | Expired/revoked rows clutter - hide by default. |
| 34 | 34-settings-secrets.jpg | Settings - Secrets | 1 secret LAB_KEY2, Set a secret, how-to YAML | Good help text | - |
| 35 | 35-settings-data.jpg | Settings - Data | Named inputs imdb50k@1, tin20k@1: files, size, cache, tree, Pin/Delete; CLI help | Clear | Data arguably deserves its own nav item, not Settings. |
| 36 | 36-settings-users.jpg | Settings - Users | No web API - CLI commands only | Honest | Read-only page; at least list users if possible later. |

## Still missing for the redesign (to ask LEAD later, only when user says)
- A FAILED job (state failed, error, attempts with failure, retries) - no failure state seen anywhere.
- A QUEUED / pending job, and a cancelled job.
- A node OFFLINE (was online, now lost heartbeat) and a node with a fresh join (pending / first heartbeat).
- A running split job across several nodes (lanes live, speculative copy) - the running shot (19) is a 1-lane offload.
- Create invite dialog / invite code view, Create API token dialog, Set a secret dialog, Revoke node confirm (DEV has its own from WSL).
- Login page, setup page, Light theme - DEV already has these from the WSL controller (`screenshots/02-controller`).
- Mobile / narrow window view.
- Benchmark campaign while RUNNING (progress, ETA, pause/cancel controls).
