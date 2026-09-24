# ADR-0010: Monorepo, single Python package, and core stack

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
AI agents will write all code; the structure must be easy to navigate, with enforced boundaries.

## Decision
One repository, one Python package `meshforge` (`common`, `controller`, `agent`, `gpu_service`, `sdk`, `cli`) with several entry points, managed by **uv**; import boundaries enforced in CI; protobuf contracts in `proto/`; React dashboard in `dashboard/`. Stack: Python 3.12, gRPC, FastAPI, PostgreSQL + SQLAlchemy 2 + Alembic, structlog, psutil, NVML, Docker, PyTorch, scikit-learn/XGBoost/LightGBM, Typer/Rich, React/TS/Vite, pytest/testcontainers/Toxiproxy/Playwright.

## Alternatives considered
Multiple repositories; microservices per subsystem; Go/Rust agent.

## Trade-offs
+ simple for a small team and for agents; + shared types. − one package must stay disciplined (boundaries enforced).

## Consequences
Separate processes only at real runtime boundaries: controller, agent, GPU service, dashboard.

## Updates

- **2026-09-24 (V0.3 G6, D23):** fault injection uses an in-process asyncio TCP proxy (`tests/_support/mf_faultproxy.py`) with Toxiproxy's fault vocabulary instead of the Toxiproxy binary — no download or Docker host networking, so the cluster tier runs after a plain `uv sync`. Rationale in `docs/briefs/V0.3-fabric-core.md` §5f D23; ARCHITECTURE §22 updated.
