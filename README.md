# MeshForge

> Working title. The final project name is not yet decided.

**An intelligent distributed compute fabric for heterogeneous personal devices.**

MeshForge turns a group of ordinary laptops on the same network into one shared pool of
compute. For each workload it answers one question:

> *Given this workload and the machines currently available, what is the best practical way to run it?*

A single planner chooses between five ways of running a workload:

| Mode | What happens |
|---|---|
| **Local** | Runs on your own machine (always the baseline for comparison). |
| **Mode 1 · Job Offloading** | The whole job runs on another laptop; only the output comes back. |
| **Mode 2 · Remote GPU** | Your program stays on your laptop; its GPU work runs on another laptop's GPU. |
| **Mode 3 · Task Splitting** | The job is split into sub-tasks that run on several laptops; results are combined. |
| **Mode 4 · Combined GPU** | The GPUs of several laptops act together for a PyTorch program running on yours. |

The planner starts rule-based and later uses machine learning trained on recorded runs to
predict execution times and pick the mode, the machines, and how much work each one gets.

## Status

**Pre-alpha, version V0.1 (foundations).** The repository is being set up: tooling,
contracts, and conventions. **None of the execution modes are implemented yet.**
See [`docs/STATUS.md`](docs/STATUS.md) for the current state and
[`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) for the plan.

## Documentation

| Document | Purpose |
|---|---|
| [`docs/PROJECT_BRIEF.md`](docs/PROJECT_BRIEF.md) | Product and engineering vision |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Current architecture |
| [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) | Versioned plan and progress checklist |
| [`docs/STATUS.md`](docs/STATUS.md) | Concise current state |
| [`docs/adr/`](docs/adr/) | Architecture decision records |
| [`CLAUDE.md`](CLAUDE.md) | Operating guide for AI coding agents |

## Repository layout

One Python package (`src/meshforge/`) with several entry points (controller, agent, CLI),
plus a React dashboard, protobuf contracts, lab scripts, and tests. The planned layout is
described in [`docs/ARCHITECTURE.md` §21](docs/ARCHITECTURE.md#21-repository-structure);
most of those directories do not exist yet.

## Development quickstart

*Available after V0.1.* The intended workflow (Python 3.12, [uv](https://docs.astral.sh/uv/),
Docker for PostgreSQL) will be:

```bash
uv sync                 # install dependencies from uv.lock
uv run pytest -m unit   # run unit tests
```

These commands do not work yet — `pyproject.toml` is added in task 0.1.2.

## License

All rights reserved. A license has not yet been chosen; this repository is private.
