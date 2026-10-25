# ADR-0022: Productization — bundled runtime, machine-wide services, managed PostgreSQL, agent local console

> **Status:** Proposed (2026-10-24) — brief approved by the team 2026-10-24; ADR pending formal acceptance · **Deciders:** team + Lead agent

## Context
Roadmap V0.17 (0.17.1–0.17.8) needs installable, self-running software: a fresh Windows laptop must go from
download to joined fabric in < 10 minutes with no terminal, and uninstall must leave no services or firewall
rules. Today installation is a developer flow (`uv sync`, Docker Compose PostgreSQL, systemd *user* unit or
`schtasks`, `meshforge-agent join` in a terminal; OPERATIONS §1–§4). Full reasoning and the decision table are in
`docs/briefs/V0.17-productization.md` §0–§2 (D384–D397).

## Decision
1. **Runtime:** ship a relocatable CPython 3.12 (python-build-standalone) with wheels installed from `uv.lock`
   (hash-checked) plus a pinned `uv` binary; no frozen executable (D384). Versioned read-only program dirs, state
   in machine-wide data dirs (D385).
2. **Services:** agent runs as an OS service under a dedicated account — Linux system unit as user `meshforge`;
   Windows native service via pywin32 as `NT SERVICE\MeshForgeAgent` (D386). Existing per-user identities are
   migrated, never re-minted (D387) — amends ADR-0013's location rule.
3. **Controller:** Linux `.deb` with controller-managed bundled PostgreSQL 16 (`[controller.db] mode = managed`,
   loopback/socket only, generated secret); `external` mode kept (D390). Windows controller out of scope.
4. **Local console:** the agent serves a loopback-only UI (127.0.0.1:47130, file-permission token) for the
   first-run wizard and owner settings, built from the same React app (D391/D392).
5. **Packaging:** `.deb` via nfpm (D394), Inno Setup on Windows (D395, unsigned for now). Named firewall rules,
   Private/Domain profiles only (D389). Side-by-side upgrade with pre-migration `pg_dump` and automatic rollback;
   uninstall keeps data by default and is checked by `lab/verify_uninstall.py` (D393).

## Alternatives considered
- **PyInstaller / Nuitka:** breaks the child processes we spawn (ML child, benchmark children, `uv` builds), needs
  per-library hooks, triggers antivirus. Rejected.
- **Keep per-user agent (Task Scheduler / user unit):** simpler identity, but stops at logoff — not "self-running".
- **NSSM / WinSW wrapper:** third-party binary; WinSW kept as fallback if spike SP1 fails.
- **Docker or distro PostgreSQL for the controller:** Docker adds a heavy prerequisite; a shared system PostgreSQL
  can clash with other software and differs per distro.
- **Tray app (Qt) for the agent UI:** a second UI stack to maintain; the browser UI reuses the dashboard code.
- **MSI/WiX, AppImage:** more work (MSI) or no clean system-service install (AppImage).

## Trade-offs
+ One tested interpreter everywhere; services survive logoff; no Docker needed for the controller; safe defaults.
− Bundle size (target < 150 MB agent, < 250 MB controller); we own PostgreSQL lifecycle (major upgrades need
  `pg_upgrade` later); identity migration is a risky one-time step; unsigned installer shows SmartScreen;
  Windows work depends on a Windows CI runner.

## Consequences
New: `scripts/build_bundle.py`, `packaging/{deb,windows}/`, `controller/db_managed.py`, agent console server and
`frontend/src/agentConsole/`, `lab/verify_uninstall.py`, `docs/user/`. OPERATIONS §2–§4 become the developer
path only. ADR-0013 gains an Update (machine-wide identity location). Revisit if SP1 shows a Windows service cannot
reach the GPU or Docker, or SP3 shows the bundle cannot be made reproducible.

## Update — what V0.17 G1 built (Linux agent package, 2026-10-24)
Implemented as decided, with these concrete choices (details: `docs/briefs/progress/V0.17-G1.md`):
- **Bundle (D384):** `scripts/build_bundle.py` + `scripts/bundle_lib.py`. Pins in `packaging/pins.toml` (URL +
  sha256, never "latest": python-build-standalone 3.12.14, nfpm 2.48.0; uv = the `uv.lock` wheel, pin checked
  equal). `BUILDINFO.json` + `MANIFEST.sha256` (`sha256sum -c` compatible); two builds are byte-identical
  (manifest and `.tar.xz`). Size budget enforced on the compressed download (Q17): agent 66.9 MB `.tar.xz`,
  84.2 MB `.deb` (budget 100 MB).
- **Install modes (D385):** `MESHFORGE_INSTALL_MODE=system` (set by the unit only, Q11) selects
  `/etc/meshforge` + `/var/lib/meshforge` (Windows: `%PROGRAMDATA%\MeshForge`, for G4). The `.deb` lays out
  `/opt/meshforge/<ver>` + `current` symlink + `/usr/bin` links. A human's admin command on a packaged laptop
  re-executes as `meshforge` via `sudo` (Q15), so it never creates a second identity in `~`.
- **Package (D394):** `scripts/build_deb.py` renders `packaging/linux/nfpm.yaml` (nfpm does not expand variables
  in `contents`), stages the files with fixed modes/mtimes (`.deb` reproducible), inspects the result without
  Debian tools, and checks every ELF file of the bundle with `ldd`: each library is bundled or a known system
  one whose package is in `Depends` (`libc6 (>= 2.28)`, `libcrypt1`, `libgcc-s1`, `libstdc++6`, `zlib1g`).
  lintian: no errors; overrides (each justified in `packaging/linux/lintian-overrides`) cover the bundled
  runtime under `/opt` (shipped as published, pre-compiled bytecode, statically linked libraries).
