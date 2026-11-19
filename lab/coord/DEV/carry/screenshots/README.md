# MeshForge v0.17.5 screenshots - index

103 screenshots: 67 by DEV (this laptop, WSL controller, 1 Windows node, no NVIDIA GPU) + 36 by LEAD (real 4-laptop fabric, 3 x RTX 3050).
Full per-page review of LEAD's 36 (good / improve / missing): `mf-dev/lab/coord/DEV/SCREENSHOTS.md`.
`_old/` = 299 raw installer frames + 11 duplicate revoke shots. Not needed, safe to delete.

## 01-windows-install (6, DEV) - Windows agent installer
| File | Shows |
|---|---|
| 01-welcome | Installer welcome |
| 02-license | Licence page |
| 03-license-scrolled | Licence scrolled down |
| 04-what-this-computer-shares | Share choices during install |
| 05-installing | Progress |
| 06-finished | Finish page |

## 02-controller (3, DEV) - first controller setup (WSL Ubuntu)
| File | Shows |
|---|---|
| 01-setup-top | Setup page top |
| 02-setup-first-admin | Create first admin |
| 03-fabric-ready-join-code | Fabric ready + first join code |

## 03-node (14, DEV) - agent console on this laptop (127.0.0.1:47130), first join
| File | Shows |
|---|---|
| 01-not-signed-in | Console before joining |
| 02-welcome | Join wizard step 1 |
| 03-join-empty / 04-join-code-entered | Step 2: paste join code |
| 05-what-this-laptop-shares / 06-shares-gpu-safety | Step 3: name, jobs on/off, CPU/mem limits, GPU sharing |
| 07-checks-running / 08-checks / 09-checks-bottom | Step 4: Docker / NVIDIA / CUDA checks |
| 10-done | Step 5: done + dashboard link |
| 11-home-connected | Home: connected, laptop facts |
| 12-settings | Node settings (owner policy) |
| 13-logs | Agent logs |
| 14-leave | Leave fabric tab |

## 04-controller-after-join (16, DEV) - dashboard with 1 node
| File | Shows |
|---|---|
| 01-sign-in | Login page |
| 02-overview-1-node-online | Overview, 1 node |
| 03-nodes | Nodes list |
| 04..07-node-* | Node detail: Live, Hardware, Network, Caches & policy |
| 08-jobs | Jobs (empty) |
| 09-benchmarks | Benchmarks (empty) |
| 10-planner | Planner (empty) |
| 11..16-settings-* | Account, API tokens, Secrets, Data, Invites (used), Users |

## 05-revoke-rejoin-all (28, DEV) - 3 revoke + rejoin cycles (c1/c2/c3)
| File | Shows |
|---|---|
| 01-c1-controller-revoke-confirm | Revoke dialog, type hostname to confirm |
| 02-c1-controller-nodes-revoked | Nodes list, 0 of 1 online |
| 03-c1-controller-node-revoked-toast | Node detail revoked + toast |
| 04-c1-controller-invite-dialog | Invite a laptop: expiry, new laptop / re-key |
| 05-c1-controller-join-code-qr | Join code shown once + QR + CA pin |
| 06-c1-node-stopped-revoked-banner | Node console: red "stopped, revoked" banner |
| 07-c1-node-leave-tab / 08-c1-node-leave-confirm | Leave fabric + confirm |
| 09-c1-node-rejoin-welcome / 10-c1-node-rejoin-shares | Wizard again (step 1, step 3) |
| 11-c1-node-home-connected-run2 | Reconnected as Victus-run2 |
| 12-c1-controller-nodes-2-entries | Same laptop now 2 rows (old revoked + new) |
| 13-c1-controller-new-node-detail | New node detail (still shows hostname) |
| 14-c2-controller-node-revoked-toast | Cycle 2 revoke |
| 15-c2-controller-join-code-qr | Cycle 2 join code |
| 16-c2-node-stopped-revoked-banner | Node stopped (shows GPU "Not available") |
| 17-c2-node-rejoin-done-run3 | Wizard done as Victus-run3 |
| 18-c2-controller-nodes-3-entries | 3 rows; new one named Victus-run2 (name lag bug) |
| 19-c2-controller-node-detail-run2 | Detail shows Victus-run2 |
| 20-c3-controller-revoke-confirm-hostname | Dialog asks for hostname, page shows custom name (bug) |
| 21-c3-controller-invites-3-used | Invites: "Created by" = raw user id (bug) |
| 22-c3-controller-join-code-closeup | Join code close-up |
| 23-c3-node-leave-tab | Leave tab |
| 24-c3-node-rejoin-shares-named | Share step with name filled |
| 25-c3-node-home-connected-run4-no-gpu | Connected as Victus-run4; NVIDIA GPU not available (AMD laptop) |
| 26-c3-controller-invites-4-used | 4 used invites |
| 27-c3-controller-nodes-4-entries-name-lag | 4 rows, 1 online, shows Victus-run3 (lag) |
| 28-c3-controller-overview-final | Final overview: 4 total, 1 online, 3 revoked, no GPU |

## 06-lead-controller (36, LEAD) - real fabric during Run 2B
| Files | Shows |
|---|---|
| 01-overview | Overview with 4 online, GPU utilisation, clock alerts |
| 02-nodes | 9 nodes (4 online, 5 revoked) |
| 03-node-L1-live | L1 live, GPU 100 % |
| 04-jobs | 50 run2b-image jobs |
| 05-benchmarks | 20 campaigns |
| 06-planner-calibration / 07-planner-models | Planner tabs |
| 08..11-node-L2-* | L2 (RTX 3050 A): live, hardware, network peer links, caches & policy |
| 12..14-job-finished-* | Finished split job (3 nodes): logs, chunks, plan |
| 15..16-benchmark-campaign-* | Campaign overview + spec |
| 17-settings-invites | Invites |
| 18..23-job-running-* / job-manifest | Running job: logs, chunks, attempts, timeline, outputs, manifest |
| 24..28-campaign-* | Campaign runs, labels, charts, quality, download |
| 29-planner-calibration-versions / 30-planner-evaluation-report | Planner history + evaluation |
| 31-submit-job | Submit job form |
| 32..36-settings-* | Account, API tokens, Secrets, Data, Users |

## Bugs seen (DEV)
- Node display name lags one join behind on the dashboard (18, 27).
- Revoke dialog asks for hostname while page shows the custom name (20).
- Invites "Created by" shows raw user id (21, LEAD 17).
- Agent console Dashboard link uses https URL Chrome can't open here; use http://127.0.0.1:47111.
- LEAD: Nodes list shows GPU "none" for L2 which has an RTX 3050 A (LEAD 02 vs 09).
