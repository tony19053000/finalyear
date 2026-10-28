# MeshForge V1.0 - release security review

Roadmap task **1.0.4** (V1.0 G3), decision **D413** (`docs/briefs/V1.0-release.md`). Date: 2026-10-28.
Method: checklist-driven review by the reviewer agent, fixes by the implementer agent, every fix backed by a test
or a CI gate. This is not an outside penetration test (Q51).

**Result: 0 untriaged critical/high findings.** 1 high, 5 medium and 2 low findings were raised; all are fixed.
The accepted risks that remain are the ones listed in `docs/THREAT_MODEL.md` §7 (summary at the end).

## 1. What was checked (D413 checklist)

| Check | Tool / test | Result (2026-10-28) |
|---|---|---|
| Every THREAT_MODEL row against the release code | `uv run python scripts/threat_model_check.py` | 123 rows: 108 mitigated, 15 accepted, **0 failing**; 469 test references, all collected under `-m security` |
| Python dependency audit | `uv export --all-extras --all-groups --no-hashes --no-emit-project --frozen` then `uvx pip-audit -r <file> --no-deps --disable-pip` (pip-audit 2.10.1) | **No known vulnerabilities** (after H1/M1/L2) |
| Frontend dependency audit | `npm audit` and `npm audit --omit=dev` in `frontend/` | **0 vulnerabilities** |
| Secrets in release packages | `scripts/package_secret_scan.py` on agent + controller bundle `.tar.xz` and `.deb` | **0 hits** in all four; the builders now refuse a hit (M3) |
| Open ports of an installed controller vs D389 | `tests/component/packaging/test_v10_g3_open_ports.py` (real `.deb`, systemd container, ufw active) | non-loopback listeners = exactly {47100, 47101, 47110}; 3 passed |
| mTLS / token on every endpoint | `tests/unit/controller/test_v10_g3_route_inventory.py`, `tests/component/controller/test_v10_g3_listener_auth.py` | every route and every LAN listener refuses missing/wrong credentials; passed |
| Static analysis | `bandit -r src -ll` through `scripts/bandit_gate.py` | 16 medium/high findings, **0 untriaged**, 0 stale |

## 2. Findings

| ID | Sev | Finding | Fix | Evidence | Status |
|---|---|---|---|---|---|
| H1 | high | `urllib3` in `uv.lock` was below 2.8.0, a version pip-audit reported with a known advisory (it ships in the agent and controller bundles) | bumped to 2.8.0; `[tool.uv] constraint-dependencies` floor `urllib3>=2.8.0` so a later `uv lock` cannot resolve back | commit `e429ce0b` + this commit; pip-audit clean | **fixed** |
| M1 | medium | `certifi` (stale CA roots) and the bundled `uv` binary out of date | `certifi` 2026.10.23 (also a constraint floor), `uv` 0.13.6; `packaging/pins.toml` `[uv]` follows the lock (the bundle builder refuses a mismatch). The bundle's Python stays the pinned 3.12.14 (a Python bump is its own decision) | commit `e429ce0b`; pip-audit clean | **fixed** |
| M2 | medium | no test proved that **every** endpoint needs mTLS or a token; a route added later could ship open | route inventory of the real app: every HTTP route and WebSocket is called with no credential, an unknown API token, a wrong fabric token, an unknown session cookie and a foreign-fabric client certificate - only 401/403/410 (WebSocket closed before accept); an allow-list of open routes (`/healthz`, login, setup...) must match the open routes both ways, each with its decision; a planted open route is caught. Socket level: a real controller bound to a LAN address - every non-loopback listener is a known port, plaintext gets no HTTP answer, gRPC needs a fabric client cert, enrollment a valid join token | `c96aa3ce`, `d74d836e`; THREAT_MODEL IP-S3 | **fixed** |
| M3 | medium | nothing checked that a package carries no key, token, join code, secret file, test fixture or dev password | `scripts/package_secret_scan.py` (folders, `.deb` control + data, tar, zip/whl, raw installer bytes; prints rule + path + offset, never the value); `build_bundle.py`, `build_deb.py`, `build_windows_installer.py` fail on a hit; CI re-scans the artefacts it uploads (Linux and Windows jobs) | `c4873a0d`; tester fix round 1 (TG3-1..TG3-5): PEM keys escaped onto one line (`\n` in JSON/Python/YAML), body lines of any length and no break before `-----END`; tokens/join codes right after an escaped `\n`; `.p12`/`.pfx`/`.jks`/`.keystore`/`id_dsa`/`.env.*` names; archives nested in a folder or archive (wheel in a bundle, tar.gz in a zip) opened up to 3 levels / 2 GiB expanded, deeper, larger or corrupt nested archives fail the scan; `tests/unit/test_v10_g3_package_secret_scan.py`, `tests/unit/test_v10_g3_tester_secret_scan.py`; real artefacts 0 hits | **fixed** |
| M4 | medium | `THREAT_MODEL.md` had no rows for the V0.17 surfaces: agent console (D391), setup page (D392), OS services / installers / firewall rules (D385-D389) | new §5.10 rows IP-S1, IP-S2, IP-E1, IP-E2, IP-I1, IP-I2, IP-S3, each pinned by a test of the shipped code or packaging file | `tests/unit/test_v10_g3_installed_product_threats.py`; `threat_model_check.py` 0 failing | **fixed** |
| M5 | medium | no CI check that an installed controller opens only the D389 ports. Writing it found a **real bug**: `prerm` looked for the ufw rule with `ufw status`, which lists no rules while ufw is inactive, so a rule added while ufw was on survived `apt remove` if ufw was switched off in between | `prerm` checks `ufw show added`; new packaging test installs the real controller `.deb` in a systemd container with ufw active: `ss -ltnH` non-loopback = D389 exactly, PostgreSQL 47150 and the rest loopback only; the named rule is there while installed and gone after remove, also after `ufw disable`. Runs in CI `package-linux` (`tests/component/packaging`) | `ba0004af`; mutation check: the old `prerm` fails `test_no_rule_survives_a_remove_while_ufw_is_off` (1 failed, 2 passed), the fixed one passes 3/3 | **fixed** |
| L1 | low | no static-analysis gate; bandit flagged `hashlib.sha1` for the Windows service SID | `scripts/bandit_gate.py` in CI lint: any medium/high finding not in `scripts/bandit_triage.toml` (keyed by test id + file + line text; one entry covers exactly `count` occurrences, default 1, so a copied triaged line is a new finding (tester TG3-6); reason + decision ref required; stale entries fail) fails the build. The SID SHA-1 is Windows' own algorithm for service SIDs, not a security hash: `usedforsecurity=False` | `0effb9ad`; `tests/unit/test_v10_g3_security_l1.py` | **fixed** |
| L2 | low | dev-only tools (`filelock`, `virtualenv`, `setuptools`) were at versions pip-audit reported | bumped in the lock (dev group; never in a package) | `e429ce0b`; pip-audit clean | **fixed** |

