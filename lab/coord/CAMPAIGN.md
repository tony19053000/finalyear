# Lab campaign V0.2 -> V1.0 (overview)

The whole open lab work of the roadmap, run by one lab agent per laptop (D426) on **Wi-Fi only** (D425), on the
published **v0.17.0** packages. Steps: `lab/coord/L1..L4/TODO.md` ("Full campaign" section). Rules:
`lab/coord/RULES.md` (agents run the whole TODO on their own; they stop only for HUMAN steps, blocking
failures, WAITs > 30 min, the end). Mode 2/4 work (V0.11-V0.13, V0.16, 0.2.8, 0.2.9) is deferred: not here.

## Laptops

| | OS | Role |
|---|---|---|
| L1 | Linux, JARVISM3, 192.168.31.13, RTX 3050, Docker | controller `.deb` + agent `.deb`; runs every `compute` / report command |
| L2 | Windows 11, AILESH, 192.168.31.155, RTX 2050, **no Docker** | agent; Windows browser / Windows `compute`; V0.10 kill victim |
| L3 | unknown (round 1 says) | agent; timed fresh install (if Windows); V0.7 revoke victim; demo 05 laptop |
| L4 | unknown, arrives later | agent; S1 catch-up; V0.5 resume victim; needed for V1.0 |

## Phases (in order)

| Phase | Roadmap | Laptops | Attended | Unattended |
|---|---|---|---|---|
| P0 prep | - | all | 0.5 h | |
| V0.2 spikes S1/S2/S3/S6/S7, GPU, network facts | 0.2.1-0.2.7, 0.2.10, 0.2.11 | all | 5 h | |
| V0.17a install v0.17.0 + join (timed Windows install on L3) | 0.17.1-0.17.5, 0.17.8 W1 | all | 1.5 h | |
| V0.3 inventory, Wi-Fi drop, agent restart, controller kill | 0.3.2, 0.3.7, 0.3.13, 0.3.15 | all | 1.5 h | |
| V0.4 examples on every laptop, GPU job, latency, cancel | 0.4.8, 0.4.15 | all | 2 h | |
| V0.5 5 GB data, iperf3, peer, resume, prepare | 0.5.11 | all | 2.5 h | |
| V0.6 network matrix, Local-vs-offload, partition/kill | 0.6.13 (E/F folded into the soak) | all | 1.5 h | |
| V0.7 refusals, revoke, captures, Windows ACLs/secrets | 0.7.12 (+ G3/G4) | all | 2.5 h | |
| V0.8 visual review (whole team), ui_perf, Windows browser | 0.8.10 | all + team | 2 h | projector later |
| V0.9 Windows bench tests, conditions, owner guard, **night 1** `lab-v1-data` | 0.9.10 | all | 2 h | 5-7 h |
| V0.10 Mode 3 c1-c5, extras | 0.10.10 | all | 2.5 h | |
| V0.14 calibrate, CLI vs dashboard | 0.14.8 (part) | L1 | 1 h | |
| V0.15 train, gate, evaluate, unseen workloads | 0.15.11, 0.14.8 | L1 (+ all online) | 4 h | offline compare, a few h |
| V0.17b upgrade / rollback / uninstall-keep-data | 0.17.6, 0.17.8 | all | 3 h | needs "this build" |
| V1.0 quiet checks, **72 h soak**, policy matrix (night 2), ablation, demo × 2 | 1.0.2, 1.0.3, 1.0.6, 1.0.7 | all 4 | 8 h | 4 h + 74.5 h + 11 h |
| Final purges (only when the Lead closes the campaign) | 0.17.6 | all | 0.5 h | |

**Total:** about **45 h attended** (6-7 lab days) and **about 100 h unattended**; calendar **about 12-15 days**
if every phase runs back to back (the soak alone is 3 days + 2 h).

