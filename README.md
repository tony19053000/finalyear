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
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Running the controller and agents in dev: ports, background services, health checks, troubleshooting |
| [`docs/adr/`](docs/adr/) | Architecture decision records |
| [`CLAUDE.md`](CLAUDE.md) | Operating guide for AI coding agents |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Task flow, commit convention, review checklist, evidence rules |

## Repository layout

One Python package (`src/meshforge/`) with several entry points (controller, agent, CLI),
plus a React dashboard, protobuf contracts, lab scripts, and tests. The planned layout is
described in [`docs/ARCHITECTURE.md` §21](docs/ARCHITECTURE.md#21-repository-structure);
most of those directories do not exist yet.

## Development quickstart

Needs Python 3.12 via [uv](https://docs.astral.sh/uv/) and Docker (for PostgreSQL tests).

```bash
uv sync --extra controller --extra agent   # install from uv.lock
uv run pre-commit install                  # commit-time checks
uv run pytest -m unit                      # unit tests (no Docker needed)
uv run pytest -m component                 # needs Docker
docker compose -f deploy/docker-compose.dev.yml up -d --wait   # dev database (see deploy/README.md)
```

Contributor and agent rules: [CONTRIBUTING.md](CONTRIBUTING.md).

## CI

<!-- Badge: add once the GitHub repo URL is known, e.g.
[![CI](https://github.com/<owner>/<repo>/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/<owner>/<repo>/actions/workflows/ci.yml) -->

GitHub Actions ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every push
to `main` and every pull request. Branch protection requires the single `ci-ok` check,
which passes only if all of these pass (run the same commands locally before pushing):

| Job | Command |
|---|---|
| lint | `uv run ruff check .` · `uv run ruff format --check .` · `uv run lint-imports` · `uv run python scripts/gen_protos.py --check` · pre-commit hygiene hooks |
| types | `uv sync --extra controller --extra agent` then `uv run mypy` |
| unit (Linux + Windows) | `uv sync` then `uv run pytest -m unit` |
| component (Linux, Docker) | `uv sync --extra controller --extra agent` then `uv run pytest -m component` |

CI installs with `uv sync --locked`, so a `uv.lock` that is out of date with
`pyproject.toml` fails the build.

## License

All rights reserved. A license has not yet been chosen; this repository is private.
