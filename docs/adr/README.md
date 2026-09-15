# Architecture Decision Records

Format: Context · Decision · Alternatives · Trade-offs · Consequences. Status values: Proposed → Accepted → Superseded.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-centralized-control-plane.md) | Centralized control plane | Proposed |
| [0002](0002-agent-initiated-grpc-session.md) | Agent-initiated bidirectional gRPC session | Proposed |
| [0003](0003-content-addressed-data-plane.md) | Content-addressed data plane over HTTP | Proposed |
| [0004](0004-execution-runtimes.md) | Execution runtimes: Docker first, uv venv fallback | Proposed |
| [0005](0005-gpu-remoting-at-pytorch-level.md) | GPU remoting at the PyTorch level with torch.export | Proposed |
| [0006](0006-work-queue-for-splitting.md) | Pull-based leased work queue for Task Splitting and Combined-GPU inference | Proposed |
| [0007](0007-planner-cost-based-ranking.md) | Planner = candidate generation + cost estimation + policy; ML predicts cost components | Proposed |
| [0008](0008-pairing-and-mtls.md) | Pairing with a fabric CA and mTLS | Proposed |
| [0009](0009-native-windows-agent.md) | Native Windows agent (not inside WSL) | Proposed |
| [0010](0010-monorepo-and-stack.md) | Monorepo, single Python package, and core stack | Proposed |
