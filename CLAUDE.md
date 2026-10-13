# CLAUDE.md — Agent Operating Guide

Project: **Intelligent Distributed Compute Fabric for Heterogeneous Personal Devices** (working title; final name undecided, "MeshForge" used informally).
Final Year / 7th Semester CS project, team of four students.

> **Read `docs/PROJECT_BRIEF.md` in full before doing any work.** It is the complete product and engineering vision. This file tells you *how to work*; the brief tells you *what we are building*.

---

## 1. Your role

Act as a **senior distributed-systems architect, systems engineer, ML engineer, and technical project lead**.

- You are NOT executing a pre-made design. Think independently.
- The brief is a **vision, not a rigid low-level spec**. Its capabilities are requirements; most implementation details are open.
- If an idea in the brief is technically weak, say why and propose something stronger.
- Do not discard difficult components to make things easier. Do not promise impossible behaviour either.
- Be critical, realistic, and ambitious.

## 2. The one question every decision must preserve

> **"Given a workload and the heterogeneous computing resources currently available, what is the best practical way to execute that workload?"**

Possible answers: **LOCAL · MODE 1 JOB OFFLOADING · MODE 2 REMOTE GPU · MODE 3 TASK SPLITTING · MODE 4 COMBINED GPU**. All live inside one planner and one fabric — never separate projects.

| | **One laptop** | **Many laptops** |
|---|---|---|
| **Task is sent away** | **Mode 1 Job Offloading** — complete task runs on another laptop; only output returns | **Mode 3 Task Splitting** — task divided into sub-tasks; each laptop runs its part; results combined |
| **Program stays on my laptop** | **Mode 2 Remote GPU** — GPU calculations run on one other laptop's GPU | **Mode 4 Combined GPU** — all laptops' GPUs act as one powerful GPU for the program on A |

- Mode 3 extends Mode 1; Mode 4 extends Mode 2 (shared runtimes — never build twice). Local is always a baseline.
- Mode 4: calculation data must travel over the network; gain is more than A alone but less than the full sum. Scope: PyTorch AI workloads.
- **Planner/AI** picks the mode, the machines, and how much work each machine gets; rule-based first, ML later predicts times from recorded runs.
- Details: brief §12 and §40 decision log. ("ModelPool" is the old name for Mode 4.)

## Quality bar
The deliverable is a **polished, finished product with really working features — not a demo or prototype**. The team has time. Every shipped feature must work on real machines, be tested, handle failures, and be usable. Prototypes are internal validation steps only.

## Working model
- **No deadline.** The target is completing the full vision at the quality bar — order work by dependency, never cut scope for time.
- **All code is written by AI agents.** Humans decide, approve, set up hardware, run physical multi-laptop tests, and review. Therefore:
  - Every task must be self-contained and verifiable; state exactly what a human must do on physical machines and what output to paste back.
  - Keep code and docs understandable — the team must be able to explain the system at evaluation.
  - Never mark work done on the agent's own say-so; tests + acceptance criteria (and real-machine results where required) decide.
  - Record every non-trivial decision in an ADR or the brief's decision log so later agent sessions keep context.

## 3. Fixed vs flexible (summary — details in brief §33)

**Relatively fixed:** the compute-fabric vision · all four execution modes (Job Offloading, Remote GPU, Task Splitting, Combined GPU) are major targets · AI has a real optimization role (never a chatbot/decoration) · runs on real machines · Python-first · React frontend · PostgreSQL persistence · telemetry/benchmarking is first-class · technically serious.

**Flexible (use your judgment, explain trade-offs):** architecture · controller design · centralized vs partially decentralized details · repo layout · DB schema · RPC definitions · packages · job format/manifest · file transfer · caching · scheduler + ML formulation · Remote GPU method · Task Splitting and Combined GPU strategy · service boundaries · implementation order · deployment · test organization.

Changing a *preferred technology* (brief §15/§59) requires explaining the trade-off to the team first.

## 4. Mandatory workflow

```
Understand complete project (read PROJECT_BRIEF.md)
→ Analyze technical feasibility and dependencies
→ Propose architecture
→ Create detailed version-by-version roadmap
→ DISCUSS AND REFINE ROADMAP WITH THE TEAM   ← stop here and wait for agreement
→ Save roadmap in repository
→ Begin first version → test → mark completed → update roadmap
→ Review previous version's criteria before starting the next
→ Continue until the final product
```

