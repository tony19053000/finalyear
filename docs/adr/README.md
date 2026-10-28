# Architecture Decision Records

Format: Context · Decision · Alternatives · Trade-offs · Consequences. Status values: Proposed → Accepted → Superseded / Deprecated, or Rejected.

Status = the `**Status:**` line of the ADR file (the source of truth; a unit test,
`tests/unit/test_v10_g5_adr_index.py`, keeps this table equal to it). **Final** statuses are Accepted,
Superseded, Deprecated and Rejected. An ADR that is still **Proposed** is listed under
[Needs team acceptance](#needs-team-acceptance-before-v100) with what the team has recorded so far: only the
team may move it (agents create ADRs as Proposed and never accept them).

| ADR | Title | Status | Release scope |
|---|---|---|---|
| [0001](0001-centralized-control-plane.md) | Centralized control plane | Accepted | current |
| [0002](0002-agent-initiated-grpc-session.md) | Agent-initiated bidirectional gRPC session | Accepted | current (its 2026-09-20 Updates section is marked Proposed - see below) |
| [0003](0003-content-addressed-data-plane.md) | Content-addressed data plane over HTTP | Accepted | current |
| [0004](0004-execution-runtimes.md) | Execution runtimes: Docker first, uv venv fallback | Accepted | current |
| [0005](0005-gpu-remoting-at-pytorch-level.md) | GPU remoting at the PyTorch level with torch.export | Accepted | deferred (Mode 2/4, ADR-0012) |
| [0006](0006-work-queue-for-splitting.md) | Pull-based leased work queue for Task Splitting and Combined-GPU inference | Accepted | current (Mode 3; the Combined-GPU use is deferred) |
| [0007](0007-planner-cost-based-ranking.md) | Planner = candidate generation + cost estimation + policy; ML predicts cost components | Accepted | current |
| [0008](0008-pairing-and-mtls.md) | Pairing with a fabric CA and mTLS | Accepted | current (detailed by 0016) |
| [0009](0009-native-windows-agent.md) | Native Windows agent (not inside WSL) | Accepted | current |
| [0010](0010-monorepo-and-stack.md) | Monorepo, single Python package, and core stack | Accepted | current |
| [0011](0011-torch-index-per-platform.md) | PyTorch wheels from the official CUDA 12.6 index on Linux and Windows | Proposed | current (GPU benchmark families) |
| [0012](0012-scope-ship-mode3-complete.md) | Ship Mode 3 (with Mode 1) complete and production-grade; defer Mode 2 and Mode 4 | Proposed | current (defines the v1.0 scope) |
| [0013](0013-node-identity-storage.md) | Node identity: ECDSA P-256 keypair + ULID `node_id`, minted once, OS-protected | Proposed | current |
| [0014](0014-interim-execution-trust.md) | Interim trust for remote execution until V0.7: fabric bearer token on REST, agent execution opt-in, scoped fetch tokens | Superseded by 0016 | historical |
| [0015](0015-retry-and-duplicate-execution.md) | Retries are at-least-once execution, exactly-once commit (V0.6) | Proposed | current |
| [0016](0016-v07-security-design.md) | V0.7 security: fabric CA, code pairing, mTLS everywhere, user tokens, owner policy, secrets (supersedes 0014) | Accepted | current |
| [0017](0017-dashboard-architecture.md) | Dashboard architecture (V0.8) | Accepted | current |
| [0018](0018-telemetry-dataset-benchmark-campaigns.md) | Telemetry dataset and benchmark campaigns (V0.9) | Accepted | current |
| [0019](0019-mode3-task-splitting.md) | Mode 3 Task Splitting: lanes, leased chunks, deterministic combine | Proposed | current |
| [0020](0020-planner-v2-calibrated-replayable.md) | Planner v2: calibrated costs, risk, objectives, replayable decisions | Proposed | current |
| [0021](0021-ml-planner-residual-hybrid.md) | ML planner as a residual hybrid over the analytical model | Proposed | current |
| [0022](0022-productization-install-services.md) | Productization: bundled runtime, machine-wide services, managed PostgreSQL, agent local console | Proposed | current |
| [0023](0023-release-acceptance-and-evaluation.md) | Release acceptance: one manifest, one check, evidence tied to the release build | Proposed | current (V1.0) |

## Needs team acceptance before v1.0.0

Each row below is built and shipping, but its ADR has not been formally accepted by the team. Before the
`v1.0.0` tag (D421) the team decides each one - **Accepted**, or a new ADR that supersedes it - and edits the
ADR's `**Status:**` line and this table together. What is recorded today:

| ADR | Recorded so far | Why it matters for the release |
|---|---|---|
| [0002 Updates](0002-agent-initiated-grpc-session.md#updates) | 2026-09-20 session epoch / reconnect policy section written as Proposed; built and tested in V0.3 | the session fencing every node relies on |
| [0011](0011-torch-index-per-platform.md) | Proposed 2026-09-16, "pending team approval and the 0.2.2 inventory of laptops 2-4" | GPU benchmark families install torch from this index |
| [0012](0012-scope-ship-mode3-complete.md) | Proposed 2026-09-20; "needs the other three team members' approval" (changes a brief §33 "relatively fixed" item). The V1.0 brief (approved 2026-10-27, Q42/Q43) plans the release on it | **the v1.0 scope itself**: Mode 2 and Mode 4 deferred. If rejected, the release notes, demos and PROJECT_SUMMARY must change |
| [0013](0013-node-identity-storage.md) | Proposed 2026-09-20, pending team approval | node identity on every laptop |
| [0015](0015-retry-and-duplicate-execution.md) | Proposed 2026-10-05, pending team approval | retry semantics users see (at-least-once run, exactly-once commit) |
| [0019](0019-mode3-task-splitting.md) | Proposed 2026-10-19, "team said proceed, review on return" | Mode 3, a headline feature |
| [0020](0020-planner-v2-calibrated-replayable.md) | Proposed 2026-10-21, team said "start V0.14" on the Lead's recommendations | the planner the evaluation measures |
| [0021](0021-ml-planner-residual-hybrid.md) | Proposed 2026-10-22, V0.15 brief D333-D344, awaiting team | the ML part of the planner |
| [0022](0022-productization-install-services.md) | Proposed 2026-10-24, "brief approved by the team 2026-10-24; ADR pending formal acceptance" | installers and services |
| [0023](0023-release-acceptance-and-evaluation.md) | Proposed 2026-10-27, "V1.0 brief approved by the team 2026-10-27; ADR pending formal acceptance" | the release rule (`release_check.py`, the acceptance manifest) |

## How to write an ADR

**When one is required.** Write an ADR for any decision that is hard to reverse or that later sessions must not re-litigate. Examples: choosing or changing a technology, protocol, or transport; changing a module or process boundary; changing a contract (proto, schema, DB model) in a non-additive way; security-model choices; any decision a spike (S1–S7) forces. Small local choices (naming, helper structure) do not need one.

**Numbering and file name.** Use the next free 4-digit number and never reuse one, even for rejected ADRs. The file name is `NNNN-kebab-case-title.md`, for example `0011-torch-index-per-platform.md`. Copy [`template.md`](template.md) and add a row to the table above.

**Status lifecycle.**
```
Proposed ──team approves──▶ Accepted ──replaced──▶ Superseded by ADR-NNNN
    │                           └──no longer relevant──▶ Deprecated
    └──team rejects──▶ Rejected (kept for history)
```
- Agents may only create ADRs as **Proposed**. Only the team moves an ADR to **Accepted**.
- Never rewrite the Decision of an Accepted ADR. Write a new ADR, mark the old one `Superseded by ADR-NNNN`, and link both ways.
- Spike findings that refine an ADR without reversing it go under an `## Updates` section with a date and a link to the `lab/reports/` evidence.

**Linking.** Add a row to the [roadmap design change log](../DEVELOPMENT_ROADMAP.md#design-change-log) for every new or superseded ADR (date · change · reason → `ADR-NNNN`). If the ADR changes the architecture, update the matching section of [`ARCHITECTURE.md`](../ARCHITECTURE.md) and link the ADR from there.
