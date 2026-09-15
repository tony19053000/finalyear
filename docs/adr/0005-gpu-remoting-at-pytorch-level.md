# ADR-0005: GPU remoting at the PyTorch level with torch.export

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
Mode 2 (Remote GPU) and Mode 4 (Combined GPU) need real GPU computation on other laptops. Transparent CUDA-API interception (rCUDA, Cricket, Juice Labs) is research/company-scale work and suffers from per-call network latency.

## Decision
Remote GPU is implemented at the **PyTorch level**: a separate **GPU service** process per GPU laptop hosts **sessions**, **remote tensor handles** and **modules shipped as `torch.export` ExportedPrograms** (no pickled code). Tensors travel as raw bytes + dtype/shape over gRPC streams (raw TCP only if S3 demands). Weights stay resident in remote VRAM; only inputs/outputs cross the network; calls are pipelined. Transparency ladder: L1 ops → L2 handles → L3 RemoteModule → L4 `torch.compile` backend (stretch). L5 CUDA interception is out of scope.

## Alternatives considered
CUDA API forwarding; per-op RPC via `__torch_dispatch__` only; shipping whole jobs (that is Mode 1).

## Trade-offs
+ achievable, safe, real GPU compute, efficient because state is resident. − only PyTorch workloads; − some models may not export (fallback: registered architectures + state_dict).

## Consequences
Mode 4 builds on the same service (replication, work queue, collectives). Spike S4 validates export/load across laptops.