**Do NOT generate a large codebase up front.** No major implementation until the team has agreed the architecture and roadmap.

### Phase 0 deliverable (your first substantive response)
Cover, as an independent engineering assessment:
- **A.** Your understanding of the project
- **B.** Required technical subsystems
- **C.** Hardest engineering challenges
- **D.** Assumptions that need prototype validation
- **E.** Existing technologies/frameworks/components worth considering
- **F.** Recommended overall architecture
- **G.** Why that architecture
- **H.** Realistic development strategy that still targets the complete vision
- **I.** How the four execution modes and AI scheduling stay cleanly integrated
- **J.** What should be built/tested first
- Plus the 30 review questions in brief §38, and a draft versioned roadmap.

### Before implementing any major subsystem
1. Understand the final requirement it serves.
2. Identify its technical dependencies.
3. Inspect the existing repository.
4. Research existing approaches (when tools permit).
5. Identify engineering risks.
6. Compare plausible architectures.
7. Select the strongest practical approach.
8. Explain important trade-offs (write an ADR for significant ones).
9. Implement incrementally.
10. Test with real behaviour — never assume correctness.

## 4b. Agent team (subagents in `.claude/agents/`)

The **main session is the Lead**: it picks roadmap tasks, confirms design against ARCHITECTURE/ADRs, delegates, integrates, and is the only one who marks `[x]`. The team is authorized to use these subagents for every roadmap task:

| Agent | Role | Edits code? |
|---|---|---|
| `implementer` | writes code + unit/component tests for ONE task; hands off a note | yes (`src/`, `tests/`) |
| `tester` | independent acceptance, edge-case, failure-injection, cluster tests from the success criteria; tries to break it | tests only, never `src/` |
| `reviewer` | architecture, correctness, concurrency, security, performance review; APPROVE / REQUEST_CHANGES | no (read-only) |

```
Lead picks task → implementer → tester ─FAIL→ implementer
                                 │PASS
                                 ▼
                              reviewer ─REQUEST_CHANGES→ implementer
                                 │APPROVE
                                 ▼
             🧑 lab step if needed → Lead marks [x] with evidence, updates STATUS.md
```

Rules: pass each subagent the task ID and the previous agent's report; one task per implementer run; never skip tester or reviewer for code that ships; the Lead resolves disagreements (or asks the team) and records architectural outcomes as ADRs. Small doc-only edits may skip tester.

## 4c. Session protocol (one session per version; split large versions)

The team opens a **new Claude session per roadmap version** (large versions may span several sessions) to save tokens. Sessions share no memory — **the docs are the memory**.

**Session start:** read `docs/STATUS.md` → the current version in `docs/DEVELOPMENT_ROADMAP.md` → only the ARCHITECTURE sections and ADRs that version needs. Do not re-read everything unless required.

**Session end (mandatory, even if interrupted work):**
0. Update `docs/SESSION_HANDOFF.md` (in-flight tasks, uncommitted state, blockers) and commit work in progress.
1. Update roadmap checkboxes (`[~]` / `[x]` with evidence).
1b. **Refresh the status bar.** Whenever a roadmap checkbox changes (`[ ]`→`[~]`, `[~]`→`[x]`) — when a task or group is closed, not only at session end — run `uv run python scripts/status_bars.py` and replace the `## Progress` block in `docs/STATUS.md` with its output, and bring the `**Last updated:**` line and the `## Current Version` entry to today. A task is not closed until the bar shows it.
2. Update `docs/STATUS.md`: done, in progress, blockers, exact next task, anything a fresh session must know.
3. Record new decisions (ADR or brief §40 decision log) and roadmap changes (design change log).
3b. If the session changed anything `docs/PROJECT_SUMMARY.md` states, update it too.
4. Tell the user the one-line prompt to start the next session.

## 5. Project documents (living)

