# Changelog

All notable changes to MeshForge. Versions follow the roadmap
(`docs/DEVELOPMENT_ROADMAP.md`); the version number has one source,
`[project].version` in `pyproject.toml` (read at runtime as
`meshforge.__version__`). A release is the tag `v<version>`; the release
workflow refuses a tag that does not match it (Q36, D403).

Agents and the controller must share the same minor version, or the agent's may
be one minor behind (see `meshforge.common.versioning`).

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) (D421): one `## [version] - date`
section per release (`## [Unreleased]` until it is tagged) with the change types *Added*, *Changed*,
*Deprecated*, *Removed*, *Fixed*, *Security*, plus three of this project's release notes: *Deferred* (designed,
not built), *Known issues* and *Accepted risks*. Inside a release, each roadmap version has a `#### V0.x` entry.
How an entry is made: `uv run python scripts/changelog.py draft V0.x` prints a draft from the roadmap's tasks and
the version's decision log; a person (or agent) edits it into this file by hand; `scripts/changelog.py check`
(run by the unit tests) fails when a roadmap version has no entry or a deferred version does not say so.

## [Unreleased]

Release candidate for **1.0.0**. The tag `v1.0.0` is made only after `scripts/release_check.py` exits 0 on the
release build, the 72 h soak has passed and its report is merged (D421, ADR-0023); until then this section is a
draft.

**What 1.0.0 ships:** Local execution, **Mode 1 Job Offloading** and **Mode 3 Task Splitting**, chosen by one
planner (rules, the calibrated v2 cost model and the ML hybrid), on a secured fabric (fabric CA, mTLS, user
tokens, owner policy), with the dashboard, the `compute` CLI, telemetry and benchmark campaigns, and installers
for Linux and Windows.

### Added

#### V1.0 - Release hardening & final evaluation

- One acceptance manifest (`release/acceptance.yaml`) with a row for every success criterion of V0.1-V0.17, a
  roadmap-coverage test, and `scripts/release_check.py`, which builds the acceptance report from it on the
  release build (G0, ADR-0023, D406-D408).
- Soak and chaos kit for the 72 h campaign: seeded load generator, resource samplers, chaos driver for Linux
  and Windows laptops, an analyser for the D410 rules, rehearsed in a dry run on a local multi-agent fabric (G1).
- Reproducible evaluation pipeline (`scripts/eval/`): analysis and report with figures, byte-identical twice,
  checked by the CI job `eval-repro` on a committed sample (G2, D419).
- Generated API reference in `docs/api/` - REST (OpenAPI), gRPC (from `proto/`), CLI (from `--help`), SDK -
  with the CI job `api-docs` and a pre-commit hook that fail on drift (G5, D414).
- Demo scenarios `scripts/demo/01-local` ... `05-install-join`, each with a reset and a `--dry-run` that CI runs
  against a real local fabric; no demo for Mode 2/4 (G5, D420).
- Changelog tooling (`scripts/changelog.py`), the ADR index with every ADR's status, ARCHITECTURE sections
  marked current or deferred (G5, D414, D421).
- The packaged controller trains the ML planner's models: the `meshforge-controller` bundle and `.deb` carry
  the `ml` extra (scikit-learn, LightGBM, XGBoost), the package depends on `libgomp1`, and the `.deb` stays
  inside the 150 MB budget (unused pyarrow Flight/S3 parts and library test suites are not shipped; the data
  member is recompressed with liblzma) (G7, D423).
- `scripts/release_check.py` refuses to run the automatic rows in an environment without every extra or
  without the built dashboard, and says how to fix it (G7).

### Fixed

- The packaged controller reads its Parquet datasets on a Linux without the system time-zone database: the
  controller takes the `tzdata` package on every OS, not only Windows (G7).
- A controller whose own event loop stalled (GC pause, starved machine) no longer marks live nodes OFFLINE:
  the lease timer listens for one heartbeat interval after a late wake-up before it judges a lease (G7, D424).

### Deferred

- **Mode 2 Remote GPU** (V0.11), **Mode 4 Combined GPU** inference and training (V0.12, V0.13) and **model
  splitting** (V0.16): designed, not built in 1.0 ([ADR-0012](adr/0012-scope-ship-mode3-complete.md)). The
  `gpu.proto` contract is a skeleton, `meshforge.sdk` has no public names, and the planner answers a GPU
  request `not_supported_in_build`. Nothing of them is simulated.

### Known issues

