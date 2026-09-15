# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-15

## Current Version
V0.1 — Foundations & engineering system · **in progress** (started 2026-09-15)

## Current Task
Closing V0.1 (session 1). Lead = main session; each task runs implementer → tester → reviewer; one local commit per task; agents never push (team pushes to private GitHub).

## Recently Completed
- Committed locally: 0.1.1 repo init (no license — team decision) · 0.1.2 pyproject/uv · 0.1.3 skeleton + entry points · 0.1.5 protos + deterministic codegen + FailureType codec · 0.1.6 `common/` (config, ULID ids, JSON logging with linear-time secret redaction, 22-value failure taxonomy, SHA-256) · 0.1.12 docs scaffold
- 0.1.4 import boundaries committed; stays [~] until a real GitHub CI run shows a boundary violation rejected
- 0.1.7 dev Postgres + SQLAlchemy base + Alembic baseline — tester PASS + reviewer APPROVE; commit pending (its tests were touched by 0.1.8)
- Team accepted ARCHITECTURE v1 and ADRs 0001–0010 (2026-09-15)

## In Progress
- 0.1.8 pytest tiers/markers + Postgres fixture — fix round done; re-test + re-review running
- 0.1.9 ruff/mypy/pre-commit — tester PASS, reviewer APPROVE (conditional on 0.1.8 green, now met); commit pending
- 0.1.10 GitHub Actions CI — written + review nits applied; local tester running; needs 🧑 real GitHub run
- 0.1.11 CONTRIBUTING.md — reviewer APPROVE (wording fixes applied); commit pending (shares README with 0.1.10)

## Blockers
- 🧑 Private GitHub repo + first push not done → V0.1 criteria 1 (Windows) and 2 (CI green, bad commit rejected) cannot be proven until then
- Laptop 1 NVIDIA driver blocked by Secure Boot (MOK not enrolled) — user must run `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, reboot, enroll
- Hardware of laptops 2–4 unknown; AI-agent access to Windows laptops undecided; Ethernet switch not purchased

## Known Bugs
- None open. (Found and fixed this session: secret leak + O(n²) in log scrubber; duplicate IDs after fork; plain-`uv sync` test collection crash; CRLF-dependent codegen output.)

## Technical Debt
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)
- Dev DB has a default password (`meshforge-dev`, DB bound to 127.0.0.1); product/non-dev must refuse to start without an explicit secret (V0.7/V0.17)
- `pytest.mark.unit` etc. not registered until 0.1.8 (warnings); ruff formats Python blocks inside docs/*.md until 0.1.9 excludes docs
- `.venv` on Laptop 1 has all extras incl. torch installed (reviewer ran `uv sync --all-extras`)

## Test Status
- Linux: `uv run pytest -q` → 611 passed, 1 skipped (AttemptState parity TODO), 2 xfailed (documented redaction scope gaps); ruff/mypy (strict, 68 files)/lint-imports/gen_protos --check clean
- Windows: not yet run (needs CI or a laptop)

## Next Recommended Task
Finish V0.1 commits (0.1.7–0.1.11), then 🧑 **GitHub push + CI proof** (only humans push):
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. In the project folder: `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions tab → run "CI" on `main`: expect lint, types, unit (ubuntu + windows), component, fresh-clone (ubuntu + windows), ci-ok all green. Paste back the run URL (and the Windows logs if red).
4. Settings → Branches/Rulesets for `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches, one PR each, commit with `--no-verify`: `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked. Paste back the 3 run URLs, then close PRs and delete branches.
After that the Lead marks 0.1.4 and 0.1.10 `[x]` and closes V0.1; then start **V0.2** (lab setup & spikes) in a new session.
