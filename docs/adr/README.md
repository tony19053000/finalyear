# Architecture Decision Records

Format: Context · Decision · Alternatives · Trade-offs · Consequences. Status values: Proposed → Accepted → Superseded / Deprecated, or Rejected.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-centralized-control-plane.md) | Centralized control plane | Accepted |
| [0002](0002-agent-initiated-grpc-session.md) | Agent-initiated bidirectional gRPC session | Accepted |
| [0003](0003-content-addressed-data-plane.md) | Content-addressed data plane over HTTP | Accepted |
| [0004](0004-execution-runtimes.md) | Execution runtimes: Docker first, uv venv fallback | Accepted |
| [0005](0005-gpu-remoting-at-pytorch-level.md) | GPU remoting at the PyTorch level with torch.export | Accepted |
| [0006](0006-work-queue-for-splitting.md) | Pull-based leased work queue for Task Splitting and Combined-GPU inference | Accepted |
| [0007](0007-planner-cost-based-ranking.md) | Planner = candidate generation + cost estimation + policy; ML predicts cost components | Accepted |
| [0008](0008-pairing-and-mtls.md) | Pairing with a fabric CA and mTLS | Accepted |
| [0009](0009-native-windows-agent.md) | Native Windows agent (not inside WSL) | Accepted |
| [0010](0010-monorepo-and-stack.md) | Monorepo, single Python package, and core stack | Accepted |
| [0011](0011-torch-index-per-platform.md) | PyTorch wheels from the official CUDA 12.6 index on Linux and Windows | Proposed |

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