```
docs/PROJECT_BRIEF.md        vision + requirements (this handover; change only with team approval)
docs/ARCHITECTURE.md         current architecture
docs/DEVELOPMENT_ROADMAP.md  versioned plan + checklist (source of truth for progress)
docs/STATUS.md               concise current state
docs/adr/NNNN-title.md       architecture decision records
docs/PROJECT_SUMMARY.md      short plain-language overview for readers outside the team
docs/SESSION_HANDOFF.md      cold-start handover: in-flight work, blockers, traps (read first)
```
**Account rotation.** The team has several Claude accounts sharing this one repository. A usage limit can end a session mid-task, so `docs/SESSION_HANDOFF.md` is kept current **continuously** — after every finished step, not only at session end — and work in progress is committed (`wip:` prefix allowed) so the next account inherits committed truth. A new session starts by reading `docs/SESSION_HANDOFF.md`, then `docs/STATUS.md`, then the current roadmap version.
`PROJECT_SUMMARY.md` is written for non-specialist readers (e.g. college evaluation), not for agents. Keep it **up to date**: whenever a decision changes what it states — the execution modes and how they relate, the product shape, platforms, or headline technical claims — update it in the same session, keeping its simple tone and short length. It is derived from the documents above, never a source of truth.
Hierarchy: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → IMPLEMENTATION`.

### Roadmap rules
Versions `V0.1, V0.2, …`. Each version MUST define:
- main objective
- features/subsystems
- technical concepts introduced
- dependencies required
- prerequisites (what must already work)
- how it is tested
- exact success criteria
- working output/deliverable
- what the next version builds on

Break tasks down concretely (e.g. not "implement networking" but the actual networking tasks).

Checklist states:
```
[ ] not started
[~] in progress
[x] completed   ← ONLY after success criteria and tests pass. Code existing ≠ done.
```
The roadmap must always show: completed · in progress · remaining · blockers/problems discovered · major design changes · next recommended task.
If development shows the roadmap is wrong, **update the roadmap** — do not blindly follow it.

### STATUS.md sections
Current Version · Current Task · Recently Completed · In Progress · Blockers · Known Bugs · Technical Debt · Test Status · Next Recommended Task. Keep it concise.

### ADR format
Context · Decision · Alternatives · Trade-offs · Consequences.

## 6. Definition of done (per feature, where applicable)
```
[ ] implementation exists
[ ] imports/build passes
[ ] unit tests pass
[ ] integration tests pass
[ ] real machine test passes
[ ] failure behaviour checked
[ ] logs verified
[ ] documentation updated
[ ] acceptance criteria satisfied
```
An implementer never self-approves. Mocks help but never replace real multi-process / multi-machine tests.

## 7. Code quality
**Prefer:** type hints · small modules · clear naming · explicit interfaces · structured logs · configuration over constants · automated tests · reproducible setup · limited coupling · small meaningful commits · documented assumptions · reproducible experiments.

**Avoid:** giant files · unexplained global state · unnecessary frameworks · premature microservices · opaque AI-generated architecture · duplicated logic · a codebase only the AI understands.

Separate processes only where a real runtime boundary exists (likely: Control Plane, Worker Agent, Frontend). Scheduling etc. start as modules inside the control plane.

Planning and execution are separated: `Workload → Planner → ExecutionPlan → Execution Backend`.

## 8. Never let the project become
A CRUD app · simple website · dashboard-only project · chatbot · ML model + frontend · theoretical report · simulation · SSH wrapper · toy remote-exec demo · Kubernetes clone · Docker UI · remote desktop · Ray wrapper · distributed-LLM wrapper · static scheduler · benchmark dashboard · cloud-only platform · only distributed inference · only an ML scheduler · only container orchestration · Combined-GPU-only app. Remote GPU and Combined GPU must be **real**, never simulated.

## 9. Security baseline
This system is remote code execution by design. Never design for unauthenticated arbitrary code execution. Early dev may assume trusted LAN machines, but the architecture must allow node identity, mTLS, authorization, and sandboxing later.

## 10. Environment facts
- Team: 1 native Linux machine, ~3 Windows machines (WSL2 Ubuntu where useful).
- Linux-first development; Windows long-term should not require users to understand WSL.
- Initial testing on Wi-Fi/LAN; Ethernet preferred later for controlled benchmarks.
- This directory is not yet a git repository. Commit/push only when asked.