Order notes:
- V0.17a comes right after V0.2: every later phase runs on the installed fabric (rule of
  `lab/checklists/V1.0-campaign.md` Day L2: the older checklists' "start the fabric" part B is replaced).
- **One overnight campaign** (`lab-v1-data`, V1.0 N1) serves V0.9, V0.14 and V0.15. `cpu.sort` and
  `split.csv_aggregate` stay **unseen** until the ML set is trained (V0.15 phase).
- 0.3.15's 24 h soak and 0.6.13's E/F runs are covered by the V1.0 72 h soak (V1.0-campaign L3.2).
- Ethernet halves are NOT RUN (D425): V0.5 criterion 2, V0.5 part G second half, V1.0 A7.

## Signals (written as `SIGNAL: ...` in the emitting laptop's REPORT.md)

| Signal | Emitted by | Waited for by |
|---|---|---|
| `Lx-ready ip= os= host=` | every laptop, P0 | everybody |
| `s1-serve-up Lx ip=` / `s1-client-done Lx` / `s1-client-done Lx-to-L4` | S1 servers / clients | the next S1 client |
| `s2-serve-up Lx` / `s2-done A-to-B` | S2 server / client | the other side |
| `s3-serve-up L1` / `s3-done L2` | L1 / L2 | L2 / L1 |
| `v02-raw-pushed Lx` | every laptop | L1 report builds |
| `controller-up url= fp=` | L1 | every agent install |
| `installed Lx`, `joined Lx node=` | agents | L1 (invites, fabric-up) |
| `fabric-up nodes=` | L1 | - |
| `clock-offset Lx ms=` | agents, V0.4 | L1 latency step |
| `v04-g-done Lx` | Windows laptops | L1 |
| `iperf3-server-up` / `iperf3-done L2` | L1 / L2 | L2 / L1 |
| `v06-d-done` | L2 | L1 |
| `clock-unix Lx t=`, `v07-icacls-done`, `capture-start`, `capture-stop`, `capture-pushed`, `rejoined L3 node=` | V0.7 | L1 / L2 |
| `policy-ok Lx` | agents, V0.9 and V0.10 | L1 campaigns |
| `night-1-started camp=`, `night-2-started`, `policy-matrix-done` | L1 | - |
| `v017b-done Lx` | agents | L1 |
| `soak-facts Lx`, `soak-plan run= t0=`, `soak-sampler-up Lx`, `soak-chaos-up Lx`, `soak-files-pushed Lx`, `soak-cleanup-done Lx` | soak | L1 / agents |
| `demo-laptop-ready L3` | L3 | L1 demo 05 |
| `Vx.y-done` (V0.2 ... V1.0) | every laptop at each LEAD CHECKPOINT | the Lead |

## Builds

- **Previous / installed:** v0.17.0 (GitHub release `v0.17.0`, packages + `SHA256SUMS`).
- **"This build" for V0.17b (upgrade, rollback):** *not set yet - the Lead writes the version and where to get it
  here. Until then the V0.17b steps are SKIPPED.*

## Evidence

Raw outputs: `lab/reports/raw/<date>-<topic>/`; reports from the tools: `lab/reports/<date>-<topic>.md`;
each laptop's step log: `lab/coord/Lx/REPORT.md`. No passwords, join codes, tokens, keys or `/tmp/val`
anywhere in git. `scripts/release_check.py record-lab` is not run by the lab agents (the Lead decides, since
this campaign runs on v0.17.0, not on a `v1.0.0-rc`).

## Lead decisions (2026-11-01, answers to the planner's questions)

1. **Builds.** V0.2-V0.17a and V0.3-V0.15 evidence on the published **v0.17.0** packages is accepted. Before the
   V0.17b phase the Lead tags **v0.17.1** (session-12 fixes F1-F3 + lab fixes) = "this build" for upgrade tests.
   The V1.0 evaluation runs on a **v1.0.0-rc** the Lead cuts before the V1.0 phase (D406); only the final v1.0.0
   tag needs team approval (Q54).
2. **Revision-2 Windows installer (W3 rollback):** CI builds `MeshForge-Agent-Setup-<ver>-2.exe`; the Lead
   attaches it to the v0.17.1 release. Until then W3 WAITs on CAMPAIGN.md §Builds.
3. **Non-fresh install timing (V0.17 criterion 1)** on L3/L4 is accepted; record what was already installed
   (uv/Python/git). No VM needed.
4. **V0.3-V0.10 run with 3 laptops** if L4 is late. L4 joins when it arrives.
5. **Follow the approved V1.0 brief:** the 24 h V0.3 soak and V0.6 E/F are covered by the 72 h soak.
6. **v0.17.0 controller has the `ml` extra** (D423 is in the tag, `ROLE_EXTRAS["controller"] = ("controller","ml")`).
   If C9.10-g still says `can train False`, that is a BUG: report it, block.
7. **Paths.** Linux installed agent: config `/etc/meshforge/config.toml`, data under `/var/lib/meshforge/`
   (identity `/var/lib/meshforge/identity/node`). Find the cache/CAS folder under `/var/lib/meshforge/`
   with `ls`, and never delete outside it. Windows: config `C:\ProgramData\MeshForge\config\config.toml`.
   Windows firewall: check with `Get-NetFirewallRule -DisplayName *MeshForge* | Get-NetFirewallPortFilter`
   and report which ports it opens.
8. **C17-o controller rollback** is acceptable: fresh `db backup` first, the human decides.
9. **L1 lab session uses a second clone** (`~/mf-lab-L1`), never the Lead's checkout.
