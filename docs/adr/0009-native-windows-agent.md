# ADR-0009: Native Windows agent (not inside WSL)

> **Status:** Proposed (2026-09-15) — pending team approval · **Deciders:** team + architect agent

## Context
Three of four lab laptops run Windows. WSL2 uses NAT by default (inbound connections from LAN fail unless mirrored networking is available), and end users should not need to understand WSL.

## Decision
The agent runs **natively on Windows** (Python, psutil, NVML, gRPC, PyTorch all support Windows). Workloads use Docker Desktop (Linux containers, GPU via WSL2 backend) when available, otherwise native venv. The GPU service runs natively with CUDA PyTorch.

## Alternatives considered
Agent inside WSL2 Ubuntu; Linux-only support.

## Trade-offs
+ no NAT problems; + installable as a Windows service; − Windows-specific code paths and CI needed.

## Consequences
CI runs unit tests on Windows; lab tests always include a Windows node. Spike S2 validates reachability.
