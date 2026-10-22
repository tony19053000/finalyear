# DEVELOPMENT ROADMAP — MeshForge Compute Fabric

> **Status:** v1 — **Approved by team** (2026-09-15)
> **Nature:** a living plan and progress tracker — **not** a rigid spec. When reality disagrees, update this file (and log it in [Design change log](#design-change-log)).
> **Reads with:** [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md) · [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`STATUS.md`](STATUS.md) · [`adr/`](adr/)
> **Budget (2026-09-20):** ~**10 weeks**, one Claude account, ~9 sessions/week → **~90 sessions**. Scope is set by
> [ADR-0012](adr/0012-scope-ship-mode3-complete.md): **ship Mode 3 complete and production-grade** (Mode 1 is its
> foundation), with the AI planner, telemetry, security and dashboard **in full**. Mode 2 and Mode 4 are **deferred**.
> Superseded: the original "no deadline, scope is never cut for time" rule. Quality bar is *polished, finished, really working*.

---

## Live status

| Field | Value |
|---|---|
| **Current version** | **V0.3 — started 2026-09-20; G1–G7 closed agent-side — all agent-side V0.3 work is done** (0.3.1, 0.3.3, **0.3.4**, 0.3.5, 0.3.6, 0.3.8, 0.3.9, 0.3.10, 0.3.11, 0.3.12, 0.3.14 `[x]`; 0.3.2, 0.3.7, **0.3.13** `[~]` on 🧑 evidence) while V0.2 waits on 🧑 lab runs; V0.2 — in progress (started 2026-09-15); V0.1 code-complete, awaiting 🧑 GitHub CI proof (0.1.4, 0.1.10) (the exit-139 bug that would have made it red is fixed, 2026-09-24) |
| **Current task** | **V0.3 closes on the 🧑 0.3.15 lab run** (kit: `lab/checklists/V0.3-fabric.md`; includes the 24 h soak). G7 closed 2026-09-25 (0.3.4 `[x]`, reviewer APPROVE). Agent-side next: **prepare the V0.4 brief** (Lead), opening with 0.4.16 (explicit Goodbye) and 0.4.17 item 2 (extract report handlers from `manager.py`) before any new handler lands. |
| **Parallel human tasks** | hardware inventory of laptops 2–4 (**blocking**) · approve ADR-0012 · GitHub push + CI proof · buy Ethernet switch · decide ADR-0011 |
| **Blockers** | See [Blockers & problems](#blockers--problems-discovered) |
| **Next recommended task** | Write the **V0.4 brief** (`docs/briefs/V0.4-remote-execution.md`: Lead decisions, task groups, first group = 0.4.16 + 0.4.17 refactors). 🧑 in parallel: `lab/inventory.py` on laptops 2–4 (still the top blocker) · **the 0.3.15 lab run** with `lab/checklists/V0.3-fabric.md` (start the 24 h soak early — calendar-bound) · team approval of ADR-0012 / ADR-0013 and a look at G6's D19/D23/D29 · **decide NB-2** · GitHub push + CI proof · the 0.3.2 Windows unit run · the 0.3.13 Windows logoff check. |

**Legend:** `[ ]` not started · `[~]` in progress · `[x]` completed (**only** after tests + success criteria pass, with evidence) · `[-]` dropped (reason logged) · 🧑 = requires a human on physical hardware · 🔬 = spike / measurement.

---

## Contents
- [How agents use this roadmap](#how-agents-use-this-roadmap)
- [Milestones & dependency graph](#milestones--dependency-graph)
- [Quality gates (apply to every version)](#quality-gates-apply-to-every-version)
- **M1 — Foundation:** [V0.1](#v01--foundations--engineering-system) · [V0.2](#v02--lab-setup--feasibility-validation) · [V0.3](#v03--fabric-core-controller--agent)
- **M2 — Mode 1 product:** [V0.4](#v04--remote-execution-core-mode-1-foundation) · [V0.5](#v05--data-plane--environments) · [V0.6](#v06--scheduling--reliability--mode-1-complete) · [V0.7](#v07--security--pairing) · [V0.8](#v08--dashboard--cli-v1) · [V0.9](#v09--benchmark--telemetry-system-v1)
- **M3 — Parallel:** [V0.10](#v010--mode-3-task-splitting) *(V0.11/V0.12/V0.13 deferred — ADR-0012)* · [V0.11](#v011--mode-2-remote-gpu) · [V0.12](#v012--mode-4-combined-gpu--inference) · [V0.13](#v013--mode-4-combined-gpu--training)
- **M4 — Intelligence:** [V0.14](#v014--unified-planner-v2) · [V0.15](#v015--ml-planner)
- **M5 — Product:** *(V0.16 deferred — ADR-0012)* [V0.16](#v016--model-splitting-mode-4-stage-3) · [V0.17](#v017--productization) · [V1.0](#v10--release-hardening--final-evaluation)
- [Cross-cutting tracks](#cross-cutting-tracks) · [Risk register](#risk-register) · [Blockers](#blockers--problems-discovered) · [Design change log](#design-change-log)

---

## How agents use this roadmap

```mermaid
flowchart LR
    A["Read STATUS.md + this file"] --> B["Pick next unblocked [ ] task in current version"]
    B --> C["Mark [~], note owner/session in STATUS.md"]
    C --> D["Design note (ADR if architectural)"]
    D --> E["Implement in small commits"]
    E --> F["Automated tests (unit / component / cluster)"]
    F --> G{"Needs physical laptops?"}
    G -->|yes| H["Write exact 🧑 lab steps; human runs; paste results into lab report"]
    G -->|no| I["Independent review (different agent/session)"]
    H --> I
    I -->|"reject"| E
    I -->|"approve"| J["Mark [x] with evidence link; update STATUS.md"]
```

**Rules**
1. Work top-down within a version; tasks may run in parallel when they have no dependency.
2. Never start a version before the previous version's **success criteria** are re-checked (or the dependency graph explicitly allows parallel work).
3. `[x]` requires **evidence**: test run output, CI link, or lab report path (`lab/reports/…`). No evidence → not done.
4. Every 🧑 task must state: which laptops, exact commands, expected output, and what to paste back.
5. Implementers never approve their own work.
6. Architectural decisions → `docs/adr/`. Scope/order changes → this file's [Design change log](#design-change-log).
7. Success thresholds marked *(calibrate)* are initial targets; revise them once V0.2 measurements exist, and log the change.

---

## Milestones & dependency graph

| Milestone | Versions | Outcome |
|---|---|---|
| **M1 Foundation** | V0.1–V0.3 | Repo, lab, measured constraints, live fabric of real laptops |
| **M2 Mode 1 product** | V0.4–V0.9 | Finished Job Offloading: fast, cached, reliable, secure, UI, benchmark data flowing |
| **M3 Parallel** | V0.10 | **Task Splitting (Mode 3)** — the shipped mode. V0.11–V0.13 (Mode 2, Mode 4) **deferred, ADR-0012** |
| **M4 Intelligence** | V0.14–V0.15 | Cost-based planner over Local + Mode 1 + Mode 3, then **ML-assisted** |
| **M5 Product** | V0.17–V1.0 | Installers, hardening, final evaluation. V0.16 (model splitting) **deferred, ADR-0012** |

```mermaid
flowchart TD
    V01["V0.1 Foundations"] --> V02["V0.2 Lab + spikes"]
    V02 --> V03["V0.3 Fabric core"]
    V03 --> V04["V0.4 Remote execution"]
    V04 --> V05["V0.5 Data plane + envs"]
    V05 --> V06["V0.6 Scheduling + reliability"]
    V06 --> V07["V0.7 Security + pairing"]
    V06 --> V08["V0.8 Dashboard + CLI"]
    V06 --> V09["V0.9 Benchmarks + telemetry"]
    V06 --> V010["V0.10 Mode 3 Task Splitting"]
    V06 -.-> V011["V0.11 Mode 2 Remote GPU — DEFERRED"]
    V011 -.-> V012["V0.12 Mode 4 inference — DEFERRED"]
    V010 -.->|work queue| V012
    V012 -.-> V013["V0.13 Mode 4 training — DEFERRED"]
    V09 --> V014["V0.14 Planner v2 (Local + M1 + M3)"]
    V010 --> V014
    V014 --> V015["V0.15 ML planner"]
    V012 -.-> V016["V0.16 Model splitting — DEFERRED"]
    V07 --> V017["V0.17 Productization"]
    V08 --> V017
    V015 --> V10["V1.0 Release + evaluation"]
    V016 -.-> V10
    V017 --> V10
```
After V0.6, up to four tracks can proceed in parallel (security, UI, benchmarks, Mode 3/Mode 2) — useful when several agent sessions work at once.

---

## Scope & budget (ADR-0012, 2026-09-20)

**Decision:** ship **Mode 3 (Task Splitting)** complete and production-grade — nothing stubbed. Mode 1 ships with it,
because Mode 3 is Mode 1 extended from one laptop to many. The **AI planner, telemetry, security and dashboard are kept
in full**. **Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16) are deferred** and revisited once Mode 3 is finished.

**Budget:** ~10 weeks × ~9 sessions × 5 h ≈ **90 sessions**; realistic capacity **140–180 tasks** (measured rate, with
the token-efficiency rules below). **Target ≈ 1.5 tasks per session.**

| # | Block | Tasks | Running |
|---|---|---|---|
| 1 | V0.3 fabric core (controller ⇄ agent) | 15 | 15 |
| 2 | V0.4 remote execution core (Mode 1) | 15 | 30 |
| 3 | V0.5 data plane & environments — full | 11 | 41 |
| 4 | V0.6 scheduling & reliability (Mode 1 complete) | 13 | 54 |
| 5 | **V0.9 telemetry & benchmarks** *(pulled before V0.10)* | 10 | 64 |
| 6 | **V0.10 Mode 3 task splitting** | 10 | 74 |
| 7 | V0.14 planner v2 (Local + M1 + M3) | 8 | 82 |
| 8 | **V0.15 ML planner** | 10 | 92 |
| 9 | V0.7 security & pairing — full | 11 | 103 |
| 10 | V0.8 dashboard & CLI v1 — full | 10 | 113 |
| 11 | V0.17 productization | 8 | 121 |
| 12 | V1.0 release hardening & final evaluation | 8 | **129** |

**129 tasks against 140–180 capacity — 11–51 tasks of margin**, deliberately held for lab-coordination delays,
hardware failures and the ML planner needing more telemetry than expected.

**Why V0.9 moves before V0.10:** V0.15 needs thousands of telemetry runs to train on. Built late, there is no training
data at the end. Built early, data accumulates throughout Mode 3 development.

**If the budget collapses toward ~5 weeks**, cut in this fixed order: V1.0 → V0.17 → thin V0.8 to a read-only
dashboard → drop V0.5 polish (0.5.6 named inputs, 0.5.9 GC). **V0.10, V0.15 and V0.9 are never cut — they are the
project.** If margin survives instead, the stretch is **V0.11 (Mode 2)**.

**Token-efficiency rules (half the reason this fits):** one *coherent group* per implementer run, not one task ·
front-load each brief with the standing objection checklist so reviews pass in 1–2 rounds · the Lead makes doc edits
and small mechanical fixes directly, with no subagent · run the ~6-minute component suite once per task group, not
once per agent.

---

## Quality gates (apply to every version)

A version is **complete** only when all apply:

```text
[ ] all tasks [x] (or [-] with logged reason)
[ ] lint + type-check clean; CI green on Linux and Windows
[ ] unit + component tests pass; coverage of new core logic ≥ 80 %
[ ] cluster tests pass (multi-process local fabric with fault injection) where relevant
[ ] 🧑 lab test on real laptops passes where relevant; report saved in lab/reports/
[ ] failure behaviour exercised (not just the happy path)
[ ] logs verified (correlation IDs present, no secrets)
[ ] telemetry recorded for new execution paths
[ ] docs updated (ARCHITECTURE, ADRs, user-facing docs, STATUS)
[ ] every success criterion below demonstrated with evidence
```

**Test tiers:** `unit` (pure logic) · `component` (real gRPC/DB/Docker, one process each) · `cluster` (controller + N agents as local processes, Toxiproxy faults) · `lab` 🧑 (real laptops) · `gpu` (needs NVIDIA GPU) · `e2e` (UI, Playwright) · `soak` / `chaos` (long-running).
**Test rules (0.1.8):** a test's tier = its first folder under `tests/` (auto-marked; mismatch or missing tier fails collection) · `lab`, `gpu`, `soak`, `chaos` run only with `--run-<tier>` · Docker-dependent tests use the `docker` marker / `docker_available` fixture and skip cleanly without Docker · Postgres tests use the shared `pg_settings` fixture (one container per session, fresh database per test) · all warnings are errors; default timeout 300 s.

---

# M1 — FOUNDATION

## V0.1 — Foundations & engineering system

**Objective:** a repository where AI agents can work safely and consistently: tooling, CI, conventions, contracts, dev database, docs.

**Concepts introduced:** monorepo package layout · lockfiles · protobuf contracts & codegen · schema migrations · structured logging · test tiers · CI on two OSes.
**Dependencies:** Git, GitHub, uv, Python 3.12, Docker (for Postgres), grpcio-tools, ruff, mypy, pytest.
**Prerequisites:** team approval of ARCHITECTURE + this roadmap.

**Tasks**
- [x] **0.1.1** `git init`, `.gitignore`, `.gitattributes` (LF), README, license decision 🧑(team decided 2026-09-15: no license file for now; private GitHub repo, human pushes) — *evidence: commit `chore(0.1.1)`, reviewer APPROVE; git check-ignore/check-attr verified*
- [x] **0.1.2** `pyproject.toml` with uv; extras `controller`, `agent`, `gpu`; dev tools as uv dependency group `dev`; Python 3.12 pinned — *evidence: commit `build(0.1.2)`, tester PASS (fresh `uv sync --locked`, extras resolve incl. Windows, py3.11/3.13 rejected), reviewer APPROVE*
- [x] **0.1.3** package skeleton `src/meshforge/{common,controller,agent,gpu_service,sdk,cli}`; entry points `compute`, `meshforge-controller`, `meshforge-agent` — *evidence: commit `feat(0.1.3)`, tester PASS (48 tests incl. clean base-only install), reviewer APPROVE*
- [~] **0.1.4** import-boundary check (controller ⟂ agent; sdk → common only) enforced in CI — *check done (commit `test(0.1.4)`, tester PASS, reviewer APPROVE after fix round); stays [~] until the 0.1.10 CI job runs it*
- [x] **0.1.5** `proto/meshforge/v1/{common,agent,gpu}.proto` skeletons + codegen script; generated code reproducible — *evidence: commit `feat(0.1.5)`, tester PASS (determinism incl. CRLF/space paths, real grpc.aio bidi session + GPU unary on localhost, base-install import, FailureType parity 22/22), reviewer APPROVE after fix round*
- [x] **0.1.6** `common/`: config (pydantic-settings: TOML + env), IDs (ULID), structlog JSON logging with correlation fields, error base classes, SHA-256 helpers — *evidence: commit `feat(0.1.6)`, tester PASS round 3 (334 unit tests in tests/unit/common incl. redaction attack + linear-time perf gates), reviewer APPROVE round 3*
- [x] **0.1.7** `deploy/docker-compose.dev.yml` (PostgreSQL 16) + SQLAlchemy base + Alembic baseline migration — *evidence: commit `feat(0.1.7)`, tester PASS (README flow verbatim, 13 component tests on real PG 16 incl. upgrade→downgrade→re-upgrade, offline --sql, special-char passwords), reviewer APPROVE*
- [x] **0.1.8** pytest config with markers (`unit component cluster lab gpu e2e`), testcontainers fixture for Postgres — *evidence: commits `test(0.1.8)` + `fix(0.1.8)`; tester PASS (fresh copy + plain `uv sync` → `pytest -m unit` 594–604 passed / 0 failed; Docker-less full run 0 failed; import guard, gpu/slow rules; DEFECT-0.1.8-01 container leak fixed: SIGINT ×1/×2, SIGTERM, SIGKILL, pytest-timeout signal+thread, next-run sweep — no leaked containers), reviewer APPROVE (2 rounds)*
- [x] **0.1.9** ruff + mypy (strict on `common`, `controller`, `agent`) + pre-commit — *evidence: commit `build(0.1.9)`, tester PASS (pre-commit rejected lint/type/boundary/private-key/large-file/CRLF bad commits in scratch repo; good commit passes), reviewer APPROVE*
- [~] **0.1.10** GitHub Actions: lint, types, unit (Linux + Windows), component (Linux) — *workflow done: local tester PASS (every ci.yml step reproduced in fresh copy; SHA pins verified; bad lint/type/boundary each fail their step), reviewer design OK; stays [~] until 🧑 first real GitHub run is green incl. Windows*
- [x] **0.1.11** `CONTRIBUTING.md` for agents: task template, commit convention, PR/review checklist, evidence rules — *evidence: commit `docs(0.1.11)`, reviewer APPROVE (docs-only, commands reconciled with 0.1.8–0.1.10)*
- [x] **0.1.12** docs scaffold: `STATUS.md`, `adr/README.md` + initial ADRs, `LAB.md` template — *evidence: commit `docs(0.1.12)`, reviewer APPROVE (docs-only, tester skipped per CLAUDE.md §4b)*

**Testing:** CI pipeline itself; smoke test `compute --version`; migration up/down on empty DB.
**Success criteria**
1. Fresh clone → `uv sync` → `pytest -m unit` passes on Linux **and** Windows.
2. CI green on `main`; a deliberately bad commit (lint/type/import-boundary) is rejected.
3. `alembic upgrade head` and `downgrade base` succeed on a clean PostgreSQL.
4. Proto codegen is deterministic (regenerating produces no diff).

**Deliverable:** clean skeleton repository with guardrails.
**Unlocks:** V0.2 (lab scripts live in `lab/`), V0.3 (contracts + config + DB ready).

---

## V0.2 — Lab setup & feasibility validation

**Objective:** make the four laptops a controlled lab and **measure the real constraints** before committing to designs that depend on them.

**Concepts introduced:** bandwidth/latency measurement · experiment reproducibility · GPU driver/CUDA stack · container GPU access · tensor serialization cost · collectives.
**Dependencies:** NVIDIA drivers, PyTorch CUDA builds, Docker / Docker Desktop, NVIDIA Container Toolkit, iperf3, gigabit switch + cables.
**Prerequisites:** V0.1 (repo for `lab/` scripts).

**Tasks**
- [~] **0.2.1** 🧑 Fix NVIDIA driver on Laptop 1 (Secure Boot MOK enrollment); verify `nvidia-smi` + `torch.cuda.is_available()` — *driver fixed 2026-09-19: `nvidia-smi` → RTX 3050 Laptop GPU, driver 595.91.07, 4096 MiB, compute cap 8.6, Secure Boot still enabled; `torch.cuda.is_available()` pending torch install (ADR-0011 / 0.2.3)*
- [~] **0.2.2** `lab/inventory.py` — cross-platform hardware/software inventory → JSON; 🧑 run on all 4 laptops → `docs/LAB.md` — *code done: commit `feat(0.2.2)`, tester PASS round 2 (798 unit; lab unit+component 234 passed incl. adversarial tree-kill, privacy, 60 s run budget), reviewer APPROVE; stays [~] until 🧑 runs on all 4 laptops (JSON in `lab/reports/inventory/`) incl. Windows tree-kill check*
- [ ] **0.2.3** 🧑 GPU stack on every NVIDIA laptop: driver, PyTorch CUDA wheel check (decide per-platform torch index via `[tool.uv.sources]` — PyPI Windows torch is CPU-only; add ADR + startup CUDA check), Docker (Desktop on Windows) + GPU container test `docker run --gpus all … nvidia-smi`
- [ ] **0.2.4** 🧑 Network: switch + cables, DHCP reservations/hostnames, Windows firewall profile set to Private; record link speeds
- [~] **0.2.5** 🔬 **S1** network matrix: iperf3 all pairs (Wi-Fi and Ethernet), RTT, jitter, loss → `lab/reports/YYYY-MM-DD-S1-network.md` — *code done: `lab/spikes/s1_network.py` + shared `_harness/` (commit `feat(0.2.5)`); tester PASS round 2, reviewer APPROVE after fix round 3 (312 unit, 52 component S1 tests); stays [~] until 🧑 Wi-Fi + Ethernet matrices are run on all 4 laptops (needs 0.2.4 switch)*
- [~] **0.2.6** 🔬 **S2** cross-OS reachability: native Windows Python gRPC + HTTP server reachable from Linux and vice-versa, without WSL — *code done: `lab/spikes/s2_crossos.py` (serve / client / report) + `_s2/` + `S2.md` + `lab/README.md` S2 subsection (commits `wip(0.2.6)` ×8, `feat(0.2.6)` `633d474`); tester round 1 pinned 6 defects (port sharing via SO_REUSEPORT, start/stop race, warmup errors demoting verdicts, NaN blob size, serve Ctrl-C exit code, malformed case entries), round 2 confirmed the fixes and found 1 shared-harness crash (`_harness/report.py`, fixed, covered in `test_spike_report.py`); reviewer REQUEST_CHANGES ×4 (a loopback run rendered as a real cross-OS row → `⚠ same machine` marker + caveats + outbound-only TBD; idle death time measured when noticed not when it happened → `failed_at` stamp; operator declaration phrased as measurement → reworded, `client_listens`/`basis`; every result-file field guarded against corrupt shapes) then APPROVE on pass 5 (1033 unit, 42 S2 component, whole lab suite 226 component green); real loopback runs recorded in `S2.md` §5 — loopback bounds the tool, not the question; stays [~] until 🧑 two-laptop runs Windows↔Linux, both directions, Wi-Fi **and** Ethernet (procedure in `lab/spikes/S2.md` §3, incl. the Windows second-`serve` check)*
- [~] **0.2.7** 🔬 **S3** transport throughput: gRPC bytes streaming vs raw TCP vs HTTP range, 1 MB → 2 GB → decides tensor/data transport (update ADR-0003/0005) — *code done: `lab/spikes/s3_transport.py` + `_s3/` + `S3.md` + `lab/README.md` S3 subsection (commits `wip(0.2.7)` ×3, `feat(0.2.7)` `81d620f`); tester PASS (24 adversarial tests), reviewer APPROVE on the second pass after 3 blocking fixes (245 unit, 62 component); real loopback run verified end to end (exit 0, report renders, limit probe correct) — loopback bounds the tool, not the link; stays [~] until 🧑 two-laptop runs on Wi-Fi **and** Ethernet (procedure in `lab/spikes/S3.md` §2; needs 0.2.4 switch for the Ethernet half)*
- [ ] ~~**0.2.8**~~ **DEFERRED (ADR-0012, with Mode 2)** — 🔬 **S4** `torch.export` round-trip: export ResNet-50 / BERT-base on one laptop, load and run on another laptop's GPU; verify equality; measure per-call latency vs payload
- [ ] ~~**0.2.9**~~ **DEFERRED (ADR-0012, with Mode 2)** — 🔬 **S5** collectives: `torch.distributed` gloo all-reduce Windows↔Linux; if unusable, minimal TCP ring prototype; measure 10 MB / 100 MB all-reduce
- [~] **0.2.10** 🔬 **S6** environments: Docker Desktop GPU PyTorch container on Windows; uv venv creation time with/without cache — *code **done** 2026-09-20: `lab/spikes/s6_environments.py run|report` + `_s6/` + `lab/spikes/S6.md`; commits `f7b094c` `f3364b8` `bfce2fa` `fb9376a`; **tester PASS (no defects, 16 adversarial tests)**, **reviewer APPROVE (no blocking findings)**. Cases: docker-facts (pre-case block) · docker-start · docker-pull-cold · docker-gpu-smi · docker-gpu-torch · venv-cold · venv-warm · venv-start. Real Laptop 1 (Linux) run in `lab/reports/2026-09-20-S6-environments.md`: GPU **is** visible in a container (nvidia-smi 361 ms), cold pull 9.7 s, venv cold 3.56 s vs warm 0.10 s — every §5 number cross-checked byte-for-byte against the raw JSON. **Stays `[~]`:** `docker-gpu-torch` was skipped as typed `insufficient_disk` (5.8 GB free, image ~3.7 GB), so `torch-cuda-in-container` is **TBD on every laptop**, Q4 is unanswered, and Windows/Docker Desktop has **zero** evidence. Closes when the 🧑 Windows runs on Laptops 2–4 and a Laptop 1 re-run with ≥ ~11 GB free are pasted back (procedure: `lab/spikes/S6.md` §2)*
- [~] **0.2.11** 🔬 **S7** hardware scores: CPU GFLOPS, memory bandwidth, GPU fp32/fp16 TFLOPS micro-benchmarks per laptop — *code done: `lab/spikes/s7_hardware.py` + `_s7/` + `S7.md` (commit `feat(0.2.11)`); tester PASS (69 adversarial tests), reviewer APPROVE after one fix round (280 unit, 29 component); GPU/torch paths covered only by fake-torch tests until ADR-0011 torch install; stays [~] until 🧑 real runs on all 4 laptops (check `threads_effective == cpu count` for memory-bandwidth and `allow_tf32 == false` for gpu-float32)*
- [x] **0.2.13** Spike report CLIs must not destroy hand-written prose: `report --out <existing file>` rewrites the whole file from `TEMPLATE.md`, blanking human sections (Goal, Background load, Commands run, Conclusion). Affects all five CLIs — `s6_environments.py:338`, `s2_crossos.py:581`, `s7_hardware.py:325`, and S1/S3 via `atomic_write_text`. Fix once, shared: refuse an existing `--out` without `--force`, or preserve the human-written sections. — *raised by the 0.2.10 reviewer (non-blocking #1); it nearly cost the S6 prose once* — ***done 2026-09-20:** shared `write_report(path, text, force=)` in `_harness/report.py`, called by all five CLIs (all now atomic). An existing `--out` is **merged** (generated regions from the fresh render, human regions from disk, `WriteResult` prints which were kept); a malformed file or another spike's report is **refused** with a typed `ReportMergeError` and left byte-identical; `--force` regenerates. Tester round found 3 defects (glued human table dropped · lone surrogate → traceback · UTF-8 BOM refused forever), all fixed in `2fd3185`; reviewer **APPROVE**, no blocking findings; follow-ups #1/#3 applied by the Lead. Commits `e23d762` `ac5a65f` `2fd3185` + final. Gates: ruff + mypy clean, lab unit **1313 passed**, component suite green (see STATUS). Pure tooling — no 🧑 step. Documented loss: hand-edits *inside* a generated region (e.g. host-table speed/power cells) are replaced (`lab/README.md`).*
- [ ] **0.2.12** Consolidate findings: update ADRs, ARCHITECTURE open questions, and *(calibrate)* thresholds in this roadmap; choose controller laptop

**Testing:** every spike is a script under `lab/spikes/` producing a machine-readable result + markdown report; each run ≥ 5 repetitions with mean/stdev.
**Success criteria**
1. All NVIDIA laptops pass `nvidia-smi`, `torch.cuda.is_available()`, and GPU-in-container checks (or documented exception with fallback).
2. `docs/LAB.md` lists full inventory and the measured network matrix for Wi-Fi and Ethernet.
3. Every open question Q2–Q4 in ARCHITECTURE §27 has a recorded answer and decision.
4. Calibrated thresholds for V0.5, V0.10–V0.13 written into this roadmap.

**Deliverable:** working lab + evidence-based design constraints.
**Unlocks:** V0.3 on real hardware; informs transport, collectives, and all performance targets.

---

## V0.3 — Fabric core: controller ⇄ agent

**Objective:** real laptops form a live fabric — persistent identity, registration, hardware discovery, resource streaming, failure detection, automatic reconnection.

**Concepts introduced:** persistent node identity (keypairs) · bidirectional gRPC streaming · leases & failure detection · exponential backoff with jitter · reconciliation · resource time-series · mDNS discovery.
**Dependencies:** grpcio (aio), psutil, nvidia-ml-py, zeroconf, SQLAlchemy async, Toxiproxy (tests).
**Prerequisites:** V0.1; V0.2 S2 (cross-OS reachability) passed.

**Tasks**
- [x] **0.3.1** `agent.proto`: `Session` bidi stream; messages Hello, Heartbeat, ResourceUpdate, Command, Ack, Reconcile — *done 2026-09-20 (G1): additive V0.3 extension — `session_id` = 3 on both envelopes, `Hello{session_epoch, os, platform, hardware}`, `Heartbeat.agent_time`, `ResourceUpdate` fields 1–7, `Reconcile.replay_from_sequence`, `HelloAck` + `RejectCode`; V0.1 field numbers pinned by test. Tester PASS on the proto (8 adversarial tests), reviewer APPROVE. Commits `da27ff1` … `0acf7eb`.*
- [~] **0.3.2** Agent identity: keypair + `node_id` generated on first start, stored in OS-appropriate data dir with restricted permissions — *code done 2026-09-20 (G1, ADR-0013): `agent/identity.py` + `common/atomic.py` + `common/filelock.py`; P-256 PKCS#8 key + `node.json`, `load_or_create` / `next_epoch` / `reset` under `identity.lock`, exclusive create, epoch bounded to uint64, partial/corrupt identity is `IdentityError`, PEM blocks scrubbed from logs. Tester found 2 critical concurrency defects (first-create fork, lost epoch increment) + 1 minor — fixed `7b7e7ff`; reviewer found 1 blocking (next_epoch clobbering a reset identity) — fixed `0acf7eb`; APPROVE on pass 2. **Stays `[~]`** until the 🧑 Windows run: `uv run pytest tests/unit/agent -q` on any Windows laptop (exercises `icacls` + `msvcrt.locking`); paste the summary line.*
- [x] **0.3.3** Hardware discovery: CPU model/cores/threads, RAM, disks, OS, GPU(s) via NVML (model, VRAM, driver, compute capability), software versions — *done 2026-09-21 (G4): `agent/hardware/` (cpu/disks/gpu/software/summary + `deadline.py`), NVML primary with an nvidia-smi fallback, per-probe guards and a hard budget on an abandoned daemon thread so a wedged driver still lets the node enrol with `basic_hardware()`; fills `Hello.hardware`. Implementer → tester FAIL (R1 critical: replay dedupe accepted duplicates; H1 major: NVML had no timeout) → fix → tester FAIL (D-EVICT-1: LRU eviction dropped the dedupe cursor) → cleanup (10 items) → tester FAIL (ISO-1: the poison-row fix dropped history during an outage) → fix → **reviewer APPROVE** (delta review, no blocking). Gates: ruff/format clean, mypy 78 files, lint-imports 7 kept / 0 broken, gen_protos clean, unit **2611 passed** / 2 skipped / 2 xfailed, component agent+store+controller **126 passed**. Lead-verified independently: all five replay/restart/reconnect/zombie/bucket cases, the NB-1 ordering guard (fails when an await is inserted), and the ISO-1 pin by mutation. Commits `5fa761b` … `84b9a1b`.* **Real-laptop confirmation of criterion 1 is still the 🧑 0.3.15 lab run.**
- [x] **0.3.4** Hardware scores from S7 micro-benchmarks, run at first enrollment and on demand — *done 2026-09-25 (G7, brief §5g, decisions D30–D39). **Agent:** `agent/benchmarks/` (S7 kernels lifted, lab `scores`/`sizing` re-export them; `numpy` joins the `agent` extra, D30) measures CPU fp32 1T/all, fp64 all, memory triad, and per GPU fp32/fp16/device-copy in an **isolated child process** (tree-killed on timeout/budget/cancel/Ctrl-C, lowered priority, `CUDA_DEVICE_ORDER=PCI_BUS_ID`, device-mismatch → FAILED); every unmeasurable metric is a typed `UNAVAILABLE`/`FAILED` with a reason (`no_blas_backend`, `torch_not_installed`, …), never a 0. Local `scores.json` keyed by a hardware+backend fingerprint; runs once after the first ONLINE when the cache is invalid, and on demand via `meshforge-agent hardware score [--json] [--force]` (`ExitCode.BUSY = 7` for a second scorer). **Fabric:** `Hello.hardware_scores = 9` (cached run) + report payload `hardware_scores = 20` (replayed like other reports); controller validates (D38: finite, >0 for OK, bounded, printable; D39: GPU index must be declared in the same session's Hello — whole set rejected, stream stays up), stores in `node.hardware_scores` (migration `0005`) with fenced, `measured_at`-ordered writes; REST `scores` with `measured/pending/none` (pending vs none from the `hardware-scores` capability tag); `compute nodes` SCORES column + detail table through `_cell()`. Rounds: implementer A → implementer B (each resumed once after a usage limit) → tester **PASS** (2 defects: undeclared-GPU score accepted → D39 fix; DB-refused write lost until next Hello → documented) → tester round 2 **PASS** → reviewer **APPROVE** (no blocking; follow-ups **0.4.17**) → Lead capped Windows `taskkill` at 1 s. Evidence: unit **3725** passed exit 0; component **462** passed exit 0 (13 min 24 s, after all fixes); cluster **40** passed exit 0 (incl. scenario s: real agent subprocess → measured scores in `compute nodes --json`, cache re-used across restart); heartbeat test: node stays ONLINE under an all-threads benchmark; ruff, format, mypy src (105), lint-imports 7/0, gen_protos clean; mutation-checked fence, `measured_at` ordering, NaN rejection, D39, orphan kill, device mismatch, fingerprint. Laptop 1 (not a benchmark claim): CPU fp32 all-threads 574 GFLOP/s, triad 24 GB/s, RTX 3050 fp32 1.8–3.7 TFLOP/s run to run. Commits `dc4ebcf` … this close. 🧑 Windows check (not blocking `[x]`, tracked in STATUS): `hardware score` exit 0, a concurrent second scorer exits 7, Ctrl-C leaves no python child. Real per-laptop numbers come from the 0.3.15 lab run.*
- [x] **0.3.5** Resource monitor: CPU %, RAM, GPU util, VRAM free, disk free every 2 s; change-threshold reporting — *done 2026-09-21 (G4): `agent/resources/` sampler + report policy + monitor task via `send_report("resource_update", …)`; samples but does not send while the session is not ONLINE, and the first ONLINE sample is always reported (D9). Implementer → tester FAIL (R1 critical: replay dedupe accepted duplicates; H1 major: NVML had no timeout) → fix → tester FAIL (D-EVICT-1: LRU eviction dropped the dedupe cursor) → cleanup (10 items) → tester FAIL (ISO-1: the poison-row fix dropped history during an outage) → fix → **reviewer APPROVE** (delta review, no blocking). Gates: ruff/format clean, mypy 78 files, lint-imports 7 kept / 0 broken, gen_protos clean, unit **2611 passed** / 2 skipped / 2 xfailed, component agent+store+controller **126 passed**. Lead-verified independently: all five replay/restart/reconnect/zombie/bucket cases, the NB-1 ordering guard (fails when an await is inserted), and the ISO-1 pin by mutation. Commits `5fa761b` … `84b9a1b`.*
- [x] **0.3.6** Controller gRPC server (grpc.aio) + session manager; node registry tables; one process hosting FastAPI + gRPC — *done 2026-09-20 (G2): Alembic `0002_nodes` (`node`, `node_session`, named CHECKs, `onupdate`, `command_timeout`), `controller/registry.py`, `controller/session/` (servicer: Hello-first, protocol range, epoch rule vs persisted high-water mark + live session; manager: per-stream reader, bounded outbound queue, `message_id` dedupe, stale `session_id` dropped+counted, `limits.py` size caps; `writer.py` per-session ordered write chain so DB I/O never blocks the reader or timer; leases: one timer task), one process hosting `grpc.aio` + FastAPI `/healthz`, LAN-only bind (`common/netaddr`), graceful SIGINT/SIGTERM with one drain deadline, startup sweep `controller_restart`, ARCHITECTURE §23 ports table. Implementer (2 runs, usage-limit cut) → tester (2 defects: epoch replay after disconnect, uint64 epoch crash; fixed `35bc872`) → reviewer pass 1 REQUEST_CHANGES (3 blocking: DB I/O in the reader path, flaky test client, unbounded message sizes; fixed `a1014d5`) → **APPROVE pass 2**. Unit 2259, component store+controller 67, controller component file 10/10 runs.*
- [~] **0.3.7** Liveness: heartbeat 2 s, lease 3 × heartbeat; states ONLINE / SUSPECT / OFFLINE per ARCHITECTURE §12.3 — *pure logic done 2026-09-20 (G1): `controller/liveness.py` — `NodeState`, `LivenessEvent`, `next_state` (the §12.3 arrows as of G1), `LivenessConfig` 2 s / +0.5 s / ×3, `LeaseTracker` on an injected monotonic clock with one-shot MISSED/EXPIRED and `deadline()`. 48-pair table test + tester's boundary tests, reviewer APPROVE. **G2 must:** extend the table to the amended §12.3 (DRAINING→SUSPECT, SUSPECT/DRAINING→REVOKED, Hello-while-SUSPECT = RECONNECTED), wire one tracker per session to a single timer task, derive `HelloAck` intervals from `LivenessConfig`.* — *wired 2026-09-20 (G2): table extended to the amended §12.3; one `LeaseTracker` per session driven by the single lease-timer task; `HelloAck` intervals derived from `LivenessConfig`; component tests show SUSPECT/OFFLINE at the configured edges with persisted state matching memory. **`[~]` only for the 🧑 criterion-2 lab evidence (cable pull ≤ 8 s) in 0.3.15.***
- [x] **0.3.8** Agent reconnect with exponential backoff + jitter; Reconcile message on reconnect — *done 2026-09-21 (G3): `agent/session/{state,policy,replay,hello,client}.py`, `agent/runtime.py`, `meshforge-agent run | identity show|reset --yes`. One task owns the stream; `next_epoch` persisted before every attempt; `HelloAck` handling (STALE_EPOCH bump+retry ≤ 3, PROTOCOL_VERSION fatal, interval clamped to [0.1 s, 30 s]); heartbeats at the ack'd interval; Pong first; Ack every command; bounded replay ring for reports only + `Reconcile{replay_from_sequence}`; full-jitter backoff base 0.5 s / cap 15 s / reset 30 s; ABORTED `superseded` → exit CONFLICT (5). Tester: 1 defect (unbounded heartbeat interval) + storms / epoch discipline / hostile controller / identity races all PASS; reviewer pass 1 REQUEST_CHANGES (1 blocking: `replay_from_sequence` ≠ 0 when empty) → fixed → **APPROVE pass 2**. **Controller restart → agent ONLINE again in ~1–2 s in-process** (criterion 3; real-laptop evidence in 0.3.15). Commits `a934621` … `0f55af0`. Unit 2437, component agent 37.*
- [x] **0.3.9** Controller discovery: explicit address in config, fallback mDNS — *done 2026-09-21 (G3): `agent/discovery.py` (configured address wins; else zeroconf browse of `_meshforge-ctl._tcp.local.` with the `common.netaddr` LAN filter, bounded timeout, retry per cycle) + `controller/discovery.py` advertiser behind `[controller.discovery] advertise` (off in tests). mDNS is an unauthenticated trust point until V0.7 — stated in the module docstring. Loopback discovery component test ran for real on Laptop 1; skips with a reason where mDNS is unavailable.*
- [x] **0.3.10** RTT to controller measured on heartbeat; stored — *done 2026-09-20 (G2): controller-initiated `Ping`/`Pong` (proto 17/19), RTT on the controller's monotonic clock, `last_rtt_ms` + EWMA(0.3) in `node_session`; loopback ≈ 1.6 ms in component tests. `Heartbeat.agent_time` kept for skew display only.*
- [x] **0.3.11** Persist resource snapshots (live buffer + downsampled history) — *done 2026-09-21 (G4): live ring per node + 1-min mean/max buckets in `resource_snapshot` (migration `0003`, which also adds `SessionEndReason.INTERNAL_ERROR`), retention sweep, flusher with poison-row isolation guarded by a real `Registry.ping()`; dedupe cursors bounded independently of the rings (`max_cursor_nodes`). Implementer → tester FAIL (R1 critical: replay dedupe accepted duplicates; H1 major: NVML had no timeout) → fix → tester FAIL (D-EVICT-1: LRU eviction dropped the dedupe cursor) → cleanup (10 items) → tester FAIL (ISO-1: the poison-row fix dropped history during an outage) → fix → **reviewer APPROVE** (delta review, no blocking). Gates: ruff/format clean, mypy 78 files, lint-imports 7 kept / 0 broken, gen_protos clean, unit **2611 passed** / 2 skipped / 2 xfailed, component agent+store+controller **126 passed**. Lead-verified independently: all five replay/restart/reconnect/zombie/bucket cases, the NB-1 ordering guard (fails when an await is inserted), and the ISO-1 pin by mutation. Commits `5fa761b` … `84b9a1b`.*
- [x] **0.3.12** REST `GET /v1/nodes`, `/v1/nodes/{id}`; CLI `compute nodes`, `compute status` (Rich tables, `--json`) — *done 2026-09-24 (G5). Read-only `controller/api/` (`context/models/shape/routes/errors`) mounted by `create_http_app`: `GET /v1/nodes`, `/v1/nodes/{id}`, `/v1/nodes/{id}/resources`, `/v1/sessions`, one typed envelope `{"error":{"code","message"}}` from the existing `FailureType` vocabulary, OpenAPI at `/v1/openapi.json` with `/docs` + `/redoc` off (no CDN fetch on an isolated LAN). **D16 honoured**: `state` / `connected` / `sample_age_s` are three independent fields, the last from the ring's monotonic `received_at` against the same `loop.time` the session manager and leases use (traced end to end by the reviewer). **D14 proved, not asserted**: every verb × path enumerated, 4 GET routes only, every mutating verb a typed 405. `compute nodes [NODE_ID] [--json] [--watch]` and `compute status [--json]` over REST only (`httpx` relocated to core deps, D15; `cli` still imports only `common`/`sdk`), ASCII-box tables, `ExitCode.UNAVAILABLE = 6` (**D18**) when the controller is unreachable, no traceback on any failure path. Brief §5e, decisions D13–D18.*
  *Rounds: implementer → tester **PASS** (no defects) → reviewer **REQUEST_CHANGES**, 2 blocking — (B1) the error-handler set was not exhaustive while its docstring said it was, so an unexpected exception returned Starlette's text/plain 500; (B2) **ANSI/OSC escape injection** — enrolment is unauthenticated in V0.3 and `Hello.hostname` is length-validated only, so any LAN host could inject escape sequences into every operator's `compute nodes` — → fixed + 7 non-blocking → tester round 2 on the delta **PASS** → reviewer **APPROVE**. 9 mutation-checks across the two rounds (renderer gate, node-id echo, 503 driver text, D16 derivation, the `le=` bound, the tiebreaker, the catch-all, exit codes, Ctrl-C). Gates: ruff + format clean, mypy `src` 89 files, lint-imports 7 kept / 0 broken, gen_protos clean, `pytest -m unit` **3214 passed** / 2 skipped / 2 xfailed. Commits `8dd97d5` … `bedbee5`.* **Caveat a later version must respect: NB-G5-1 — `state` alone is not trustworthy until G6 (see the design change log); read `connected` / `sample_age_s`.**
- [~] **0.3.13** Run as background service in dev: systemd user unit (Linux); scheduled task / NSSM (Windows) — documented — *code/docs done 2026-09-24 (G5): `docs/OPERATIONS.md` (D14 trust boundary stated first, ports, logs, health via `curl /healthz` and `compute status`, install **and** uninstall for both platforms), systemd **user** units + Windows Task Scheduler XML under `deploy/`, and `scripts/print_service_setup.py` which **prints and installs nothing** — verified by a real subprocess run asserting no file mtime or directory listing changed (D17). Units are user-scope, no elevation, no secret in a unit file, `RestartPreventExitStatus` matching `ExitCode.CONFLICT`/`CONFIG_ERROR`; the task XML is `LeastPrivilege`/`InteractiveToken`. Reviewer APPROVE.* **`[~]` only for the 🧑 Windows logoff check** — `docs/OPERATIONS.md` §3 deliberately marks "does the task survive logoff?" **unverified** rather than guessing, and carries the 5-step lab procedure; run it and paste both `compute nodes` tables plus `schtasks /query /tn MeshForgeAgent /v /fo LIST`.
- [x] **0.3.14** Cluster test harness: start controller + N agents as local processes, fault injection (kill, pause, network cut via Toxiproxy) — *done 2026-09-24 (G6, brief §5f, decisions D19–D29). **Harness:** `tests/_support/mf_faultproxy.py` (FaultLink: in-process asyncio proxy with down/up, reset_peer, blackhole, latency, bandwidth, retarget — D23 replaces the Toxiproxy binary, ADR-0010 Updates), `mf_cluster.py` (controller + N agent subprocesses, process-group containment + PDEATHSIG, observation via REST + JSON logs only, per-scenario invariant check), `tests/cluster/` (5-agent scenarios: kill, pause, blackhole, reset_peer with the controller up — the G4 R1 gap —, graceful stop, agent restart, controller SIGKILL/SIGINT + restart, latency false-positive guard, R1 replay regression) + a Linux CI `cluster` job. **Inherited fixes:** NB-G5-1 (ARCHITECTURE §12.3 amended: clean close → OFFLINE, abrupt → SUSPECT + lingering lease; supersession writes nothing), fenced state writes (D20, a latent reconnect race), NB-1 Reconcile-first gate (D21), ingestion validation of Hello strings (D22), `controller.listening` (D24), LATE-1 bucket grace (D26), reconnect cap 5 s + `hello_timeout_s` 3 s (D27/D29, criterion 2), pending-Hello claim (D28). **0.3.15 lab kit** (`lab/fabric_check.py`, `lab/agent_rss.py`, `lab/checklists/V0.3-fabric.md`) ready. Rounds: implementer ×3 (A, B, C) → tester **FAIL** (2 major) → fix → tester round 2 **PASS** → reviewer REQUEST_CHANGES (lint only) → fixed → reviewer **APPROVE**. Evidence: `-m cluster` 39 passed exit 0 (3 repeated runs ~105 s); loopback at shipped timings: blackhole → OFFLINE 6.0 s (≤ 8), restore → ONLINE ≤ 3.6 s after a 120 s outage (≤ 10), controller SIGKILL → 5 agents back 1.8–2.4 s (≤ 15); unit 3475 passed exit 0; component controller 87 / agent 72 / store 17 exit 0; ruff, mypy src, lint-imports, gen_protos clean; mutation-checked every fix. Commits `64a89b5` … `64e488a`. Follow-ups in **0.4.16**.*
- [ ] **0.3.15** 🧑 Lab test on 4 laptops (script + checklist in `lab/`)

**Testing:** unit (lease/state logic) · component (real gRPC session) · cluster (5 agents; kill/pause/partition) · lab 🧑 · soak (24 h).
**Success criteria**
1. All 4 laptops appear with hardware matching `docs/LAB.md`.
2. Network cable pulled → node OFFLINE within **≤ 8 s**; restored → ONLINE within **≤ 10 s** with no agent restart.
3. Controller killed and restarted → all agents reconnect within **≤ 15 s**, no manual action.
4. Agent restart keeps the same `node_id`.
5. 24 h soak on Ethernet: zero false OFFLINE events; agent RSS growth < 10 %.

**Deliverable:** `compute nodes` shows a live, self-healing fabric of real laptops.
**Unlocks:** V0.4 (commands can be sent to nodes).

---

# M2 — MODE 1 PRODUCT

## V0.4 — Remote execution core (Mode 1 foundation)

**Objective:** submit a project from one laptop; it runs isolated on a chosen laptop; logs stream live; outputs return.

**Concepts introduced:** job & attempt state machines · manifests · sandboxing · log streaming · idempotent submission · cancellation · phase telemetry.
**Dependencies:** Docker SDK for Python, zstandard, WebSockets (FastAPI), NVIDIA Container Toolkit.
**Prerequisites:** V0.3 complete.

**Tasks**
- [x] **0.4.1** Manifest schema (`compute.yaml`, Pydantic) with validation, defaults, inferred entrypoint when absent. **Done 2026-09-28 (V0.4 G2):** evidence — tester round 1 PASS (`677939c`), reviewer REQUEST_CHANGES (non-atomic submit) → fix `c8fc346` `2f4476f` `f266274` → tester round 2 PASS (`d3581aa`: pg_terminate_backend + random cancellation mid-submit leave no row) → reviewer APPROVE. Gates: ruff/format/mypy/lint-imports/gen_protos exit 0; unit 4351 exit 0; component ctl+store 171 exit 0; cluster 41 exit 0. Commits `1462e34` … `f92e4fd` + fixes.
- [~] **0.4.2** Source bundling with ignore rules (`.gitignore`, `.computeignore`), deterministic tar + zstd, SHA-256. *G2 tester + reviewer APPROVE (see 0.4.1); stays `[~]` only until the first GitHub CI run shows the same golden digest on `windows-latest` (cross-OS determinism is part of the brief).*
- [x] **0.4.3** Upload to controller store (simple path; CAS arrives in V0.5). Same evidence as 0.4.1.
- [x] **0.4.4** Job, Attempt tables + transition module enforcing ARCHITECTURE §12 state machines. Same evidence as 0.4.1 (D55's three extra arrows land in G4).
- [x] **0.4.5** `POST /v1/jobs` with `Idempotency-Key`; manual placement `--node`; local placement `--local`. Same evidence as 0.4.1; submission is one transaction. Criterion 4's "one execution" half is proven in G4 (dispatch); the `compute run --local` command is G5.
- [x] **0.4.6** Dispatcher → `StartAttempt` command; agent executor: fetch, unpack, run. **Done 2026-09-28 (V0.4 G4, brief §2g/§5e, D64–D69):** implementer `6039802` … `5d22825` → tester round 1 PASS (`3662ccd`: real controller SIGKILL+restart race ×4 → one execution; cancel from every state + cancel-vs-DONE race ×20; workdir 0700 + cleanup; fencing_token bound, mutation-checked) → reviewer APPROVE, no blocking (6 non-blocking → D70, carried by G5). Gates: ruff/format/mypy (152)/lint-imports/gen_protos exit 0; unit 4506 exit 0; whole component 634 passed exit 0; cluster 48 passed exit 0. Loopback, Laptop 1 (not a benchmark): cancel 0.11 s, timeout overshoot 0.011 s, submit→running 0.19 s.
- [x] **0.4.7** Runtime interface + **Docker runtime** (limits: CPU, RAM, pids; workdir-only mount; non-root user) + subprocess fallback. **Done 2026-09-28 (V0.4 G3):** tester round 1 PASS (`5551c74`: parked sandbox/process/settings tests + docker-socket test; own mutations no-new-privileges and pids_limit caught; suspected run-phase network leak ruled out), reviewer APPROVE (network_mode set per container role, `docker_runtime.py:587`; setup and run never share a container). Gates: ruff/format/mypy/lint-imports/gen_protos exit 0; unit 4383 exit 0; component/agent incl. docker + --run-gpu 160 exit 0; whole component 604 exit 0 (14m35s); cluster 41 exit 0. Commits `d625a45` … `b14baa3`. Follow-ups → D63 (G4). Windows (Docker Desktop, job objects) is proven in 🧑 0.4.15.
- [~] **0.4.8** GPU pass-through (`resources.gpu`) via NVIDIA Container Toolkit / Docker Desktop. *G3 tester + reviewer APPROVE (see 0.4.7): exact-NVML-index `DeviceRequest` + `NVIDIA_VISIBLE_DEVICES=void` tested with --run-gpu on Laptop 1. Stays `[~]` until a real PyTorch CUDA workload runs in the pinned pytorch image (🧑 Laptop 1 needs ~12 GB free disk) — G5's GPU example / 0.4.15.*
- [x] **0.4.9** Log streaming agent → controller → client (WebSocket), persisted log file — ✅ G5 closed 2026-10-01: tester r2 PASS `44dcf44`, reviewer pass 3 APPROVE; unit 4623 / component 619 (+52 env-only skips, `-rs` checked) / cluster 52, exit 0
- [x] **0.4.10** Exit code + declared outputs collected, uploaded, downloadable — ✅ G5 closed 2026-10-01: tester r2 PASS `44dcf44`, reviewer pass 3 APPROVE; unit 4623 / component 619 (+52 env-only skips, `-rs` checked) / cluster 52, exit 0
- [x] **0.4.11** Cancel (graceful then forced) and timeout enforcement. **Done 2026-09-28 (V0.4 G4, brief §2g/§5e, D64–D69):** implementer `6039802` … `5d22825` → tester round 1 PASS (`3662ccd`: real controller SIGKILL+restart race ×4 → one execution; cancel from every state + cancel-vs-DONE race ×20; workdir 0700 + cleanup; fencing_token bound, mutation-checked) → reviewer APPROVE, no blocking (6 non-blocking → D70, carried by G5). Gates: ruff/format/mypy (152)/lint-imports/gen_protos exit 0; unit 4506 exit 0; whole component 634 passed exit 0; cluster 48 passed exit 0. Loopback, Laptop 1 (not a benchmark): cancel 0.11 s, timeout overshoot 0.011 s, submit→running 0.19 s.
- [x] **0.4.12** Phase timings (queue, stage, env, run, collect) + start snapshots recorded per attempt **Done 2026-09-28 (V0.4 G4, brief §2g/§5e, D64–D69):** implementer `6039802` … `5d22825` → tester round 1 PASS (`3662ccd`: real controller SIGKILL+restart race ×4 → one execution; cancel from every state + cancel-vs-DONE race ×20; workdir 0700 + cleanup; fencing_token bound, mutation-checked) → reviewer APPROVE, no blocking (6 non-blocking → D70, carried by G5). Gates: ruff/format/mypy (152)/lint-imports/gen_protos exit 0; unit 4506 exit 0; whole component 634 passed exit 0; cluster 48 passed exit 0. Loopback, Laptop 1 (not a benchmark): cancel 0.11 s, timeout overshoot 0.011 s, submit→running 0.19 s.
- [x] **0.4.13** CLI: `compute run`, `jobs`, `logs -f`, `cancel`, `get` — ✅ G5 closed 2026-10-01: tester r2 PASS `44dcf44`, reviewer pass 3 APPROVE; unit 4623 / component 619 (+52 env-only skips, `-rs` checked) / cluster 52, exit 0
- [x] **0.4.14** Example workloads in `workloads/examples/` (CPU script, PyTorch GPU training script) — ✅ G5 closed 2026-10-01: tester r2 PASS `44dcf44`, reviewer pass 3 APPROVE; unit 4623 / component 619 (+52 env-only skips, `-rs` checked) / cluster 52, exit 0
- [ ] **0.4.15** 🧑 Lab: run examples from Laptop 1 on each other laptop
- [x] **0.4.16** **Explicit agent `Goodbye{reason}`** before the half-close; the controller's rule becomes "clean close = Goodbye seen *and* the request iterator ended", anything else is `STREAM_LOST`. Replaces the timing inference `HALF_CLOSE_CONFIRM_S` (kept only as a fallback for old agents) and lets the `grpcio<1.85` cap be lifted. **Must land before V0.6's retry-on-OFFLINE** — an abrupt end misread as clean makes OFFLINE early and would duplicate work. Also from the G6 review: a deadline on D28's admission claim (the lease timer skips a claimed node while its Hello waits on the admission lock), extract `session/claims.py` (`EndArbiter`) from `manager.py` (~1220 lines), a `registry.write_queue_overflow` signal, and a direct test for the shutdown deferred-end backstop (`manager.py` `# pragma: no cover`). *Raised 2026-09-24 by the G6 reviewer.* **Done 2026-09-28 (V0.4 G1):** Goodbye payload 21, clean close on receipt, any end without Goodbye = `stream_lost`, `HALF_CLOSE_CONFIRM_S` deleted (D40, departs from this row's "keep as fallback"); admission deadline + `REJECT_CODE_UNAVAILABLE` (D41); `claims.py`/`ends.py`/`reports.py`/`dispatch.py` split, `manager.py` 1315 → 824 (D42); overflow episode signal + counters + backstop test (D43); `grpcio<1.85` cap lifted (D43b). Evidence: tester round 1 PASS (`6f545ca`; unit ctl+agent 1641, component ctl+agent 290, cluster 41, exit 0; SIGTERM → OFFLINE 0.10 s loopback), reviewer APPROVE, no blocking. Commits `bfbe960`, `e189f86` (Goodbye, mislabelled G3 — staging race), `483b7ab`, `ff9a785`.
- [~] **0.4.17** **G7 review follow-ups** (hardware scores; none blocked 0.3.4): (1) Windows `taskkill` in `agent/benchmarks/runner.py::kill_tree` still runs on the event loop (capped at 1 s) — move it off the loop without breaking cancel-safety, verified on Windows; (2) extract per-report handlers (`_on_resource_update`, `_on_hardware_scores`, `_hello_scores`, `_reject_scores`) from `controller/session/manager.py` (**1315 lines**) into `session/reports.py` **before V0.4 adds AttemptStatus/LogChunk handlers**; (3) split `ChildProcess`/`kill_tree` out of `runner.py` (780 lines); (4) publish score reason codes as shared constants (freeze into an enum before V0.15 trains on them); (5) surface `hardware_scores_rejected` next to the other session counters; (6) close the wedged-child-after-agent-SIGKILL gap (PDEATHSIG / Windows job object) and the cancel window during `create_subprocess_exec`; (7) the controller trusts the agent's score fingerprint — revisit with V0.7 identity/authorization; (8) track the flaky `tests/component/lab/test_s1_network.py::test_garbage_header_does_not_break_the_server` (`ENOTCONN` on `shutdown()` under load). *Raised 2026-09-25 by the G7 reviewer and tester.* *Progress 2026-09-28: items 2 and 5 done in G1; items 3, 4, 6, 8 done in G3 (tester PASS, reviewer APPROVE); item 1 written, awaits a 🧑 Windows run; item 7 is V0.7; D59 (subprocess grandchildren) open for V0.6.*

**Testing:** component (executor + Docker) · cluster · lab 🧑.
**Success criteria**
1. Example CPU and GPU projects run on every node type; outputs **byte-identical** to a local run on the same hardware class (CPU example everywhere; PyTorch on CPU; GPU on the same GPU model + driver), and **within the stated tolerance** of `workloads/examples/pytorch-train/compare.py` across different CPU/GPU models (GPU kernels are not bit-reproducible across hardware — D76, 2026-09-29).
2. Log lines appear on the client **≤ 1 s** after being printed remotely.
3. Cancel stops the workload **≤ 5 s**; timeout enforced within 2 s of limit.
4. Repeated `POST` with the same `Idempotency-Key` → one job, one execution.
5. Sandbox test: workload cannot read/write outside its workdir or see host processes.
6. Every attempt has complete phase timings in the DB.

**Deliverable:** working whole-job offload with manual placement.
**Unlocks:** V0.5 (make it fast), V0.6 (make it automatic and robust).

---

## V0.5 — Data plane & environments

**Objective:** move code, data, and models efficiently and safely; reuse environments; never transfer or build twice.

**Concepts introduced:** content-addressed storage · Merkle tree manifests · have/need negotiation · HTTP range & resumable transfer · peer-to-peer fetch · cache location index · environment hashing · LRU GC.
**Dependencies:** httpx/aiohttp, uv (venv runtime), Docker build cache.
**Prerequisites:** V0.4; S3 transport decision.

**Tasks**
- [x] **0.5.1** CAS store (controller + agents): write-then-rename, verify on read, chunking for large files — ✅ G1 closed 2026-10-01: tester r3 + reviewer pass 2 APPROVE (`470b203`); 0 bytes on resubmit, resume, corruption never used (real proxies), 5 GiB sparse e2e 126.6 s on Laptop 1 (not a benchmark claim); unit 5157 / component ctl+store 273 exit 0
- [x] **0.5.2** Tree manifests; client uploads only missing blobs (have/need) — ✅ G1 closed 2026-10-01: tester r3 + reviewer pass 2 APPROVE (`470b203`); 0 bytes on resubmit, resume, corruption never used (real proxies), 5 GiB sparse e2e 126.6 s on Laptop 1 (not a benchmark claim); unit 5157 / component ctl+store 273 exit 0
- [x] **0.5.3** Peer blob HTTP server in agent (range GET, short-lived tokens); fetch from nearest holder — ✅ G4 closed 2026-10-05: reviewer APPROVE (no blocking) after 4 tester / 3 fix rounds (`7945c60` … `b51af39`); peer server + Ed25519 tokens, work-stealing multi-source fetch, cache reports + `cache_location` index; criteria 3, 4, 6 proven at component level with real peers (byte-exact source split, corrupt/killed/expired peers dropped, resume without re-sending chunks); loopback 1 GiB from 2 peers 4.88 s (not a benchmark claim); unit 5433 / whole component 939 / cluster 55 exit 0 (only lab-spike + since-fixed load flakes)
- [x] **0.5.4** Parallel chunked download, resume after interruption, integrity check — ✅ G4 closed 2026-10-05: reviewer APPROVE (no blocking) after 4 tester / 3 fix rounds (`7945c60` … `b51af39`); peer server + Ed25519 tokens, work-stealing multi-source fetch, cache reports + `cache_location` index; criteria 3, 4, 6 proven at component level with real peers (byte-exact source split, corrupt/killed/expired peers dropped, resume without re-sending chunks); loopback 1 GiB from 2 peers 4.88 s (not a benchmark claim); unit 5433 / whole component 939 / cluster 55 exit 0 (only lab-spike + since-fixed load flakes)
- [x] **0.5.5** Cache reports → controller location index (blobs, envs, images per node) — ✅ G4 closed 2026-10-05: reviewer APPROVE (no blocking) after 4 tester / 3 fix rounds (`7945c60` … `b51af39`); peer server + Ed25519 tokens, work-stealing multi-source fetch, cache reports + `cache_location` index; criteria 3, 4, 6 proven at component level with real peers (byte-exact source split, corrupt/killed/expired peers dropped, resume without re-sending chunks); loopback 1 GiB from 2 peers 4.88 s (not a benchmark claim); unit 5433 / whole component 939 / cluster 55 exit 0 (only lab-spike + since-fixed load flakes)
- [x] **0.5.6** Named, versioned inputs (datasets/models registered once, referenced by name@hash) *(G5 CLOSED 2026-10-05: implementer a7ec04e…948720e, tester r1 47e6724 FAIL → fix 1041f7f…3e22ad1, tester r2 ab20929 PASS → fix 4f6531f…472319a, reviewer APPROVE no blocking; unit 5547 / component 1046 / cluster 56 exit 0, gates exit 0.)*
- [x] **0.5.7** Environment builder: env hash; Docker image per env hash; **venv runtime (uv)** with same interface — ✅ G2 closed 2026-10-01: env hash (D85, v3 incl. package source), build-once cached envs (venv/uv, Docker image per hash, cached --target), damaged-env detection, local-path refusal; criterion 5 warm env 0.42 s incl. interpreter start on Laptop 1 (not a benchmark claim). Tester ×3 rounds, reviewer APPROVE; commits `81e0ead` … `7819323`; unit 5232 exit 0, cluster 52 passed.
- [x] **0.5.8** Base images `python-cpu`, `cuda-pytorch`; `compute node prepare` pre-pulls *(Local part done in G2 2026-10-01: pinned base images by digest, `meshforge-agent prepare`; controller-driven `compute node prepare` → G5.)* *(G5 CLOSED 2026-10-05: implementer a7ec04e…948720e, tester r1 47e6724 FAIL → fix 1041f7f…3e22ad1, tester r2 ab20929 PASS → fix 4f6531f…472319a, reviewer APPROVE no blocking; unit 5547 / component 1046 / cluster 56 exit 0, gates exit 0.)*
- [x] **0.5.9** GC: LRU with per-node quota; pinned items *(G5 CLOSED 2026-10-05: implementer a7ec04e…948720e, tester r1 47e6724 FAIL → fix 1041f7f…3e22ad1, tester r2 ab20929 PASS → fix 4f6531f…472319a, reviewer APPROVE no blocking; unit 5547 / component 1046 / cluster 56 exit 0, gates exit 0.)*
- [x] **0.5.10** Transfer telemetry (bytes, source node, throughput, cache hit/miss) *(G5 CLOSED 2026-10-05: implementer a7ec04e…948720e, tester r1 47e6724 FAIL → fix 1041f7f…3e22ad1, tester r2 ab20929 PASS → fix 4f6531f…472319a, reviewer APPROVE no blocking; unit 5547 / component 1046 / cluster 56 exit 0, gates exit 0.)*
- [ ] **0.5.11** 🧑 Lab: multi-GB dataset tests on Wi-Fi and Ethernet

**Testing:** kill transfer mid-way and resume; inject corrupted chunk; cache-hit tests; GC under quota pressure; lab 🧑.
**Success criteria**
1. Resubmitting an unchanged project transfers **0 bytes** of source/data; changing one file transfers only that file.
2. 5 GB transfer on Ethernet reaches **≥ 80 %** of iperf3-measured bandwidth *(calibrate)*.
3. Interrupted transfer resumes without re-sending completed chunks.
4. A corrupted blob is detected and never used.
5. Warm env cache: job start overhead **≤ 3 s** *(calibrate)*; cold build time recorded.
6. Data fetched from a peer when the peer is closer/faster than the controller.

**Deliverable:** fast, cached, integrity-checked Mode 1.
**Unlocks:** V0.6 (cache-aware planning), V0.11 (model blobs for GPU sessions).

---

## V0.6 — Scheduling & reliability — Mode 1 complete

**Objective:** automatic placement across many laptops and robust behaviour under failure; Mode 1 reaches finished functional quality.

**Concepts introduced:** queues & slots · candidate/feasibility/cost/policy pipeline · baseline policies · network matrix probing · fencing tokens · at-least-once + dedupe · crash recovery & reconciliation · failure taxonomy.
**Dependencies:** none new.
**Prerequisites:** V0.5; **0.4.16 (explicit agent Goodbye)** — retry-on-OFFLINE must not rest on the timing-based clean-close inference (G6 review).

**Tasks**
- [x] **0.6.1** Per-node slots (CPU/GPU concurrency) + global pending queue with priorities — **[x] 2026-10-05 V0.6 G2** (impl 13fcdce..88d5182 → tester FAIL D-1..3 527d633 → fixes f4a37ce/600a70a/d573197 → tester tests pass → reviewer APPROVE; unit 5878 ✓, component 1170 ✓ (1 known load-flaky), cluster 57 ✓, ruff/mypy/lint-imports/protos clean)
- [x] **0.6.2** Planner v1 pipeline: candidate generator (Local + Mode 1 per node) → feasibility filter (OS, RAM, VRAM, GPU, disk, runtime, owner settings) — **[x] 2026-10-05 V0.6 G2** (impl 13fcdce..88d5182 → tester FAIL D-1..3 527d633 → fixes f4a37ce/600a70a/d573197 → tester tests pass → reviewer APPROVE; unit 5878 ✓, component 1170 ✓ (1 known load-flaky), cluster 57 ✓, ruff/mypy/lint-imports/protos clean)
- [x] **0.6.3** Policies: `random`, `round_robin`, `least_loaded`, `strongest_suitable`, `resource_score`, `cache_affinity`, **`cost_model` (default, D107)**; selectable globally and per job — **[x] 2026-10-05 V0.6 G2** (impl 13fcdce..88d5182 → tester FAIL D-1..3 527d633 → fixes f4a37ce/600a70a/d573197 → tester tests pass → reviewer APPROVE; unit 5878 ✓, component 1170 ✓ (1 known load-flaky), cluster 57 ✓, ruff/mypy/lint-imports/protos clean)
- [x] **0.6.4** Analytical estimator v0: missing-bytes/bandwidth, env cache state, history-based compute time — ✅ 2026-10-07 G4 CLOSED (tester r2 PASS, fix r2 `222774a` `db8fc96` `c9d35f0`, reviewer APPROVE; exit list at `7cb2479` all exit 0)
- [x] **0.6.5** Decision records (candidates, rejections + reasons, estimates) + `compute explain <job>` — **[x] 2026-10-05 V0.6 G2** (impl 13fcdce..88d5182 → tester FAIL D-1..3 527d633 → fixes f4a37ce/600a70a/d573197 → tester tests pass → reviewer APPROVE; unit 5878 ✓, component 1170 ✓ (1 known load-flaky), cluster 57 ✓, ruff/mypy/lint-imports/protos clean)
- [x] **0.6.6** Peer network probes (scheduled, rate-limited) → network matrix table — ✅ 2026-10-07 G4 CLOSED (tester r2 PASS, fix r2 `222774a` `db8fc96` `c9d35f0`, reviewer APPROVE; exit list at `7cb2479` all exit 0)
- [x] **0.6.7** Node loss → attempt LOST → retry per failure-type policy; **fencing tokens** reject stale reports — ✅ 2026-10-06 G3 (`60020d8`..`db58508`; tester PASS, reviewer APPROVE)
- [x] **0.6.8** Controller crash recovery: rebuild from DB + agent Reconcile; running attempts adopted, not duplicated — ✅ 2026-10-06 G3 (`60020d8`..`db58508`; tester PASS, reviewer APPROVE)
- [x] **0.6.9** Command delivery at-least-once with agent-side dedupe — **[x] 2026-10-05 V0.6 G1** (impl → tester PASS f573a0b → fix 8eb3f71 → reviewer APPROVE; unit agent+controller 2655 ✓, component 1071 ✓, cluster 57 ✓, ruff/mypy/lint-imports clean)
- [x] **0.6.10** Failure taxonomy (`user_error`, `oom`, `vram_oom`, `env_error`, `disk_full`, `lost`, `timeout`, `infra_error`) everywhere — **[x] 2026-10-05 V0.6 G1** (impl → tester PASS f573a0b → fix 8eb3f71 → reviewer APPROVE; unit agent+controller 2655 ✓, component 1071 ✓, cluster 57 ✓, ruff/mypy/lint-imports clean)
- [x] **0.6.11** Local execution path via the submitting laptop's own agent — **[x] 2026-10-05 V0.6 G2** (impl 13fcdce..88d5182 → tester FAIL D-1..3 527d633 → fixes f4a37ce/600a70a/d573197 → tester tests pass → reviewer APPROVE; unit 5878 ✓, component 1170 ✓ (1 known load-flaky), cluster 57 ✓, ruff/mypy/lint-imports/protos clean)
- [x] **0.6.12** Chaos suite in cluster harness (random kills, partitions, controller restarts) — ✅ 2026-10-06 G3 (`60020d8`..`db58508`; tester PASS, reviewer APPROVE)
- [ ] **0.6.13** 🧑 Lab: 100-job mixed run on 4 laptops with scripted disruptions
- [x] **0.6.14** Refresh upload grants on Reconcile for RUNNING/UPLOADING attempts (mirror V0.5 D95; today a controller restart during an upload ends the attempt `output_error`) — from the V0.5 stabilisation review 2026-10-04 — **[x] 2026-10-05 V0.6 G1** (impl → tester PASS f573a0b → fix 8eb3f71 → reviewer APPROVE; unit agent+controller 2655 ✓, component 1071 ✓, cluster 57 ✓, ruff/mypy/lint-imports clean)

**Testing:** unit (policies, feasibility, estimator) · cluster chaos · lab 🧑.
**Success criteria**
1. 100 mixed jobs across 4 laptops with random agent kills: **0 lost, 0 duplicated** executions; all reach correct terminal states.
2. Controller `kill -9` with 20 running jobs → after restart every job reaches the correct state; no duplicate runs.
3. `compute explain` shows every candidate, rejections with reasons, and predicted breakdown.
4. For a tiny job whose transfer dominates, planner selects **Local** (test case).
5. All baseline policies selectable and covered by tests.

**Deliverable:** Mode 1 functionally complete (security in V0.7, UI in V0.8).
**Unlocks:** parallel tracks V0.7 · V0.8 · V0.9 · V0.10 · V0.11.

---

## V0.7 — Security & pairing

**Objective:** only enrolled devices participate; all traffic authenticated and encrypted; workloads constrained.

**Concepts introduced:** PKI / fabric CA · CSR enrollment · one-time join tokens · mTLS · revocation · least privilege · secret handling · threat modelling.
**Dependencies:** `cryptography`, gRPC TLS credentials, TLS for HTTP servers.
**Prerequisites:** V0.6 (identity keypairs from V0.3 already exist).

**Tasks**
- [x] **0.7.1** `docs/THREAT_MODEL.md` — G1 2026-10-08: `1360654` + fix `2e5dbd6`; tester PASS `65d4af5`; reviewer APPROVE
- [x] **0.7.2** `compute fabric create` → CA keypair, fabric record — G1 2026-10-08: `meshforge-controller fabric create|show`, `common/pki.py`, server cert (`a92bb6e` `65627d9`, fix `2643847`); unit 7816 / G1 component 22 / cluster 67 exit 0; reviewer APPROVE
- [x] **0.7.3** `compute fabric invite` → short-lived single-use join code (address + token + CA fingerprint); QR in dashboard later — V0.7 G2 `d79287c` `80bcbd3`; tester r1 FAIL→fix→r2 PASS, reviewer APPROVE 2026-10-08
- [x] **0.7.4** `compute fabric join <code>` → CSR → signed node cert; secure storage — V0.7 G2 `3321171` `5a8282c`; MITM/pin, race, rollback, crash tests; reviewer APPROVE 2026-10-08
- [x] **0.7.5** mTLS on agent gRPC; TLS + node-cert auth on peer blob servers; scoped short-lived tokens — V0.7 G3 2026-10-11 (`64c2f1e`..`5e68599`, fix r1 `e094f10`..`b9ea681`): tester r1 FAIL (T-1..T-3, D155) → fix → tester r2 PASS (`42679c7`; unit 8303, component 1463 exit 0, cluster 72+known wss flake rerun 4/4, chaos 3) → reviewer APPROVE; 🧑 lab re-join + checklist V0.7-G3 in 0.7.12
- [x] **0.7.6** REST user authentication + CLI credential storage — G4 closed 2026-10-12: users, scrypt passwords, hashed API tokens, `compute login`, D148 auth on GETs, cross-user 404, live-stream re-auth; tester r2 PASS `1d45758`, reviewer APPROVE; follow-ups D158/D159
- [x] **0.7.7** Revocation + certificate rotation — revocation (0.7.7a) done in G3 2026-10-11 (live cut, superseded serials, expiry cut, migrations 0016/0017); rotation done in G4 2026-10-12 (pending cert + activate, migration 0018); tester r2 PASS, reviewer APPROVE
- [x] **0.7.8** Owner policy per node: accept jobs / accept GPU sessions / CPU-RAM-GPU caps — G5 `2b42a69`..`7aad987`; tester r1 FAIL → D160 fix → tester r2 PASS → T-10 fix → reviewer APPROVE (2026-10-12). Windows job-object caps proven in 🧑 0.7.12
- [x] **0.7.9** Sandbox hardening: no privileged containers, dropped capabilities, read-only base, optional no-network jobs — G5 `a291934`..`7aad987`; create-time allow-list vs plan, 36 real-Docker tampers refused, 36 escape probes blocked, output cap + run-time disk floor; reviewer APPROVE (2026-10-12). Docker Desktop proof in 🧑 0.7.12
- [x] **0.7.10** Job secrets (injected as files/env; redacted in logs) — G6 code `19c4fb1`..`f21646b`; tester r1 FAIL 2026-10-13 (3 major, 7 minor) → D162 → fix round 1 `bee2625`..`630e0c1` committed, NOT verified (exit list blocked: laptop on battery; unit red on one open pre-G6 test); tester r2 FAIL (T-17 blocker, T-14 major) → D163 → fix round 2 `8c54823`..`5897278` committed, NOT verified (exit list blocked: laptop on battery); tester r3 FAIL (T-21 major) → D164 → fix round 3 `8f113b4`..`1266086` + test fixes `8b9871d`, `30f2cc0`; exit list green (unit 9789, cluster 81, chaos 5x, F6 20x, seeds 2/3/7, burners; whole component run 1 had 1 test-only race, fixed and rerun 20x green; final whole component at `147942b`: 1901 passed / 14 skipped / 0 failed, exit 0 (67 min, 2026-10-14)); tester r4 PASS `bde4410`; reviewer APPROVE 2026-10-14 (4 non-blocking → D165) — **CLOSED 2026-10-14**
- [x] **0.7.11** Security test suite + review against threat model — V0.7 G7 2026-10-15 (`d1c4c61`..`e4438f5`): `security` marker + `scripts/threat_model_check.py` (99 rows: 84 mitigated / 15 accepted, exit 0); tester r1 PASS → fix r1 (D167) → reviewer APPROVE (row-by-row, none disputed); security 1475 passed, unit 10023, cluster 87, whole component 1916 exit 0; CG-E2 defect fixed `0e670a5`; 🧑 real capture in 0.7.12
- [ ] **0.7.12** 🧑 Lab run: 4 laptops pair by code, live revoke, code reuse/expiry, Wireshark capture, Windows agent join (`lab/checklists/V0.7-security.md`) — added 2026-10-07 (V0.7 brief)

**Testing:** negative tests (unenrolled/revoked/expired), packet capture check, sandbox escape attempts, log redaction tests.
**Success criteria**
1. Unenrolled agent cannot connect; revoked node rejected within one heartbeat.
2. Join code works once and expires as configured.
3. Packet capture shows no plaintext job data or credentials on any plane.
4. Secrets never appear in logs or telemetry.
5. Security suite green; threat-model review items closed or accepted with rationale.

**Deliverable:** secure fabric with real pairing.
**Unlocks:** V0.11 GPU services inherit mTLS/tokens; V0.17 installers.

---

## V0.8 — Dashboard & CLI v1

**Objective:** a professional, infrastructure-grade UI and polished CLI for operating the fabric.

**Concepts introduced:** SPA served by controller · live updates over WebSocket · design system · e2e testing.
**Dependencies:** React, TypeScript, Vite, TanStack Query, Tailwind + Radix, Recharts, Playwright.
**Prerequisites:** V0.6 (APIs stable); V0.7 for auth screens (can integrate when ready).

**Brief:** `docs/briefs/V0.8-dashboard-cli.md` (approved 2026-10-15, D169–D180, ADR-0017). **G1 item 0 = D168** (V0.7 non-blocking N1, N2, N3, T-12).

**Tasks**
- [x] **0.8.1** Design system: dark default, neutral grays, one accent, typography scale, status colours, spacing tokens (per brief §24) — G2 CLOSED 2026-10-16 (D192)
- [x] **0.8.2** App shell, auth, routing, API client (generated from OpenAPI) — backend part (browser session, SPA serving, OpenAPI models) in G1, frontend part in G2 (D169–D171) — G1+G2 CLOSED 2026-10-16 (D186, D192)
- [x] **0.8.3** Overview page (fabric health, nodes online, active jobs, utilization) — G3 CLOSED 2026-10-16 (D195, D200)
- [x] **0.8.4** Nodes list + node detail (capabilities, live metrics, network links, caches, owner settings) — G3 CLOSED 2026-10-16 (D195, D200); cache details "Not reported" until agents report them
- [x] **0.8.5** Jobs list + job detail (plan explanation table, timeline, terminal-style logs, artifacts, attempts) + browser submit (D177) — G4 CLOSED 2026-10-16 (D198, D200)
- [x] **0.8.6** Settings (fabric, invite/join code + QR, revoke, policies) — G5 CLOSED 2026-10-17 (D203, D204, D206); Users page = CLI command list until a `/v1/users` API exists
- [x] **0.8.7** Live updates via WebSocket (≤ 2 s latency) — event bus + `/v1/events/ws` in G1 (D172), UI wiring in G3; measured node kill → page 18–261 ms, heartbeat p95 ≤ 237 ms (G3 hand-off) — CLOSED 2026-10-16 (D200); load-scale latency is G7
- [x] **0.8.8** CLI polish: consistent output, `--json`, shell completion, helpful errors — G6 CLOSED 2026-10-16 (D192)
- [x] **0.8.9** Playwright e2e suite against a cluster-harness fabric — G7 merged `334da94` (D207–D210): e2e 105+ passed ×2, parity, leak gate, perf criterion 2 (p50/p95/max ≤ 2 s; heartbeat p95 252 ms, job.state p95 ≤ 101 ms, one laptop); whole `-m component` on main 2269 passed / 0 failed (2026-10-17)
- [ ] **0.8.10** 🧑 Visual review against brief §24 checklist

**Success criteria**
1. Every Mode 1 operation available from the UI and the CLI.
2. Live metrics update within 2 s; UI remains responsive with 4 nodes × 100 jobs history.
3. e2e suite green; no console errors; keyboard navigation for core flows.
4. 🧑 Team visual review passes (no "AI-dashboard" styling, per brief).

**Deliverable:** dashboard v1 + polished CLI. Later versions add their own pages.
**Unlocks:** V0.10–V0.15 UI pages; V0.17 first-run wizard.

---

## V0.9 — Benchmark & telemetry system v1

**Objective:** automatically generate large, labelled, reproducible performance datasets from real executions.

**Concepts introduced:** experiment design · parameter-space sampling · controlled conditions · forced-strategy runs · measured labels · data quality checks.
**Dependencies:** pandas/pyarrow, numpy; Linux `tc netem` for shaping.
**Prerequisites:** V0.6.

**Brief:** `docs/briefs/V0.9-benchmark-telemetry.md` (approved 2026-10-17, D211–D224, ADR-0018). **G0 item 0 = D210** (clock skew first, before 🧑 0.8.10 perf step). G1 ∥ G2 allowed (Q4).

**Tasks**
- [x] **0.9.1** Finalize telemetry schema (attempt summary, phase timings, start snapshots, network matrix, cache state) + Parquet export — *evidence: G1 merged `10f1f8c` (D227–D231); tester r1 (T-1..T-6) + r2 (T2-1) closed, reviewer APPROVE; unit 13799 exit 0; whole `-m component` alone 2400 passed / 16 failed → 14 stale V0.6 test fakes fixed (`Live.clock`, 6th phase), re-run green; 2 known load flakes green standalone*
- [x] **0.9.2** Workload library — G2 merged `8010dcb` (D225), tester r1/r2 + 2 fix rounds, reviewer APPROVE; whole `-m component` on main (G0+G2) 2367 passed / 22 skipped / 0 failed, exit 0 (84 min, 2026-10-17); GPU/ML families pending first CUDA run · (parametrized): CPU (matmul, primes, sort, compression, hashing, numerical), memory (arrays, dataframes, copies), GPU (matmul, conv, tensor ops, image processing), ML (CNN inference/training, transformer inference; batch/model/sequence sweeps)
- [x] **0.9.3** Campaign spec (YAML) + samplers (random, Latin hypercube) over params × conditions × strategies × nodes — G3 lane B, D232–D240; whole component 2494 passed / 1 load flake (green 3/3 standalone), D242
- [x] **0.9.4** Forced-strategy execution via planner override, tagged `benchmark`, randomized order, repetitions — G3 lane B, D232–D240; gate D242
- [x] **0.9.5** — G4 lanes A (item 0, schema v2) + B merged `a380385`/`701899c`, D241–D245; lane B tester r1/r2 PASS, reviewer r2 APPROVE (r1 race fixed); whole component 2564 passed / 1 load flake (green 9/9 standalone); 🧑 lab C1–C3 (real tc, Windows cpu_load, gpu_load) pending · Condition controllers: CPU/GPU load generators, cache warm/cold, queue pressure, network shaping (Linux), Wi-Fi vs Ethernet sessions, node-unavailable scenarios
- [x] **0.9.6** (G5 lane A, D246–D248; whole component 2671 passed / 2 failed — neither in labels, D255) Automatic labels per group: full measured cost vectors + best measured plan
- [x] **0.9.7** (G5 lane B D246–D253 + G5c item 0.0 race fix D257, merged) Unattended, resumable, throttled campaign runner; "don't disturb owner" guard — evidence: whole `-m component` alone on main 22742c1, 2026-10-19: 2722 passed / 22 skipped / 1 failed (1:40); the failure (V0.5 `test_snapshot_of_many_pages_replaces_exactly`, fake node lease missed under load) green 3/3 standalone + file 10/10 (D258)
- [x] **0.9.8** Data quality report: variance (CV), outliers, missing fields (G5c, D254–D257; same gate D258)
- [x] **0.9.9** Benchmarks page in dashboard (G6, D256–D257; vitest 618, e2e 14; same gate D258)
- [ ] **0.9.10** 🧑 Overnight campaign on the lab

*Carried from V0.8 G1 (D181/D184):* agent-side live-log redaction still runs on the agent's event loop (THREAT_MODEL RE-D2 residual) — move it off the loop with the telemetry/agent work here, without reopening the T-14 drain/cancel race.

**Success criteria**
1. Unattended campaign of **≥ 1,000 runs** across 4 laptops with **< 2 %** infrastructure failures.
2. Export reproducible (same campaign ID → same dataset rows).
3. Each configuration has ≥ 3 repetitions and reported CV.
4. Every later mode version adds its benchmark families here (tracked in cross-cutting tracks).

**Deliverable:** benchmark system + first real dataset.
**Unlocks:** V0.14 calibration, V0.15 ML.

---

# M3 — PARALLEL & GPU MODES

## V0.10 — Mode 3 Task Splitting

**Objective:** divide one task into sub-tasks across many laptops, run them, and combine results — correct, balanced, and fault-tolerant.

**Concepts introduced:** work queues with leases · pull-based load balancing · straggler mitigation (speculative execution) · makespan · deterministic combine.
**Dependencies:** none new.
**Prerequisites:** V0.6 (V0.9 recommended for benchmarks).

**Tasks**
- [x] **0.10.1** `split:` manifest spec (over glob/list/range/dataset shards; chunk size; combine = concat / merge-csv / merge-json / script) — evidence: D277/D278/D279 (V0.10 brief); tester PASS + reviewer APPROVE; gate on main 2026-10-20: unit 16226 passed, component 2832 passed / 3 failed → 1 stale V0.5-era test (expected split refused) fixed, 2 load flakes green 3/3 standalone
- [x] **0.10.2** **Shared work-queue module** (chunks, leases, visibility timeout, idempotent completion) — reused by Mode 4 — evidence: D277/D278/D279 (V0.10 brief); tester PASS + reviewer APPROVE; gate on main 2026-10-20: unit 16226 passed, component 2832 passed / 3 failed → 1 stale V0.5-era test (expected split refused) fixed, 2 load flakes green 3/3 standalone
- [x] **0.10.3** Planner: Mode 3 candidates (node sets), rate estimates, chunk size, "don't split" comparison — evidence: D280 (tester PASS, reviewer APPROVE); gate D282
- [x] **0.10.4** Per-node worker: one env prep, many chunks; chunk-level input staging (only needed files) — evidence: D281 (3 fix rounds; tester r3 + reviewer r2); gate D282
- [x] **0.10.5** Stragglers: speculative duplicates of tail chunks; first result wins, idempotent — G6 merged a006c894 (2026-10-21); tester r1 PASS (unit 16756; V0.10 comp+cluster 133 exit 0; workqueue comp 74), reviewer r1 APPROVE, F1 flake fixed (D285)
- [x] **0.10.6** Lost node → chunk leases expire → re-queued — evidence: D281 (kill -9 / controller restart / lease expiry / re-plan tests); gate D282
- [x] **0.10.7** Ordered, deterministic combine step — G5 merged 2026-10-20; tester r2 PASS (d892881b: unit 16700 passed, V0.10 comp+cluster one-process 121 passed ×3, ruff clean, mypy src 422 clean), reviewer r2 APPROVE
- [x] **0.10.8** Progress per chunk/node (CLI progress bar, dashboard page) — G7 merged b47bc074 (2026-10-21); tester r1 PASS, reviewer r1 APPROVE, Lead fixes N2/N3/T-1..T-3 (D286); post-merge main (G6+G7): unit 16860, V0.10 comp+cluster+workqueue one process 228 passed, ruff + mypy src clean
- [x] **0.10.9** Mode 3 benchmark families — G8 merged `33f8e97e` (D287): tester PASS ∥ reviewer APPROVE; post-merge main unit 16991, V0.10 comp/cluster/workqueue 238 green
- [ ] **0.10.10** 🧑 Lab: heterogeneous 3–4 laptop runs with a node killed mid-way

**Success criteria**
1. 3,000-image classification split over 3 heterogeneous laptops produces output **identical** to a single-laptop run.
2. Makespan **≤ 1.25 ×** the ideal Σ-throughput bound *(calibrate)*.
3. Speedup vs best single laptop **≥ 2.0 ×** on 3 laptops for CPU/GPU-bound tasks *(calibrate)*.
4. Killing one laptop mid-run still yields correct, complete output.
5. Planner picks "don't split" when splitting is predicted slower (test case).

**Deliverable:** Mode 3 finished.
**Unlocks:** V0.12 (work queue), V0.14.

---

## V0.11 — Mode 2 Remote GPU

> **DEFERRED — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (2026-09-20).** Not in the 10-week
> plan. Kept in full as the future-work record; architecture and ADRs stay valid. Revisit **first** once Mode 3 is complete and margin remains: Mode 4 extends Mode 2, so this is the step that moves the team's original goal forward. Costs its full 13 tasks (GPU service, tensor codec, `torch.export` path).

**Objective:** a program on one laptop uses another laptop's GPU for real PyTorch computation, efficiently and safely.

**Concepts introduced:** GPU sessions · remote tensor handles & lifetimes · tensor serialization without pickle · `torch.export` · call pipelining · VRAM admission control · typed remote errors.
**Dependencies:** PyTorch (CUDA), torch.export; GPU nodes from V0.2.
**Prerequisites:** V0.5 (model blobs), V0.6; V0.7 for mTLS on GPU plane (can integrate when ready); S3/S4 results.

**Tasks**
- [ ] **0.11.1** GPU service process: managed by agent, own uv venv (torch+CUDA), health checks, crash restart
- [ ] **0.11.2** `gpu.proto`: OpenSession, PutTensor(stream), LoadModule, Call, GetTensor(stream), Free, CloseSession; session leases
- [ ] **0.11.3** Tensor codec: raw bytes + dtype/shape/strides, chunked, optional fp16/bf16 compression; never pickle
- [ ] **0.11.4** Remote handles: refcounts, session GC, VRAM accounting
- [ ] **0.11.5** Module offload via `torch.export` blobs (CAS-cached on GPU nodes)
- [ ] **0.11.6** SDK: `mf.remote_gpu("auto"|node)`, `RemoteModule`, `RemoteTensor`, `.to_local()`; sync + async APIs; call pipelining
- [ ] **0.11.7** AcceleratorRequest → planner placement (local GPU / remote GPU / CPU) with reasons
- [ ] **0.11.8** Admission control: VRAM reservations, max sessions per GPU, owner caps, local-user priority
- [ ] **0.11.9** Errors: remote CUDA OOM → typed exception; disconnect → `SessionLost` with optional reopen; timeouts
- [ ] **0.11.10** Per-call telemetry (bytes in/out, RTT, remote compute time)
- [ ] **0.11.11** Communication-heavy benchmark families
- [ ] **0.11.12** *Stretch:* `torch.compile` backend shipping FX graphs (transparency level 4)
- [ ] **0.11.13** 🧑 Lab: GPU-less/weak laptop drives inference and a training loop on a remote GPU

**Success criteria**
1. ResNet-50 and BERT-base inference via Remote GPU: outputs match local reference (**atol 1e-4**, fp32).
2. Measured call time within **20 %** of the bandwidth+compute model prediction *(calibrate)*.
3. Remote OOM surfaces as a clear typed exception; killing the GPU laptop never hangs the client beyond the configured timeout.
4. Two clients share one GPU safely under VRAM limits.
5. Speedup over local CPU demonstrated for compute-heavy cases; planner keeps small/chatty cases local.

**Deliverable:** Mode 2 finished for PyTorch module-level workloads.
**Unlocks:** V0.12.

---

## V0.12 — Mode 4 Combined GPU — inference

> **DEFERRED — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (2026-09-20).** Not in the 10-week
> plan. Kept in full as the future-work record; architecture and ADRs stay valid. Requires V0.11 **and** V0.10's work queue — ~87 tasks in total, against Mode 3's ~64. Also bounded by ~4 GB VRAM per card.

**Objective:** a program on one laptop uses all laptops' GPUs as one combined GPU for inference over large inputs.

**Concepts introduced:** multi-session pools · replication · heterogeneous batch sharing · ordered streaming combine · member failure recovery.
**Dependencies:** V0.11 runtime; V0.10 work queue.
**Prerequisites:** V0.11 (V0.10 for the shared queue).

**Tasks**
- [ ] **0.12.1** `mf.combined_gpu(nodes="auto")` pool (includes local GPU as a member)
- [ ] **0.12.2** Parallel model replication (CAS cached)
- [ ] **0.12.3** `pool.map(model, dataset, batch_size)` and batch-split call `pool(model)(big_batch)`
- [ ] **0.12.4** Heterogeneous sharing via shared work queue with per-member in-flight window
- [ ] **0.12.5** Ordered streaming combine
- [ ] **0.12.6** Member failure → in-flight batches reassigned; slow-member demotion
- [ ] **0.12.7** Planner: member selection (drop net-negative links), speedup prediction, single-GPU fallback
- [ ] **0.12.8** Combined GPU dashboard page
- [ ] **0.12.9** Benchmark families
- [ ] **0.12.10** 🧑 Lab: 3–4 GPU runs on Ethernet and Wi-Fi

**Success criteria**
1. Results identical and in order vs single-GPU reference.
2. Throughput with all GPUs on Ethernet **≥ 75 %** of the sum of individually measured throughputs for compute-heavy batch workloads *(calibrate)*.
3. Across the benchmark suite, planner never chooses a pool slower than the best single GPU by more than **10 %**.
4. Killing a member mid-run → job still completes correctly.

**Deliverable:** Combined GPU inference finished.
**Unlocks:** V0.13, V0.16.

---

## V0.13 — Mode 4 Combined GPU — training

> **DEFERRED — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (2026-09-20).** Not in the 10-week
> plan. Kept in full as the future-work record; architecture and ADRs stay valid. Requires V0.12. The most research-grade work in the plan.

**Objective:** train one model using all laptops' GPUs together, with results equivalent to single-GPU training.

**Concepts introduced:** data parallelism · all-reduce (coordinator, ring) · sample-weighted gradient averaging · gradient compression · checkpoint/resume · synchronization cost modelling.
**Dependencies:** S5 decision (gloo vs custom ring).
**Prerequisites:** V0.12.

**Tasks**
- [ ] **0.13.1** `pool.train(...)` high-level API + `pool.step(batch)` custom-loop API
- [ ] **0.13.2** Replica initialization + parameter broadcast
- [ ] **0.13.3** All-reduce v1 (coordinator reduce) → v2 (ring over TCP, or gloo per S5); optional fp16 gradients
- [ ] **0.13.4** Heterogeneous batch shares with sample-weighted averaging
- [ ] **0.13.5** Periodic checkpoint to Laptop A; resume with remaining members after failure
- [ ] **0.13.6** Sync-overhead telemetry; planner decides when training pool is worthwhile
- [ ] **0.13.7** Benchmark families (models × batch × network)
- [ ] **0.13.8** 🧑 Lab: ResNet-18/CIFAR-10 and a small transformer on Ethernet and Wi-Fi

**Success criteria**
1. Pooled training reaches the same accuracy (**± 1 %**) as single-GPU training with identical global batch/hyperparameters.
2. Step-time speedup on Ethernet ≥ *(calibrate from S5)* for compute-heavy models.
3. Member failure mid-epoch → training continues from last checkpoint automatically.
4. On Wi-Fi where pooling is slower, planner correctly keeps training on a single GPU.

**Deliverable:** Combined GPU training finished.
**Unlocks:** V0.14.

---

# M4 — INTELLIGENCE

## V0.14 — Unified planner v2

> **RE-SCOPED — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (2026-09-20).** Compares **Local + Mode 1 + Mode 3**
> only, since Mode 2 and Mode 4 are deferred. The `AcceleratorRequest` candidate space (0.14.1) and any
> prerequisite on V0.13 fall away; everything else stands. Prerequisites become **V0.9 + V0.10**.

**Objective:** one planner comparing **every mode this build ships** (Local, Mode 1, Mode 3) with calibrated analytical cost models and clear explanations.

**Concepts introduced:** unified candidate spaces · set enumeration with pruning · model calibration from telemetry · risk-adjusted cost · decision regret · oracle comparison.
**Prerequisites:** V0.9, V0.10 (V0.13 dropped by ADR-0012). Brief: `docs/briefs/V0.14-unified-planner-v2.md` (D290–D303) · ADR-0020.

**Tasks**
- [x] **0.14.1** Unified candidate generation: JobRequest (Local, M1 × nodes, M3 × node-sets); AcceleratorRequest = typed "not supported in this build" (D290) · + replayable decisions (D292) — **done 2026-10-21** merge `f72681c0`: tester r1 PASS (4200 decisions byte-identical vs pre-refactor planner; savepoint isolation ×8 real PG), reviewer APPROVE; unit 18423, component planner/jobs/migration 149, cluster test_v010_mode3 4 passed
- [x] **0.14.2** Pruning (dominated sets, infeasible links) keeping decision latency low — **done 2026-10-22** merge `3392fe49` (G3): tester r1 FAIL D1 → fix r1 → tester r2 PASS, reviewer r1+r2 APPROVE, merge-integration (M1–M5, REPLAY_VERSION 2) APPROVE; post-merge main: ruff/format/mypy clean, unit 19511 passed, cluster test_v010_mode3 4 passed; D309–D311; p99 16-node split ≤33 ms
- [x] **0.14.3** Analytical cost models per ARCHITECTURE §7, parameters calibrated from telemetry — **done 2026-10-22** merge `092b60c7`: tester r1 PASS + r2 PASS (fix round 1: format:true, CLI scrub, no-new-runs skip, refit off loop, rejected pin refused), reviewer r1+r2 APPROVE; post-merge main: ruff/format/mypy clean, unit 19182 passed; G2 component 703, cluster test_v010_mode3 4 (worktree)
- [x] **0.14.4** Reliability-based risk penalty — **done 2026-10-22** merge `3392fe49` (G3): tester r1 FAIL D1 → fix r1 → tester r2 PASS, reviewer r1+r2 APPROVE, merge-integration (M1–M5, REPLAY_VERSION 2) APPROVE; post-merge main: ruff/format/mypy clean, unit 19511 passed, cluster test_v010_mode3 4 passed; D309–D311
- [x] **0.14.5** Objectives: min completion time (default), min network usage, prefer-local — **done 2026-10-22** merge `3392fe49` (G3): tester r1 FAIL D1 → fix r1 → tester r2 PASS, reviewer r1+r2 APPROVE, merge-integration (M1–M5, REPLAY_VERSION 2) APPROVE; post-merge main: ruff/format/mypy clean, unit 19511 passed, cluster test_v010_mode3 4 passed; D309–D311
- [x] **0.14.6** Scheduler/decision page: candidate table with predicted breakdown vs actual — **done 2026-10-22** merges G4 `38dd1a25` + G5 `6e014015` + post-merge items `02e10a85`…`eea1327c` (D321/D323): G4 tester r1+r2 PASS (T1–T4 fixed), reviewer r1+r2 APPROVE, paired criterion 2 (D322); G5 tester r1 PASS, reviewer r1 APPROVE; post-merge main: ruff/format/mypy clean, vitest 731, typecheck/lint/check:api clean, unit 19905 passed exit 0 (one unnamed flake in the first run, clean rerun), G4 eval component + G5 e2e (real API reports) pass
- [x] **0.14.7** Offline evaluator: replay benchmark dataset → regret vs oracle for every policy — **done 2026-10-22** merges G4 `38dd1a25` + G5 `6e014015` + post-merge items `02e10a85`…`eea1327c` (D321/D323): G4 tester r1+r2 PASS (T1–T4 fixed), reviewer r1+r2 APPROVE, paired criterion 2 (D322); G5 tester r1 PASS, reviewer r1 APPROVE; post-merge main: ruff/format/mypy clean, vitest 731, typecheck/lint/check:api clean, unit 19905 passed exit 0 (one unnamed flake in the first run, clean rerun), G4 eval component + G5 e2e (real API reports) pass
- [~] **0.14.8** 🧑 Lab validation runs with fresh (unseen) workloads — **agent side done 2026-10-22** merge `dbfe3a59` (G6: migration 0033 `decision_ms` + criterion-3 latency verdict, harness `tests/cluster/test_v014_planner_e2e.py` green, checklist `lab/checklists/V0.14-planner.md`, D329 stratified held-out slice; gates green, tester r1 PASS (T1 fixed), reviewer APPROVE ×2). **Open: the 🧑 lab run per the checklist.**

**Success criteria**
1. On held-out benchmark groups, chosen plan within **15 %** of oracle in **≥ 80 %** of cases *(calibrate)*.
2. Mean regret lower than every rule baseline.
3. Decision latency **< 100 ms** for 4 nodes.
4. Every decision explainable in UI and `compute explain`.

**Deliverable:** unified, explainable, cost-based planner.
**Unlocks:** V0.15.

---

## V0.15 — ML planner

**Objective:** machine learning improves cost predictions from real telemetry, measurably beating the analytical planner — with safe fallback.

**Concepts introduced:** supervised regression on tabular data · group-wise cross-validation · leakage prevention · quantile/uncertainty estimation · OOD detection · model registry · online retraining · ablation studies.
**Dependencies:** scikit-learn, XGBoost, LightGBM.
**Prerequisites:** V0.14 + sufficient telemetry (≥ several thousand runs across families).

**Tasks**
- [ ] **0.15.1** Dataset builder from telemetry; group splits by workload family
- [ ] **0.15.2** Feature pipeline (workload, hardware scores, load, network, cache)
- [ ] **0.15.3** Targets: compute time per node, env time, per-mode overhead residuals, failure probability
- [ ] **0.15.4** Model comparison: linear/ridge, tree, random forest, XGBoost, LightGBM, kNN (+ quantile variants)
- [ ] **0.15.5** Uncertainty + OOD detection → fallback to analytical
- [ ] **0.15.6** Planner integration as cost-component predictors; decision records store predictor version
- [ ] **0.15.7** Model registry, versioning, scheduled retraining
- [ ] **0.15.8** Evaluation report: MAPE, regret vs analytical/rules/oracle, ablations, feature importance
- [ ] **0.15.9** Dashboard: predicted vs actual, model versions
- [ ] **0.15.10** Optional experiment: direct strategy classifier vs cost-regression (for the report)

**Success criteria**
1. ML-hybrid planner reduces mean regret vs analytical v2 on held-out workload groups, reported with 95 % confidence intervals.
2. Never worse than fallback beyond a set tolerance on any benchmark family.
3. Retraining automated and reproducible.

**Deliverable:** AI scheduler integrated with real execution telemetry.
**Unlocks:** V1.0 evaluation.

---

# M5 — ADVANCED & PRODUCT

## V0.16 — Model splitting (Mode 4 stage 3)

> **DEFERRED — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (2026-09-20).** Not in the 10-week
> plan. Kept in full as the future-work record; architecture and ADRs stay valid. Requires V0.12.

**Objective:** run a model larger than any single GPU's memory by splitting its layers across GPUs.

**Concepts introduced:** layer profiling · memory-constrained partitioning (dynamic programming) · pipeline execution · activation transfer · micro-batching.
**Prerequisites:** V0.12.

**Tasks**
- [ ] **0.16.1** Per-block time/memory profiler per GPU
- [ ] **0.16.2** DP partitioner minimising bottleneck stage subject to VRAM and link costs
- [ ] **0.16.3** Pipeline executor across GPU services; micro-batching
- [ ] **0.16.4** Supported family: sequential models, transformer block stacks (incl. common Hugging Face models)
- [ ] **0.16.5** Failure → replan; planner candidate when model > any VRAM
- [ ] **0.16.6** 🧑 Lab: model exceeding the largest single GPU's VRAM

**Success criteria**
1. A model larger than any single available GPU runs across ≥ 2 GPUs with reference-equal outputs.
2. Partitioner beats equal-layer split in throughput on heterogeneous GPUs.

**Deliverable:** model-splitting capability inside Combined GPU.

---

## V0.17 — Productization

**Objective:** installable, self-running software on Windows and Linux with a first-run experience that needs no terminal.

**Concepts introduced:** OS services · installers · bundled runtimes · upgrade/uninstall · first-run UX.
**Prerequisites:** V0.7, V0.8.

**Tasks**
- [ ] **0.17.1** Windows installer: bundled Python runtime, agent as Windows service, firewall rules, NVIDIA/Docker checks
- [ ] **0.17.2** Linux packages (.deb and/or AppImage) + systemd service
- [ ] **0.17.3** Controller install incl. managed PostgreSQL
- [ ] **0.17.4** First-run wizard (create fabric / join fabric with code or QR)
- [ ] **0.17.5** Settings UI: resource caps, GPU sharing, node name
- [ ] **0.17.6** Upgrade path and clean uninstall
- [ ] **0.17.7** User documentation (install, pairing, running workloads, SDK)
- [ ] **0.17.8** 🧑 Fresh-machine install tests

**Success criteria**
1. A fresh Windows laptop goes from download to joined fabric in **< 10 minutes** with no terminal use.
2. Uninstall leaves no running services or firewall rules.
3. Install/upgrade/uninstall tested on fresh machines for both OSes.

**Deliverable:** installable product.

---

## V1.0 — Release hardening & final evaluation

**Objective:** everything re-verified on the release build; the system proven stable, secure, and measurably intelligent.

**Tasks**
- [ ] **1.0.1** Full acceptance re-run of V0.3–V0.17 success criteria on release build
- [ ] **1.0.2** 72 h soak + chaos campaign
- [ ] **1.0.3** Performance profiling & tuning pass
- [ ] **1.0.4** Final security review
- [ ] **1.0.5** Documentation complete (user guide, architecture, API reference, ADRs)
- [ ] **1.0.6** Final evaluation: research question with all baselines, CIs, ablations (report/paper-ready)
- [ ] **1.0.7** Scripted demonstration scenarios for every mode
- [ ] **1.0.8** Release tag + changelog

**Success criteria**
1. All acceptance criteria pass on the release build.
2. 72 h soak: no crashes, leaks, or lost jobs.
3. Evaluation report complete with reproducible experiment scripts.

**Deliverable:** MeshForge v1.0.

---

## Cross-cutting tracks

| Track | Starts | Rule |
|---|---|---|
| **Telemetry** | V0.4 | every new execution path records phase timings + snapshots |
| **Benchmarks** | V0.9 | every mode version adds its benchmark families |
| **Dashboard** | V0.8 | every mode version adds its page |
| **Security** | V0.3 (identity) | every new network endpoint uses mTLS/tokens once V0.7 lands |
| **Docs/ADRs** | V0.1 | architectural change ⇒ ADR; behaviour change ⇒ docs |
| **Testing** | V0.1 | each version extends cluster + lab suites; no regressions allowed |
| **Cross-platform** | V0.1 | CI on Linux + Windows; lab tests always include a Windows node |

---

## Risk register

| # | Risk | Impact | Likelihood | Mitigation | Watch in |
|---|---|---|---|---|---|
| R1 | Too few / weak NVIDIA GPUs in the lab | Mode 2/4 demos weak | Med | inventory early (V0.2); plan around real VRAM; model choice sized to hardware | V0.2 |
| R2 | Windows networking / firewall / Docker Desktop GPU quirks | blocks multi-node work | High | native Windows agent; S2/S6 spikes; documented setup; venv fallback | V0.2–V0.4 |
| R3 | Network too slow for Combined GPU gains | Mode 4 speedup small | Med | Ethernet switch; compression; planner avoids net-negative pools; report honestly | V0.12–V0.13 |
| R4 | `torch.export` fails on some models | Mode 2/4 coverage gaps | Med | supported-model list; fallback to state_dict + registered architectures | V0.11 |
| R5 | Cross-OS collectives (gloo) unusable | training design change | Med | custom TCP ring (S5) | V0.2, V0.13 |
| R6 | Insufficient/noisy telemetry for ML | ML gains unproven | Med | benchmarks from V0.9; repetitions; CV checks; group splits | V0.9, V0.15 |
| R7 | AI-generated code grows opaque | team can't explain system | Med | ADRs, reviews by separate agents, small modules, docs per version | always |
| R8 | Agents can't access Windows laptops directly | slow lab testing | High | SSH/Claude Code on each laptop, or scripted 🧑 steps with paste-back | V0.2 |
| R9 | Controller laptop off/asleep | fabric down | Med | choose always-on laptop; document; failover future work | V0.3 |
| R10 | Docker socket = root-equivalent | security exposure | Med | minimal agent, sandbox hardening, owner policies, threat model | V0.7 |

---

## Blockers & problems discovered

| Date | Item | Status | Owner |
|---|---|---|---|
| 2026-09-15 | Laptop 1 NVIDIA driver not loading (Secure Boot key not enrolled) | **resolved 2026-09-19** (MOK enrolled; driver 595.91.07 works, Secure Boot still on) | team |
| 2026-09-15 | Hardware of laptops 2–4 unknown | open | team |
| 2026-09-15 | AI-agent access to Windows laptops undecided | open | team |
| 2026-09-15 | Ethernet switch not yet purchased | open | team |
| 2026-09-15 | Git repository / GitHub not yet created | local repo created (0.1.1); private GitHub repo + push pending — humans push, agents never push | team |
| 2026-09-15 | Carry-over for V0.3 (from 0.1.7 review): DB statement/lock timeout (`command_timeout`) via `DatabaseSettings`; rule "all DB writes via SQLAlchemy" or `BEFORE UPDATE` trigger for `updated_at`; every `CheckConstraint` needs `name=`; log `store.migrations.failed` with revision; ~~session-scoped Postgres fixture~~ (done in 0.1.8) | open | V0.3 / 0.1.8 |
| 2026-09-15 | Carry-over for V0.3/V0.4 (from 0.1.5): Python `AttemptState` + codec + real parity test (skipped placeholder exists); `MAX_TENSOR_CHUNK_BYTES` constant (V0.11); ~~Hello session epoch~~ (ADR-0002 Updates 2026-09-20) + request_id (V0.3); GPU session_id bound to mTLS peer (V0.11) | open | V0.3 / V0.11 |
| 2026-09-15 | Follow-ups from 0.1.6 (non-blocking): pass scan limit into `redaction._quoted_end`; scrub tracebacks per line / keep tail instead of 64 KiB head; sensitive keys with non-ASCII letters not recognised (xfail pinned); `Authorization=Basic` double `[REDACTED]` cosmetic | open | next logging touch |
| 2026-09-15 | Carry-over for V0.2 (from 0.1.2 review): per-platform torch index — PyPI Windows torch is CPU-only; Linux default pulls CUDA 13 wheels | open | 0.2.3 |
| 2026-09-20 | Follow-ups from the 0.2.10 S6 review (non-blocking, none blocking APPROVE): **(1)** report CLIs overwrite hand-written prose → new task **0.2.13**; **(2)** stronger honesty check than an upper bound on `n` — when a case carries `samples`, cross-check `summary.n == len(samples)` and render INVALID on mismatch (catches a hand-edited `10**9` *and* a truncated sample list, with no arbitrary constant); **(3)** `n_headline`'s `(no valid rows)` branch should also carry the INVALID count, like the mixed branch; **(4)** `TEMPLATE.md:34` hardcodes "n < 5" while all code uses `MIN_VALID_REPS` — wants a placeholder; **(5)** `_s6/run.py` `_cleanup` removes every `s6-` container, so two concurrent S6 runs on one host would kill each other's — a per-run suffix closes it | reviewer APPROVE, 2026-09-20 |
| 2026-09-20 | **`session_epoch` stored as `NUMERIC(20,0)`** (`UInt64` TypeDecorator, CHECK `[0, 2⁶⁴−1]`), not int8 — the controller accepts the full wire domain because ADR-0013 lets the agent's counter reach 2⁶⁴−1 and refusing the upper half would strand a node with no remedy but `identity reset`; reviewer ruled it correct. **Migration `0002_nodes` was amended in place** (it never ran outside dev): anyone who ran `meshforge-controller` against a dev Postgres between `226506e` and `35bc872` must drop the dev volume or `alembic downgrade 0001 && upgrade head` | G2 tester D2 + reviewer ruling | ADR-0002 Updates / ADR-0013 (doc note pending) |
| 2026-09-21 | **V0.3 G4 review follow-ups** (final reviewer APPROVE, delta `ac20ea3..84b9a1b`; item 3 fixed by the Lead in `b0d5e0f`): **(1)** during a real outage the flusher issues up to `MAX_ISOLATED_ROWS`=100 failing statements *before* asking the ping — at `connect_timeout_s`=10 a tick can take minutes and starve `sweep_once` and the bucket drain; probe the first row, then ask `_database_answers()` immediately and return on "down" · **(2)** no `ResourceTracker.forget_node()`: once G5 adds a node-delete path, delete + re-enrol leaves the old `_Cursor.epoch` high-water fencing every report as `stale_epochs` until eviction · **(4)** the NB-1 ordering invariant is a comment + a *component* test that the token policy runs once per group, so a unit-gate edit will not see it — structural version: an `asyncio.Event` set after `_reconcile_and_replay()` (pre-set for non-reconnects) awaited by `send_report` (schedule with G6) · **(5)** `MAX_GPUS`/`MAX_DISKS` are wire caps living in `controller.session.limits`, forcing a deferred import inside `Downsampler.__init__` to dodge a real cycle — move them to a shared limits module · **(6)** with NB-3's `WHERE samples <= excluded.samples` guard, `upsert_resource_snapshots` can change zero rows while reporting the count sent, so `flusher.written` overstates (return `rowcount` if V0.8's dashboard displays it) · **ARCHITECTURE:** the `max_cursor_nodes` split (dedupe state bounded independently of the live rings) deserves one paragraph in §19 — not an ADR. **Escalated to the team: NB-2** — where NVML is missing but `nvidia-smi` works, the node reports GPUs in its inventory but **never** GPU util/VRAM in any `ResourceUpdate`, so V0.9 telemetry and the V0.15 planner would train on a node that looks GPU-equipped and never uses it; option is a low-cadence (~30 s) `nvidia-smi` poll behind a setting | open | G5 / G6 / V0.4 / team |
| 2026-09-21 | **V0.3 G3 review follow-ups** (APPROVE pass 2): `client.py` 829 lines — move `_read_loop`/`_handle`/`_heartbeat_loop` to `session/stream.py` when V0.4 adds command handlers; `zeroconf._utils.net.get_all_addresses` is a private import (prefer `ifaddr.get_adapters()` or an upper pin); UNAUTHENTICATED → backoff vs PERMISSION_DENIED → fatal to be revisited with ADR-0008 (V0.7); `identity.created/loaded` logs the identity dir path (checklist 7 asks for no user-dir paths); every failed connect consumes an epoch (correct per ADR-0002 §1, visible in logs); a second HelloAck on an open stream now counts as `stale_message` rather than `protocol_violation` (same outcome). Declared deviations accepted: `ExitCode.CONFLICT = 5`, `agent.identity_lock_timeout_s`, BACKOFF → DISCOVERING, stop half-closes and keeps reading, controller signal handlers installed before start (mDNS registration had widened a SIGTERM window), test helpers → `tests/_support/mf_controller_harness.py` | open | V0.4 / V0.7 |
| 2026-09-20 | **V0.3 G2 review follow-ups** (APPROVE pass 2): `manager.py` 829 lines — extract admission to `session/admission.py` before G3 adds replay handling; `SessionEndReason` lacks `internal_error` (reader crash persisted as `protocol_error`; needs a 0003 migration); flood test should assert `AGENT_CLOSED` now that B1 is fixed; `REJECT_CODE_INVALID_ARGUMENT` would be cleaner than `INTERNAL` for an over-long Hello; `flush_touch` is `async` with no await; controller exits 0 even when stop was caused by a dead lease timer; `SO_REUSEADDR` on Windows should be `SO_EXCLUSIVEADDRUSE`; two controllers on one DB sweep each other's sessions (single-controller assumption, document); `hardware` JSON bounded only by the 4 MiB gRPC limit (G4 caps it); `touch_node` one UPDATE per node per interval is fine at 4 nodes | open | G3 / G4 / G5 |
| 2026-09-20 | **V0.3 G1 review follow-ups** (reviewer APPROVE pass 2; blocking + #1/#3/#6 fixed in `0acf7eb`): **(2)** `load_or_create`/`next_epoch` block (lock poll ≤ 10 s, keygen, fsync) — G2/G3 must call them via `asyncio.to_thread` (docstring note pending); **(4)** Windows directory ACL uses `(F)` without `(OI)(CI)` and `_check_protected` is a no-op on Windows — tighten in G3 when the agent runtime lands; **(5)** ARCHITECTURE §12.3 had no lease arrows from DRAINING and no revoke from SUSPECT/DRAINING — **diagram amended 2026-09-20**, code table extended in G2; **(7)** `filelock.created` is exists()-before-open (both racers may protect; harmless); **(8)** `agent/identity.py` imports `cryptography` at module import — `agent/main.py --version` should import identity lazily (G5); **(9)** `HelloAck` carries no controller `protocol_version` (additive later). Tester: Windows `icacls`/`msvcrt` paths unverified on real Windows | open | V0.3 G2/G3/G5 |
| 2026-09-20 | **ADR-0013** (Proposed): node identity = ECDSA P-256 key + ULID `node_id` minted once, `session_epoch` in the same file, `0700/0600` on POSIX, `icacls` SID-restricted on Windows, partial identity is a fatal error; adds `cryptography` to core deps. P-256 over Ed25519 because grpcio's BoringSSL does not verify Ed25519 certificates by default — a key change at V0.7 would be the redesign ADR-0008 forbids | V0.3 task 0.3.2, prep in `docs/briefs/V0.3-fabric-core.md` D1 | ADR-0013 |
| 2026-09-20 | **ADR-0002 Updates** (Proposed): session epoch fences zombie streams (higher epoch wins, lower/equal rejected), `session_id` on every envelope via `HelloAck`, full-jitter backoff base 0.5 s cap 15 s + gRPC keepalive, bounded replay ring with `Reconcile.replay_from_sequence`, monotonic-clock leases 2 s / 6 s. Closes the 0.1.5 carry-over (Hello session epoch) | V0.3 tasks 0.3.1/0.3.7/0.3.8 | ADR-0002 |
| 2026-09-20 | Follow-ups from the 0.2.13 review (non-blocking, APPROVE): **(1)** ~~`_identity` captured up to the next `·`, so an annotated / re-punctuated meta line was refused as "another spike"~~ **fixed by the Lead** (first token only, message names the `> File:` line); **(2)** a *headerless* `|---|` fragment glued under the last host row makes that row the human table's header, so it appears twice after merge (no loss, cosmetic, stable); **(3)** ~~README silent on the one blind spot~~ **fixed** (blank line before your own table); **(4)** a hand-edited `Author:` on the `> File:` line is reverted every run — consider `--author` on the CLIs; **(5)** CLIs print `cannot write <out>` for every `OSError`, including a read-side `IsADirectoryError` (exit code correct) | reviewer APPROVE, 2026-09-20 |
| 2026-09-20 | Follow-ups from 0.2.6 S2 review (non-blocking, all reviewer passes closed): the server's session hold cap (`MAX_HOLD_S`) ends the stream up to 5 s late because the handler joins its request-reader thread (S2.md §6) — a `done`-aware reader would tighten it; `extra.local` carries no hostname, so same-machine detection compares `host.hostname` with `peer_identity.hostname` (two distinct machines sharing a hostname would be marked same-machine — conservative) | open | 0.2.12 |
| 2026-09-20 | Follow-ups from 0.2.7 S3 review (non-blocking): ~~promote `spikes._s1.protocol` (address rules, framing, `ProtocolError`) into `_harness/`~~ **done 2026-09-20 by the Lead (mechanical move, no behaviour change): `_harness/protocol.py` holds framing + LAN address rules, `_s1/protocol.py` keeps S1's constants and `MeasureError` and re-exports the harness names; S2/S3 import from the harness; `test_harness_protocol_boundary.py` forbids any non-S1 spike importing `spikes._s1`**; prime `payload.crc_of` before `transfers.begin` (a `--warmup 0` run would report a visibly slow first upload); list invalid chunks in `chunk_line`'s "others:" so the sentence and the sweep table agree | open | 0.2.12 |
| 2026-09-15 | Follow-ups from 0.2.2 review (non-blocking): mask UNC / `\\wsl$` user paths in `lab/_inventory/sanitize.py`; `wsl -l -v` decode ignores `WSL_UTF8=1`; runner output cap is post-read (document); pass `_RunState` explicitly if reused as a library (V0.3 agent hardware report); split `probes.py` (801 lines) into hw/sw/collect | open | next lab touch / V0.3 |

---

## Design change log

- **2026-10-11 (D156):** migration 0017 used by V0.7 G3 fix r1 (cert session end reasons); G4 → 0018, G6 secrets → 0019.

| Date | Change | Reason |
|---|---|---|
| **2026-10-17** | **V0.9 planned and approved** (`docs/briefs/V0.9-benchmark-telemetry.md`, D211–D224, groups G0–G7, ADR-0018). Item 0 = D210 list; Parquet export from Postgres run records; forced runs via planner pinning; `tc netem` via narrow sudoers rule; pandas dev-only; G1 ∥ G2; 0.9.10 may span two nights (≥ 1,000 runs in one unattended stretch). | Team: "go with your recommendations". |
| **2026-10-15** | **V0.8 planned and approved** (`docs/briefs/V0.8-dashboard-cli.md`, D169–D180, groups G1–G7, ADR-0017). 0.8.2 split into backend (G1) + frontend (G2); V0.7 D168 leftovers are V0.8 G1 item 0; browser submit is in V0.8 scope (criterion 1). | Team: "go with your suggestions". |
| **2026-10-07** | **V0.7 planned and approved** (`docs/briefs/V0.7-security-pairing.md`, D133–D146, groups G1–G7). **ADR-0016 (Accepted)** supersedes ADR-0014: fabric CA + code pairing, mTLS everywhere, per-user API tokens, owner policy on the agent, job secrets. **New task 0.7.12 🧑 lab run.** Team answers: CA installed in browsers (V0.8), CA rotation → V0.17, Windows subprocess stays unsandboxed opt-in, fabric token deleted in V0.8, no QR dependency now. | Criteria 1 and 3 need real machines; choices keep zero new dependencies. | V0.7, V0.8, V0.17 |
| **2026-10-05** | **V0.6 planned** (`docs/briefs/V0.6-scheduling-reliability.md`, D102–D119): 4 sequential groups (G1 reliability floor 0.6.9/0.6.10/0.6.14 + dispatcher/gc_index splits + D59; G2 planner core; G3 retries/recovery/chaos; G4 network matrix + estimator). **`cost_model` policy added to 0.6.3's list and made the default** (needed for criterion 4). **Local seeding (D109):** `compute run` puts the tree into the submitting laptop's agent CAS. **ADR-0015 (Proposed):** at-least-once execution, exactly-once commit; criterion 1 counts duplicate commits and duplicates under kills, partition overlaps reported separately. | Pure planner keeps Mode 3/ML reuse; honest retry semantics. | V0.6 |
| **2026-09-29** | **V0.4 criterion 1 reworded:** byte-identical on the same hardware class; within `compare.py`'s stated tolerance across different CPU/GPU models (D76). | GPU (and some CPU) kernels are not bit-reproducible across hardware; the old wording would make the 0.4.15 lab result pass or fail depending on which document was read (G5 reviewer pass 2). |
| **2026-09-28** | **V0.4 planned for pace** (`docs/briefs/V0.4-remote-execution.md`): 17 tasks in **5 groups** (V0.3 used 7 for 15), **G1 ‖ G2 ‖ G3 run in parallel** on disjoint files, all decisions settled up front (D40–D54). **D40 departs from the 0.4.16 row:** the timing rule `HALF_CLOSE_CONFIRM_S` is **deleted, not kept as a fallback** — an old agent's clean stop degrades to SUSPECT → OFFLINE at lease expiry, the safe direction. **ADR-0014 (Proposed):** fabric bearer token on mutating/job REST + agent execution opt-in until V0.7. `split:` in a manifest is rejected until V0.10, never silently ignored. | §0d pace check: ~1.8 tasks/day against ~4.3 needed; larger groups and parallel lanes are levers 1–2. V0.4 is the first version that runs code on other laptops (CLAUDE.md §9). | V0.4, V0.6, V0.7 |
| **2026-09-24** | **V0.3 G6 (0.3.14): liveness on session end redesigned and a real cluster tier added.** ARCHITECTURE §12.3 amended before code (D19 clean close → OFFLINE, abrupt → SUSPECT + lingering lease; D20 fenced state writes). Tester round 1 FAIL (2 major) → D26 LATE-1 grace 20 s, D27 reconnect cap 15 → 5 s, D28 a pending higher-epoch Hello owns the state, D29 agent `hello_timeout_s` 3 s with `cap + hello ≤ 8 s` → round 2 PASS → reviewer REQUEST_CHANGES (lint only) → fixed. D23 replaces the Toxiproxy binary with an in-process proxy (ADR-0010 Updates). grpcio capped `<1.85` because the clean-close inference is measured on 1.84. **Behaviour change:** an agent `config.toml` with `hello_timeout_s = 5` now refuses to start. | NB-G5-1 made `state` lie; the reviewer's first fix ("every close → OFFLINE") would have turned every Wi-Fi blip into a V0.6 retry and a false soak OFFLINE. Criterion 2 was only met by luck with a 15 s cap. The explicit Goodbye (0.4.16) is the durable replacement for the timing inference. | V0.4 (0.4.16), V0.6 |
| **2026-09-25** | **V0.3 G7 (0.3.4): hardware scores on the fabric.** Two sequential implementer runs (A agent-side, B wire/controller/REST/CLI) to survive usage limits — both were cut once and resumed with no loss because they committed early. `numpy` added to the `agent` extra (D30) so CPU/memory scores exist without torch; GPU scores are typed `UNAVAILABLE: torch_not_installed` until ADR-0011. Tester found a GPU score accepted for an undeclared GPU → **D39** (whole set rejected). New small protocol convention (implementer's, reviewer-accepted, **team to confirm**): the `hardware-scores` capability tag distinguishes REST `pending` from `none`. Known gap kept: a DB-refused score write is already acked and heals on the next Hello. | Scores are ML-planner features (ARCHITECTURE §7, V0.15): a quiet wrong number is worse than none, so every path was traced for 0/NaN/stale/wrong-GPU values. | V0.4 (0.4.17), V0.7, V0.15 |
| **2026-09-24** | **NB-G5-1 recorded, not fixed: a cleanly closed session never moves `node.state`.** `SessionManager._close` enqueues only `close_session(reason)`; `apply_event` is reachable only from the lease timer and heartbeat paths, which walk `self._live`, from which `_close` has already removed the session. The only thing that corrects the row is `registry.sweep_open_sessions` at controller start-up. So a laptop that shuts down gracefully reads `state: online` indefinitely and **`connected` is the only honest field.** Confirmed independently by the tester (tight lease, graceful `request_stop()`: `state` stayed `online` for 6× the would-be OFFLINE deadline) and by the reviewer reading the code. **Anything reading liveness before G6 lands must use `connected` / `sample_age_s`, never `state` alone** — including V0.14's rule-based planner and any V0.15 training label. **Fix belongs to G6**, and needs an ARCHITECTURE §12.3 amendment first: an explicit `STREAM_CLOSED` event with `ONLINE\|SUSPECT → OFFLINE`, written from `_close` on the same `live.writer` chain as `close_session` (same chain = no reordering against the session-end row; idempotent because `_close` already early-returns on `live.ended`), with **supersession excluded** — the new session's `Hello` owns the state. That exclusion is why it is a §12.3 decision, not a one-line patch. | G5 was told not to touch the liveness machine, and inventing a transition inside a REST task is precisely how G4's three fix-induced defects happened. D16's three-field design is what makes the lie survivable — a good argument that D16 was right. | G6 |
| **2026-09-24** | **`pytest -m unit` exits 139 (SIGSEGV) after a fully green summary** — pre-existing, reproduced with and without this session's changes. faulthandler: `Fatal Python error: Segmentation fault` in a native thread with `<no Python frame>`, i.e. a C-level crash at interpreter shutdown. Each of `tests/unit/{cli,common,agent,controller}` exits **0** alone; only the whole `tests/unit` set crashes, and the top-level files are the ones that import grpc/protobuf — prime suspect `grpcio`'s C core. **This will make the first GitHub Actions `unit` job red no matter how many tests pass, so it blocks closing V0.1 (0.1.4, 0.1.10).** Nobody caught it because every agent round reported pass counts and never the exit code. | found while closing G5; out of G5's scope, raised as its own task. The lasting lesson is the gate gap: a green summary is not a green run, and `pytest-timeout` cannot see this. | open / new task |
| **2026-09-20** | **Scope set by [ADR-0012](adr/0012-scope-ship-mode3-complete.md): ship Mode 3 complete and production-grade (Mode 1 is its foundation), AI planner + telemetry + security + dashboard in full; **defer Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16)**; re-scope V0.14 to Local + M1 + M3; move V0.9 before V0.10** | team stated the real constraint (one Claude account, ~10 weeks ≈ 90 sessions). Measured throughput (`git log`: 1.04 tasks/h on V0.1 scaffolding, 0.60 on V0.2 spikes) puts capacity at 140–180 tasks, not 171-and-rising. Mode 3 chosen over the GPU modes because V0.12 requires V0.11 **and** V0.10 (~87 tasks vs ~64), Mode 3 uses the CPU-only Laptop 4, and unequal shares across 2× RTX 3050 + RTX 2050 + CPU give the planner a real optimization problem. Supersedes the "no deadline, never cut scope for time" rule |
| **2026-09-20** | **S4 (0.2.8) and S5 (0.2.9) drop off the critical path**; ADR-0011 no longer gates the plan's spine | both existed for Mode 2 / collectives. torch is still needed for GPU-backed Mode 3 chunks, so ADR-0011 still matters, just not as a blocker |
| 2026-09-15 | Roadmap v1 drafted: 18 versions in 5 milestones | initial plan |
| 2026-09-15 | Four execution modes (1 Job Offloading, 2 Remote GPU, 3 Task Splitting, 4 Combined GPU) replace earlier "ModelPool" framing | team decision (brief §40) |
| 2026-09-15 | Roadmap v1 approved by team; one Claude session per version | team decision |
| 2026-09-15 | Mode 3 and Mode 4-inference share one work-queue abstraction | avoid duplicate logic; natural heterogeneity balancing |
| 2026-09-15 | Team accepted ARCHITECTURE v1 + ADRs 0001–0010; no license file yet; private GitHub repo, agents commit locally, humans push | team decision |
| 2026-09-15 | Dev tools are a uv dependency group `dev`, not an extra | uv-native; synced by default |
| 2026-09-15 | Agent never imports `gpu_service` (separate process, gRPC) — enforced by import-linter + completeness guard `tests/unit/_import_guard.py` | keep torch off the agent; stop silent boundary gaps |
| 2026-09-15 | FailureType = 22 permanent values (adds `vram_oom`, `resource_busy`, `timeout` vs `deadline_exceeded`), mirrored 1:1 in proto with a parity test; BlobRef digest uses `sha256:<hex>` | DB/ML labels are permanent; one text form for digests |
| 2026-09-15 | Log redaction: key-based + value scrubbing (URL creds, Bearer/Basic, key=value) with linear-time scanner and 64 KiB cap, in `common/redaction.py` | secrets never in logs (§16) without log-triggered DoS |
| 2026-09-15 | Test tiers = folders (unit component cluster lab e2e soak chaos); `docker`, `gpu`, `slow` are capability markers; optional-extra imports must follow `pytest.importorskip` | agents can't mis-tier tests; Mode 2/4 tests can be component + gpu; plain `uv sync` must always work |
| 2026-09-15 | Test containers: labelled, atexit + finally cleanup, SIGINT-deferring shield, namespace-safe stale sweep | DEFECT-0.1.8-01 (leak on Ctrl-C); WSL + Windows share one Docker daemon |
| 2026-09-15 | CI: unit job uses plain `uv sync --locked` on Linux + Windows; required check `ci-ok`; actions pinned by commit SHA; Dependabot weekly | criterion 1 honesty; supply-chain safety |
| 2026-09-15 | V0.2 agent-side work (lab scripts, spikes) starts while V0.1 criteria 1–2 wait for the 🧑 GitHub CI run; V0.1 closes when that run is green | lab scripts don't depend on CI; the only V0.1 gap is a human push |
| 2026-09-15 | Lab scripts: stdlib-first, own process group per command + tree kill, one shared run budget (`--timeout`, default 60 s); inventory JSON has a top-level `problems` list for human-actionable issues (probe ok ≠ component healthy) | tester DEFECT-2/3; a broken driver must not hide inside `ok: true` |
| 2026-09-15 | S1 network spike uses a Python TCP/RTT tool as primary (iperf3 optional cross-check) | iperf3 not installed on Laptop 1 and needs a third-party download on Windows; same code path the V0.3 agent network probe can reuse |
| 2026-09-16 | ADR-0011 (Proposed): torch from the official cu126 index on Linux + Windows via `[tool.uv.sources]`; GPU service refuses to start without working CUDA; confirm or move to cu130 in 0.2.12 from inventory | PyPI Windows torch is CPU-only; cu130 needs driver ≥ 580 and drops pre-Turing GPUs; laptops 2–4 unknown → ADR-0011 |

---

## Next recommended task

1. 🧑 Team reviews [ARCHITECTURE.md](ARCHITECTURE.md) and this roadmap; comments → revision v1.1.
2. On approval: start **0.1.1** (repository init) and, in parallel, 🧑 **0.2.1** (driver fix) and **0.2.2** (inventory).
