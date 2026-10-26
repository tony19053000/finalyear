# Changelog

All notable changes to MeshForge. Versions follow the roadmap
(`docs/DEVELOPMENT_ROADMAP.md`); the version number has one source,
`[project].version` in `pyproject.toml` (read at runtime as
`meshforge.__version__`). A release is the tag `v<version>`; the release
workflow refuses a tag that does not match it (Q36, D403).

Agents and the controller must share the same minor version, or the agent's may
be one minor behind (see `meshforge.common.versioning`).

## 0.17.0 - first release

The first published release. Before it the project carried the development
placeholder `0.1.0.dev0`, so no earlier release exists to upgrade from. It
contains the work of roadmap versions V0.1 to V0.17:

- **V0.1 Foundations** - uv monorepo, protobuf contracts and codegen, Alembic
  migrations, structured JSON logs, test tiers, CI on Linux and Windows.
- **V0.2 Lab** - lab inventory, measurement spikes and the feasibility record.
- **V0.3 Fabric core** - persistent node identity, bidirectional gRPC sessions,
  hardware discovery, resource streaming, failure detection and reconnection.
- **V0.4 Remote execution (Mode 1 foundation)** - jobs and attempts as explicit
  state machines, manifests, sandboxed Docker runs, live logs, cancellation,
  phase telemetry.
- **V0.5 Data plane** - content-addressed storage, have/need transfer,
  resumable and peer-to-peer fetch, environment reuse, cache GC.
- **V0.6 Scheduling and reliability** - Mode 1 Job Offloading complete:
  feasibility/cost/policy placement, fencing, crash recovery, failure taxonomy.
- **V0.7 Security** - fabric CA, one-time join tokens, mTLS everywhere,
  revocation, threat model.
- **V0.8 Dashboard and CLI** - React dashboard served by the controller, live
  updates, the `compute` CLI.
- **V0.9 Benchmarks and telemetry** - reproducible, labelled benchmark datasets
  from real executions.
- **V0.10 Mode 3 Task Splitting** - leased work queues, pull-based balancing,
  speculative execution for stragglers, deterministic combine.
- **V0.14 Unified planner** - one planner compares Local, Mode 1 and Mode 3.
- **V0.15 ML planner** - learned runtime predictions with uncertainty and a
  safe fallback to the analytical planner.
- **V0.17 Productization** - self-contained bundles with their own Python;
  `.deb` packages for the agent (Ubuntu 22.04/24.04) and controller (Ubuntu
  24.04) and a Windows installer for the agent; OS services; a first-run setup
  and local console with no terminal needed; upgrade, rollback and uninstall;
  agent/controller version checks; a tagged release workflow.

Not in this release: Mode 2 Remote GPU (V0.11), Mode 4 Combined GPU (V0.12,
V0.13) and model splitting (V0.16) are deferred by
[ADR-0012](adr/0012-scope-ship-mode3-complete.md). Real-machine lab steps that
are still open are listed in `docs/STATUS.md`.
