# ADR-0004: Execution runtimes: Docker first, uv venv fallback

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
Workloads must be isolated, reproducible and fast to start repeatedly, on Linux and Windows, with GPU access.

## Decision
A common **Runtime interface** with two implementations: **Docker** (default where available; CPU/RAM/pids limits, workdir-only mounts, non-root, GPU via NVIDIA Container Toolkit / Docker Desktop) and **venv via uv** (fallback; also used for the GPU service). Environments are identified by an **env hash** (base image + Python + resolved lockfile) and cached per node.

## Alternatives considered
Docker only (fails on nodes without Docker); venv only (weak isolation); Conda (slow, heavy); full VMs (too heavy).

## Trade-offs
+ strong isolation where possible, still runs everywhere. − venv isolation is weaker (documented); − Docker Desktop on Windows adds a layer.

## Consequences
Planner considers env cache state. Spike S6 validates Docker Desktop GPU on team Windows laptops.
