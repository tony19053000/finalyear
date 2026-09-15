# Contributing

How work gets done in this repository. All code is written by AI agents; humans decide,
approve, run physical lab tests, and push. This file is the **practical checklist**.
The rules behind it live in [`CLAUDE.md`](CLAUDE.md) (workflow §4, agent team §4b,
session protocol §4c, definition of done §6, code quality §7, security §9) and
[`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md#how-agents-use-this-roadmap).
Where they differ, those files win. Fix this file to match them.

---

## 1. Who does what

| Role | Does | Never |
|---|---|---|
| **Lead** (main session) | picks the task, checks the design against ARCHITECTURE/ADRs, delegates, integrates, commits, **marks `[x]`**, updates `STATUS.md` | skips tester/reviewer for shipped code |
| **implementer** ([`.claude/agents/implementer.md`](.claude/agents/implementer.md)) | code + unit/component tests for **one** task; may set that task to `[~]` | marks `[x]`, approves own work, pushes |
| **tester** ([`.claude/agents/tester.md`](.claude/agents/tester.md)) | tests derived from the success criteria; edge cases, failure injection; writes 🧑 lab checklists in `lab/` | edits `src/`, weakens or deletes tests |
| **reviewer** ([`.claude/agents/reviewer.md`](.claude/agents/reviewer.md)) | read-only review; APPROVE / REQUEST_CHANGES | edits files |
| **Humans** 🧑 | approve architecture/ADRs, set up hardware, run lab steps, paste raw output, push to GitHub | |

```
Lead picks task → implementer → tester ─FAIL→ implementer
                                  │PASS
                                  ▼
                               reviewer ─REQUEST_CHANGES→ implementer
                                  │APPROVE
                                  ▼
              🧑 lab step if needed → Lead marks [x] with evidence, updates STATUS.md
```

Docs-only changes may skip the tester (CLAUDE.md §4b). They still need a reviewer.

## 2. Task brief (Lead → implementer)

```
TASK: <id + title from the roadmap, e.g. 0.3.7 liveness leases>
READ: <roadmap section, ARCHITECTURE §§, ADR ids>
SCOPE: <what to build, what NOT to touch (files other agents are editing)>
SUCCESS CRITERIA: <copied from the roadmap>
PREVIOUS REPORTS: <tester/reviewer findings to address, if this is a fix round>
COMMIT: yes/no
```

## 3. Handoff and report formats

Copy these **exactly**. The source of truth is the agent file linked in each heading.

**Implementer handoff** ([implementer.md](.claude/agents/implementer.md#handoff-note-your-final-message))
```
TASK: <id + title>
CHANGED FILES: <list>
WHAT IT DOES: <3–6 lines>
DESIGN NOTES / DEVIATIONS: <anything that differs from ARCHITECTURE/ADRs, and why>
TESTS ADDED: <list>
COMMANDS RUN + RESULTS: <exact commands, pass/fail counts>
NEEDS HUMAN LAB STEP (🧑): <exact laptops, commands, expected output, what to paste back — or "none">
KNOWN LIMITATIONS / FOLLOW-UPS: <list>
```

**Tester report** ([tester.md](.claude/agents/tester.md#report-your-final-message))
```
TASK: <id + title>
VERDICT: PASS | FAIL | BLOCKED
CRITERIA COVERAGE: <criterion → test(s) → pass/fail>
DEFECTS: <id, severity (critical/major/minor), reproduction steps, expected vs actual>
FLAKY TESTS: <list or none>
COMMANDS RUN + RESULTS: <exact commands, counts>
LAB STEPS REQUIRED (🧑): <path to lab script/checklist, or none>
```

**Reviewer report** ([reviewer.md](.claude/agents/reviewer.md#report-your-final-message))
```
TASK: <id + title>
VERDICT: APPROVE | REQUEST_CHANGES
BLOCKING: <numbered findings: file:line — problem — failure scenario — fix>
NON-BLOCKING: <numbered suggestions>
ARCHITECTURE DRIFT: <none, or what differs and whether an ADR is needed>
VERIFIED BY RUNNING: <commands + results>
```

## 4. Commits

[Conventional Commits](https://www.conventionalcommits.org/) with the **roadmap task id as the scope**:

| Type | Use for | Example |
|---|---|---|
| `feat` | new behaviour | `feat(0.1.3): package skeleton and entry points` |
| `fix` | bug fix | `fix(0.1.6): redact SecretStr inside nested models` |
| `test` | tests only | `test(0.1.4): import-linter contracts + completeness guard` |
| `docs` | docs only | `docs(0.1.12): ADR guide and LAB.md` |
| `build` | packaging, deps, lockfile | `build(0.1.2): pyproject with uv` |
| `ci` | GitHub Actions | `ci(0.1.10): lint and unit jobs on Linux + Windows` |
| `chore` | housekeeping with no task | `chore: bump uv.lock` |

- **One commit per completed task.** Small follow-up fix commits with the same scope are fine.
- Subject: imperative, lower-case, no trailing period, about 72 characters or fewer.
- Agent commits end with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Agents commit **locally only, and only when the Lead asks**. Agents **never push**.
  Humans push to the private GitHub repository.
- Never commit secrets, `.env` files, keys, or lab output with passwords/tokens in it.
- No license file yet (team decision, 0.1.1). Do not add one.

## 5. Local checks

One-time setup (uv must be on `PATH` in the shell git uses, also on Windows):

```bash
uv sync --extra controller --extra agent   # dev group + extras mypy needs
uv run pre-commit install                  # hooks run on every commit
```

Before handing off, run what CI runs (`.github/workflows/ci.yml`, required check `ci-ok`):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy                                  # strict; config in pyproject.toml
uv run lint-imports                          # module-boundary contracts
uv run python scripts/gen_protos.py --check  # generated protos up to date
uv run pytest -m unit                        # CI: Linux + Windows, plain `uv sync` (run once
                                             # in a plain-sync env to catch missing importorskip)
uv run pytest -m component                   # CI: Linux; needs Docker
uv run pre-commit run --all-files            # all of the above except pytest, plus hygiene
```

CI uses `uv sync --locked`: a stale `uv.lock` fails the build. A plain `git commit` does
not catch hand edits to generated code in `src/meshforge/common/proto/` — never edit it;
`gen_protos.py --check` in CI does catch it.

Report the exact commands and pass/fail counts in your handoff. If a command cannot run
(no Docker, no GPU), say so. Never claim it passed.

**Test tiers = folders** under `tests/`: `unit` · `component` · `cluster` · `lab` 🧑 ·
`e2e` · `soak` · `chaos` (auto-marked; a test outside a tier folder fails collection).
**Capability markers** any tier may add: `docker` (skips if the Docker daemon or the `docker` package is missing), `gpu` (runs only
with `--run-gpu` and CUDA), `slow` (runs only with `--run-slow`). `lab`, `soak`, `chaos` run only with `--run-<tier>`. Warnings are
errors. A test module that needs an extra (`sqlalchemy`, `alembic`, `torch`, …) must call
`pytest.importorskip(...)` **before** importing it, or a plain `uv sync` run breaks. Postgres
tests use the shared `pg_settings` fixture (fresh database per test). See the roadmap's
[quality gates](docs/DEVELOPMENT_ROADMAP.md#quality-gates-apply-to-every-version).

## 6. Contracts and schema changes

- **Protobuf:** edit `proto/meshforge/v1/*.proto` first, then run
  `uv run python scripts/gen_protos.py` and **commit the generated code** in the same commit.
  CI checks it with `--check` (regenerating must produce no diff).
- **`FailureType` is permanent.** Values are stored in the DB and sent on the wire: add
  members, never rename or reuse them. The Python enum (`meshforge.common.errors`) and the
  proto enum are mirrored 1:1, and `tests/unit/test_proto_enum_parity.py` enforces it.
  Add the member in both places.
- **Database:** changes only through Alembic revisions in
  `src/meshforge/controller/store/migrations/versions/`, with sequential ids
  (`0001_baseline`, `0002_<slug>`, …). Every constraint and index must be named (the metadata
  `naming_convention` in `store/base.py` does this). Provide a working `downgrade()`, and
  test upgrade → downgrade → upgrade on real PostgreSQL.
- A non-additive contract change needs an ADR (§9).

## 7. Module boundaries

Rules are in [ARCHITECTURE §21](docs/ARCHITECTURE.md#21-repository-structure): `common` is
the bottom layer, `controller` and `agent` never import each other, `sdk` and `gpu_service`
import only `common`, and `cli` imports `common` + `sdk`.

Adding a **new top-level subpackage** under `src/meshforge/` means updating both:
1. the import-linter contracts in `pyproject.toml` (`[tool.importlinter]`): a `forbidden`
   contract for the new package, and add it to the other contracts' `forbidden_modules`
   unless it is allowed;
2. `ALLOWED_DEPS` in `tests/unit/_import_guard.py`.

`pytest -m unit` is the gate. `lint-imports` alone does not check completeness.
**Dynamic imports** of `meshforge` subpackages (`importlib.import_module`, `__import__`)
are invisible to these checks, so they are forbidden unless the reviewer explicitly signs off.

## 8. Logging

- Use `meshforge.common.logging` only (`configure_logging`, `get_logger`, `log_context`).
  Do not use `print` or build a separate structlog config.
- Log events as short dotted names (`attempt.started`) and put the data in fields.
- Bind the correlation fields that apply: `job_id`, `attempt_id`, `node_id`, `session_id`.
- Prefer `with log_context(...)` because it restores the previous values on exit. **Inside
  thread pools always use `log_context`, not `bind_context`**, which leaks the fields into
  the next job on that worker thread.
- **Never log secrets.** Redaction catches sensitive keys and `SecretStr`/`SecretBytes`
  values, but free-text scrubbing only catches known shapes (URL credentials, `Bearer …`,
  `key=value`). Keep secrets in `SecretStr` fields and never interpolate them into
  messages or exception text.

## 9. When an ADR is required

Anything hard to reverse, or that later sessions must not re-argue: a new or changed
technology, protocol, or transport; a module or process boundary; a non-additive contract
change; a security-model choice; any decision a spike forces. Follow
[`docs/adr/README.md`](docs/adr/README.md). Agents create ADRs as **Proposed** only, and the
team accepts them. Add a row to the roadmap design change log.

## 10. Review checklist

This merges the reviewer checklist with CLAUDE.md §6. Tick every item that applies.

```
[ ] scope: exactly the assigned task, no unrelated changes
[ ] correctness: meets every success criterion; state transitions valid
[ ] architecture: matches ARCHITECTURE + ADRs; boundaries respected; planning ≠ execution; no duplicated shared logic
[ ] failure paths: explicit states, typed failure_type, timeouts on every network call, retries/idempotency
[ ] concurrency/resources: no races, leaks (tasks, sockets, files, VRAM), or blocking calls in async code
[ ] security: authn/authz, input validation, no path traversal, no pickle of untrusted data, no secrets in logs
[ ] performance: no needless copies of large data, N+1 queries, or chatty network patterns
[ ] cross-platform: Windows paths, line endings, signals, services
[ ] imports/build pass; lint + format + mypy + lint-imports + proto --check clean
[ ] unit tests pass; component/cluster tests use real dependencies where relevant
[ ] tests prove the criteria (failure paths too); no weakened assertions or over-mocking
[ ] telemetry (phase timings) recorded for new execution paths (ARCHITECTURE §17)
[ ] logs verified: correlation IDs present, no secrets
[ ] real-machine 🧑 test passed where required; report in lab/reports/
[ ] docs updated: ARCHITECTURE, ADRs, STATUS, user docs
```

## 11. Evidence rules

- `[x]` needs **evidence**: test output (commands + counts), a CI run link, or a lab report
  path (`lab/reports/…`). Code existing is not done. No evidence means not done.
- Only the Lead marks `[x]`, after tester PASS and reviewer APPROVE. The evidence goes on the
  roadmap line (see the finished V0.1 tasks for the format).
- `[~]` means in progress, or partly proven (for example, "stays `[~]` until the CI job runs
  it"). Say what is missing.
- Changes to a success threshold get logged in the design change log.

## 12. Writing 🧑 lab steps

Follow [`docs/LAB.md` §5](docs/LAB.md#5-lab-procedures--human-steps). Every step states
**which laptops**, the **exact commands** per OS (bash / PowerShell), the **expected
output**, and **what to paste back**. Humans record `git rev-parse --short HEAD`. Raw output
goes to `lab/reports/raw/YYYY-MM-DD-<topic>/<hostname>.txt`. The agent writes the report
from [`lab/reports/TEMPLATE.md`](lab/reports/TEMPLATE.md). Also follow the
[safety notes](docs/LAB.md#6-safety-notes).

## 13. Windows notes

- Line endings are LF through [`.gitattributes`](.gitattributes); Windows script files (`.ps1/.psm1/.psd1/.bat/.cmd`) are CRLF.
  Set `git config core.autocrlf false` (or `input`). Never use `true`.
- Write paths in TOML with forward slashes (`C:/meshforge/data`). Backslashes are escape
  characters in TOML basic strings.
- Code uses `pathlib`, never hard-coded `/` or `\` separators.
- The MeshForge **agent runs natively on Windows, not inside WSL** (ADR-0009). WSL2 is only
  a developer convenience.