- Real-machine lab steps still open at the time of writing are listed in `docs/STATUS.md` (each roadmap
  version's 🧑 task); the release report names each one as passed or waived.
- Some tests are load-sensitive and can fail under a heavily loaded machine while passing alone (listed under
  "Known Bugs" in `docs/STATUS.md`).

### Accepted risks

- Windows jobs without Docker run unsandboxed, and only when the laptop's owner opted in (ADR-0016).
- Evaluation numbers come from four laptops on a LAN; Wi-Fi noise and the chosen workload families limit how far
  they generalise (stated in the evaluation report).
- ADRs still marked *Proposed* need the team's acceptance before the tag
  ([adr/README.md](adr/README.md#needs-team-acceptance-before-v100)).

## [0.17.4] - 2026-11-12

Lab fix release (lab fix group G12, `docs/briefs/progress/V1.0-G12-planner.md`).

### Changed

- A new workload's split lanes use the node's learned chunk cost from recent chunks of any workload with the same
  runtime kind, instead of the shared ~0 overhead; `compute explain` labels it `node_history <runtime> n=..` (D459).
- Mode hysteresis: the planner keeps a workload's previous mode unless the new best is at least
  `[controller.planner] mode_switch_margin` (default 0.10, 0 = off) faster; explain shows `kept previous mode` (D460).

### Fixed

- Demo 03 and demo 04 job C use 4M rounds per item so they still split once the workload has history.

## [0.17.3] - 2026-11-12

Lab fix release (lab fix group G11, `docs/briefs/progress/V1.0-G11-lab-fixes.md`, `V1.0-G11-D457.md`,
`V1.0-G11-D458.md`).

### Added

- `compute data gc [--dry-run] [--json]` (admin) and `POST /v1/data/gc`: free every unreferenced input blob now
  (D454).
- `compute run --node` accepts a node name or hostname (D452).

### Changed

- The planner learns a per-node fixed chunk cost and a lane's first-chunk cost from `chunk_record`, so slow-start
  nodes (Windows venv lanes) get fewer, larger chunks or none, and the prediction is closer to the real time
  (D456).

### Fixed

- `--exclude-node <name>` no longer matches revoked nodes with the same name (D452).
- `compute jobs <id>` and `run -f` show the last attempt's failure cause (e.g. `node_lost: node_revoked`) (D453).
- `compute data rm` frees the removed input's blobs at once and prints the bytes freed (D454).
- `compute run --local` works with the packaged (system) agent: the agent publishes a public `node-id.json`
  (`/var/lib/meshforge-public`, `%ProgramData%\MeshForge\public`); the private identity stays locked (D457).
- Cached venvs no longer keep the old bundled Python after an agent upgrade: the env key names the base
  interpreter, and the agent GC removes venvs of old bundles (D458; every upgrade rebuilds venvs once).

### Deferred

- Warm lane worker and per-chunk phase times (ADR-0024, future work).

## [0.17.2] - 2026-11-11

Lab fix release from the real-laptop campaign (lab fix group G10, `docs/briefs/progress/V1.0-G10-lab-fixes.md`).

### Fixed

- Docker env builds no longer fail with ENOSPC after the torch download; a full disk is reported as such (D442, D450).
- A failed env build is not retried for every chunk of a lane (D443); chunk `env_error` and a Docker daemon blip
  have backstops (D448, D449).
- Lane lease and test-isolation fixes from L3 under memory pressure (D444); a lane keeps its item list through
  the staging retry after a stream drop instead of ending `failed unauthorized` (D447).
- Lanes end cleanly when the queue is drained (D446) and survive a short reconnect within a grace period (D445).
- A hung chunk no longer holds its lease for many minutes (BUG-4, D451): Windows venv chunks start one
  interpreter instead of launcher + interpreter; the agent never shows invisible error dialogs; the agent watches
  each chunk's CPU and output, reports `stalled_s` on lease renewals, and the controller gives a stalled chunk's
  copy to another node first (`chunk_record.stalled_s`, migration 0038).

## [0.17.1] - 2026-11-06

Lab fix release from the real-laptop campaign (lab fix group G9, `docs/briefs/progress/V1.0-G9-lab-fixes.md`).

### Added

- `compute run --exclude-node <id|name>` (repeatable): placement and retries never use the named nodes (G9-B).
- `[agent.envs] extra_index_urls` (up to 4): extra package indexes for venv and Docker builds, e.g. CUDA PyTorch
  wheels on Windows GPU laptops without Docker (G9-B, D436).
- `meshforge-agent doctor cpu --seconds N [--job-cpus C] [--json]`: live CPU samples and the owner's CPU cap (G9-C).
- Manifest hint `split.item_seconds`: a declared per-item time lets a long split job split on its first run
  (G9-E, D438).

### Changed

- A split is no longer chosen on assumed per-item rates only (`[controller.split] require_measured_rates`,
  default true): a new workload's first run runs whole unless it declares `split.item_seconds`; recorded chunk
  times now feed the split planner (G9-E, D438).
- A split chunk that hits `limits.timeout` is retried once on another node, never on the same one; with no other
  node it fails once with a clear message (G9-D, D437).

### Fixed

- Big input fetches on Wi-Fi: staging retries inside the attempt and bulk traffic is marked low priority, so
  heartbeats are not starved (G9-A, D429-D431).
- `cuda-pytorch` is only placed where Docker or an agent PyTorch exists (typed `no_runtime_for_base`) (G9-B, D432).
- A failed attempt's type and detail (e.g. `node_revoked`) are shown in `compute jobs` and `compute run -f` (G9-B).
- An `env_known_bad` mark is cleared when the owner changes the env policy (G9-B, D433); fewer repeated
  `clock_uncertain` alerts (D434).
- Windows live CPU % under load was 0 or noise (psutil per-thread baseline); a failed read is unknown, never 0 %
  (G9-C, D435).
- A long single chunk was restarted from zero at each per-chunk time limit and never finished (G9-D, D437).
- The controller retries a PostgreSQL deadlock/serialization conflict in the split store (G9-D2, D440).
- `compute --install-completion` works in PowerShell through the Windows `.cmd` launcher; cmd.exe gets a clear
  message (G9-F, D439).
- The "Cancel job" button is hidden for finished jobs; the log view says "following the combine step", not
  "retried" (G9).

## [0.17.0] - 2026-10-27

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

### Added

#### V0.1 - Foundations & engineering system

- uv project with the `controller`, `agent`, `gpu` (later `ml`) extras; one package `meshforge` with the
  entry points `compute`, `meshforge-controller`, `meshforge-agent`.
- `proto/meshforge/v1/*.proto` and deterministic codegen (`scripts/gen_protos.py --check`).
- Shared `common/`: settings (TOML + env), ULID ids, structlog JSON logs with correlation ids, errors, hashing.
- PostgreSQL 16 dev compose, SQLAlchemy base, Alembic migrations; pytest tiers and markers; ruff, strict mypy,
  import-boundary contracts, pre-commit; GitHub Actions on Linux and Windows; `CONTRIBUTING.md`; docs scaffold.

#### V0.2 - Lab setup & feasibility validation

- `lab/inventory.py` and the spike tools S1-S3, S6, S7 (network matrix, cross-OS reachability, transport
  throughput, environments, hardware scores) with their report templates; spike reports never overwrite
  hand-written prose (0.2.13).
- Deferred with Mode 2/4 (ADR-0012): S4 `torch.export` round-trip and S5 cross-OS collectives. The lab runs
  themselves are open 🧑 steps.

#### V0.3 - Fabric core: controller ⇄ agent

- Persistent node identity (ECDSA P-256 key + ULID `node_id`, ADR-0013); one agent-initiated bidirectional gRPC
  session per node with session epochs, heartbeats, leases and ONLINE/SUSPECT/OFFLINE liveness (ADR-0002).
- Hardware discovery (CPU, RAM, disks, OS, NVIDIA GPUs via NVML), measured hardware scores, resource streaming
  with history, RTT on heartbeats, mDNS discovery, reconnect with backoff and Reconcile.
- `GET /v1/nodes`, `compute nodes` / `status`; the cluster test harness (controller + N agents as processes,
  fault proxy). Decisions D1-D39.

#### V0.4 - Remote execution core (Mode 1 foundation)

- `compute.yaml` manifests, deterministic source bundles, jobs and attempts as enforced state machines,
  `POST /v1/jobs` with idempotency keys, `--node` / `--local` placement.
- Agent executor with a Docker runtime (limits, non-root, workdir-only mount) and a subprocess fallback; GPU
  pass-through; live logs over WebSocket; outputs collected and downloadable; cancel and timeouts; phase
  timings per attempt; `compute run / jobs / logs -f / cancel / get`; example workloads. Decisions D40-D78.

#### V0.5 - Data plane & environments

- Content-addressed store on controller and agents, tree manifests with have/need upload, peer blob servers,
  parallel resumable chunked fetch, cache location index, named versioned inputs.
- Environment builder (per-hash Docker images, uv venvs), base images and `compute node prepare`, LRU cache GC
  with pins, transfer telemetry. Decisions D79-D101.

#### V0.6 - Scheduling & reliability — Mode 1 complete

- Per-node CPU/GPU slots and a priority queue; planner v1 (Local + Mode 1 candidates, feasibility, seven
  policies, `cost_model` default), the analytical estimator and stored decision records with
  `compute explain`.
- Peer network probes; node loss -> attempt LOST -> retry by failure type with fencing tokens (ADR-0015);
  controller crash recovery without duplicate runs; at-least-once commands with agent dedupe; the failure
  taxonomy; the chaos suite. Decisions D102-D132.

#### V0.7 - Security & pairing

- Threat model; fabric CA (`fabric create`), single-use join codes, CSR enrolment; mTLS on the agent session and
  peer blob servers with scoped short-lived tokens; REST users and API tokens; revocation and certificate
  rotation; owner policy per node; sandbox hardening; job secrets redacted from logs; the security test suite
  (ADR-0016, superseding ADR-0014). Decisions D133-D168. See also *Security* below.

#### V0.8 - Dashboard & CLI v1

- React + TypeScript dashboard served by the controller (overview, nodes, jobs with the plan explanation,
  logs, artefacts and browser submit, settings with invite codes and QR), typed from the OpenAPI document, live
  over WebSocket; CLI polish (`--json`, completion, exit codes); Playwright e2e suite (ADR-0017). Decisions
  D169-D210.

#### V0.9 - Benchmark & telemetry system v1

- Telemetry schema v2 with Parquet export; the benchmark workload library; campaign specs with samplers;
  forced strategies; condition control; automatic labels; the unattended, resumable campaign runner with the
  owner guard; the data quality report; the Benchmarks page (ADR-0018). Decisions D211-D258.

#### V0.10 - Mode 3 Task Splitting

- `split:` manifests (glob, list, range, lines; chunking; concat / merge-csv / merge-json / script combine);
  the shared leased work queue; Mode 3 candidates with the "don't split" margin test; lanes that prepare once
  and run many chunks; speculative copies for stragglers; lost-node lease recovery; ordered deterministic
  combine; per-chunk progress in the CLI and dashboard; Mode 3 benchmark families (ADR-0019). Decisions
  D259-D289, D304.

#### V0.11 - Mode 2 Remote GPU

- Deferred (ADR-0012): designed, not built in this release.

#### V0.12 - Mode 4 Combined GPU — inference

- Deferred (ADR-0012): designed, not built in this release.

#### V0.13 - Mode 4 Combined GPU — training

- Deferred (ADR-0012): designed, not built in this release.

#### V0.14 - Unified planner v2

- One candidate generator for Local, Mode 1 and Mode 3 sets, with pruning; analytical costs calibrated from
  telemetry; a reliability risk term; objectives `min_time`, `min_network`, `prefer_local`; replayable
  decisions; the decision page (predicted vs actual); the offline evaluator (regret vs the best measured plan)
  (ADR-0020). Decisions D290-D303, D305-D332.

#### V0.15 - ML planner

- Training dataset from telemetry with family-grouped splits; features; residual targets; model comparison
  (ridge, kNN, trees, forest, LightGBM, XGBoost) with conformal uncertainty and OOD fallback to the analytical
  model; planner integration with the predictor version on each decision; model registry with gated
  activation, pinning and retraining; evaluation report; dashboard views; the classifier experiment (report
  only) (ADR-0021). Decisions D333-D383.

#### V0.16 - Model splitting (Mode 4 stage 3)

- Deferred (ADR-0012): designed, not built in this release.

#### V0.17 - Productization

- Self-contained bundles with their own Python; `.deb` packages (agent and controller) with systemd services;
  the Windows installer with the agent as a Windows service and firewall rules; managed PostgreSQL for the
  controller; first-run setup and the agent's local console (join by code or QR, settings); upgrade, rollback
  and clean uninstall; user documentation (`docs/user/`) (ADR-0022). Decisions D384-D405.

### Security

- Mutual TLS on the agent session and peer data plane, per-user API tokens on REST, owner policy on each
  laptop (V0.7, [ADR-0016](adr/0016-v07-security-design.md), superseding the interim trust of ADR-0014). Join
  codes, tokens and keys are scrubbed from logs and error lines. Threat model: `docs/THREAT_MODEL.md`; the
  final security review of the release build is roadmap task 1.0.4.

### Deferred

- Mode 2 Remote GPU (V0.11), Mode 4 Combined GPU (V0.12, V0.13) and model splitting (V0.16), by
  [ADR-0012](adr/0012-scope-ship-mode3-complete.md).