- **Service (D386) — one addition: `meshforge-agent.path`.** An agent that never joined exits 4 "not enrolled",
  so the service has `ConditionPathExists=/var/lib/meshforge/identity/node.crt` (a fresh install is *inactive*,
  not *failed*) and a path unit (`PathChanged=` on that file) starts it when `join` writes the certificate.
  `PathChanged` (not `PathExists`) so a service that stopped itself (exit 4/5) or was stopped by an admin is
  not restarted behind their back. G3's local console will need the agent to run *before* joining; it then
  replaces this with an in-process wait (follow-up for the Lead).
- **Migration (D387):** `meshforge-agent migrate-identity` (plan read-only, all-or-nothing, `migration.pending`
  blocks minting while undecided, `--dry-run` writes nothing). Owner policy (`policy.toml`) is not migrated (the
  G3 wizard asks), env/peer caches neither (Q14).
- **Uninstall (D393, first version):** `remove` keeps data (and `prerm` marks it, so a re-install is treated as
  fresh and re-enables the units — dpkg passes the old version to `postinst` after a `remove`); `purge` removes
  data, the account and groups; `postrm` resets a `failed` unit state. `lab/verify_uninstall.py --os linux
  [--data-removed] [--docker NAME]` checks it (Windows: G4/G5).
- **Tests:** unit (builder, packaging files vs code, verifier rules); component — the real `.deb` in clean
  Ubuntu 22.04/24.04 systemd containers (migration keeps `node_id`, hardening read back from the running unit
  and the agent's own mount namespace, restart / `kill -9` / exit 4 / exit 5, upgrade, remove, re-install,
  purge, fresh install waiting for the join); cluster — the packaged agent joins a real fabric through the path
  unit, the CPU cap is reported enforced (24.04), and a requirements job is built by the bundled uv on the
  bundled interpreter. CI job `package-linux` builds, lintian-checks, cmp-checks and runs all of it.
- **Hardening deliberately omitted from the unit (accepted by the Lead, 2026-10-25):** `ProtectClock=` (on
  systemd 249 / Ubuntu 22.04 it implies a device allow-list that blocks `/dev/nvidia*`, so GPU chunks fail);
  `PrivateDevices=` (hides the GPU device nodes); `RestrictNamespaces=` (Docker/bubblewrap-style sandboxes and
  uv's build isolation need namespaces); `MemoryDenyWriteExecute=` (breaks JIT users — PyTorch, numba, LightGBM);
  `SystemCallFilter=` (the workload runs arbitrary user code under the agent's process tree; a filter strict
  enough to matter breaks real jobs). Isolation of user code is the sandbox's job (ADR on V0.7 security), not
  the unit's. Revisit per-flag if the sandbox moves fully out of the agent's tree.
- **Start on join via a path unit** (above) is a G1 stop-gap; G3 replaces it with the agent running before join.
- **Accepted G1 deviations:** `safe_source` accepts a group-writable source when the group is the owner's
  private group (Debian user-private-group convention); a source caught mid-rotation is refused (operator
  re-runs `migrate-identity`) rather than retried.

## Update — what V0.17 G3 built (agent local console, 2026-10-25)
Details: `docs/briefs/progress/V0.17-G3.md`; architecture: ARCHITECTURE §23 "Agent local console".
- **The service always runs** (replaces G1's `ConditionPathExists=` + `meshforge-agent.path`): with the console on
  (default for the system service) a supervisor keeps the console up, waits for the join (console or CLI), starts
  the fabric part in process, restarts it in process, stops it on "leave", and keeps the process up after a typed
  stop (revoked / conflict / config) so the console can say why. Exit codes 4/5 still apply to a broken
  configuration file at start (before the console exists). The package's `preinst` disables the G1 path unit on
  upgrade; `prerm` disables units one by one.
- **Console (D391)**: FastAPI + plain uvicorn (Q23) on `127.0.0.1:47130` only; token in
  `/run/meshforge-console/token` (tmpfiles.d, `2750 meshforge:meshforge-admin`) instead of
  `/var/lib/meshforge/console/token` - the state folder is `0750 meshforge:meshforge`, so the admin group could not
  reach a file inside it; `/run` also gives "rotates on restart" for free. The launcher puts a single-use 60-s code
  in the URL fragment, not the token (process arguments are visible to every local user).
- **Docker access (Q24)**: a polkit action + helper run by `pkexec` from the owner's **desktop session** (app-menu
  action), not a button that makes the service call pkexec: the service has `NoNewPrivileges=yes` and no session,
  and any rule letting the service user trigger it would let unsandboxed job code (which runs as that user) make
  itself root-equivalent. The console explains and shows the command.
- **QR (Q25)**: `jsqr` 1.4.0 is **Apache-2.0** (the brief said "both MIT"; still permissive, no network), loaded only
  when the owner opts in to scanning (own chunk).
- **Node name (Q27)**: `Hello.display_name` -> `node.display_name` (migration 0035) -> `GET /v1/nodes` -> dashboard.
- Packaging: `.desktop` entry + icon, `meshforge-console-open` in the bundle, the installing user (`SUDO_USER` /
  `PKEXEC_UID`) joins `meshforge-admin`; the launcher uses `sg meshforge-admin` when that membership is newer than
  the desktop session, so no log-out is needed. The ufw app profile of D389 was not added (an `/etc` conffile
  would survive `apt remove` and fail the uninstall check); the firewall check prints a port rule instead.