### Triaged bandit findings (16, all medium; none high)

| Test | Where | Why it is safe |
|---|---|---|
| B608 SQL from strings (7) | `common/_gc_schema.py` (5), migrations `0018`, `0028` | every interpolated piece is a module constant of the same file; no request, node or user value reaches SQL text |
| B108 temp paths (4) | `agent/execution/docker_runtime.py`, `sandbox_check.py` (2), `secret_files.py` | three name the job container's own size-capped `/tmp` tmpfs, not a host path; `secret_files.py` uses a private `0700` subfolder of `/dev/shm` whose owner and mode are checked before use (planting it = denial of secrets only, accepted LF-T6) |
| B604 `shell=True` (1) | `cli/completion.py` | false positive: `shell` is Typer's keyword naming the shell to generate completion for; no subprocess starts |
| B506 `yaml.load` (1) | `common/manifest.py` | `_StrictSafeLoader` subclasses `yaml.SafeLoader` (no object construction) and also refuses aliases and duplicate keys; a unit test pins the base class |
| B104 bind all interfaces (2) | `controller/enrollment/codes.py`, `controller/fabric_ca.py` | the wildcard strings are only recognised, to keep them out of join codes and the server certificate; nothing binds to them |

Each entry, with its exact reason and decision reference, is in `scripts/bandit_triage.toml`.

## 3. Accepted risks (unchanged; reasons in `docs/THREAT_MODEL.md` §7)

- **Unsigned Windows installer** (Q2, D395): SmartScreen warns, the install guide says so (`docs/OPERATIONS.md`).
  Added to THREAT_MODEL §7 by this review (it was recorded only in ADR-0022 and OPERATIONS).
- **No sandbox beyond Docker for the subprocess runtime** (WS-E3, Q3): it needs two explicit owner opt-ins in
  `[agent.policy]` (`accept_jobs` + `allow_unsandboxed`, D142); a Windows AppContainer sandbox is a large Windows-only
  effort.
- Every other accepted row (15 rows marked `accepted` in §5), each cited with its reason in §7, is checked by
  `threat_model_check.py`.

## 4. What was not done

- **semgrep**: not run. bandit covers the Python rule set D413 needs; a second engine would add triage work without
  a known class of bug it catches here. Can be added as a CI job later.
- **bandit low-severity findings** are not gated (mostly `subprocess` / `assert` notices that the code review covers).
- **No outside penetration test** (Q51: reviewer agent + tools).
- The open-port check covers the **Linux controller** package. The Windows agent's firewall rule (TCP 47120, Private
  + Domain) is pinned only by unit tests of the code that creates it (IP-I1), not by a port scan of an installed
  Windows machine; the V1.0 lab campaign (human step L) is where a real Windows install is observed.

## 5. How to repeat

```bash
uv run python scripts/threat_model_check.py
uv export --all-extras --all-groups --no-hashes --no-emit-project --frozen > /tmp/req.txt
uvx pip-audit -r /tmp/req.txt --no-deps --disable-pip
(cd frontend && npm audit)
uv run python scripts/bandit_gate.py
uv run python scripts/package_secret_scan.py dist/*.deb dist/*.tar.xz dist/controller/*.deb dist/controller/*.tar.xz
uv run python scripts/pytest_exit_guard.py -m security tests/unit/controller/test_v10_g3_route_inventory.py \
  tests/component/controller/test_v10_g3_listener_auth.py tests/unit/test_v10_g3_installed_product_threats.py
MESHFORGE_TEST_CONTROLLER_DEB_DIR=dist/controller uv run python scripts/pytest_exit_guard.py --run-slow \
  tests/component/packaging/test_v10_g3_open_ports.py
```
