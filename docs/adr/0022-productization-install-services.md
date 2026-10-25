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

## Update — what V0.17 G2 built (Linux controller package, 2026-10-25)
Implemented as decided (D390, D392, D393 controller half, D398; Q18-Q22), details and every deviation in
`docs/briefs/progress/V0.17-G2.md`:
- **Managed PostgreSQL wired in:** `[controller.db]` (mode/port 47150/root/socket_dir/bin_dir/timeouts/backups),
  `db_managed/runtime.py`, started first / stopped last by `ControllerRuntime`, postmaster death = controller
  `FAILURE`. Q18 roles: superuser `meshforge_admin` for migrations (acting as the app role) / backup /
  restore; non-superuser app role `meshforge` owns every table. The dev default password check no longer
  applies in managed mode (the generated passwords are used).
- **Bundle:** `build_bundle.py --role controller` adds `<bundle>/pgsql` (`scripts/pg_bundle.py`, pin in
  `packaging/pins.toml [postgresql]`, Q19 mirror URL first — the release asset still has to be uploaded).
  `numpy` joined the `controller` extra (model serving imports it at start; the bundled controller could not
  start without it).
- **Package:** `meshforge-controller` .deb (`build_deb.py --role controller`), program under
  `/opt/meshforge/controller/` so it coexists with the agent package; `/etc/meshforge/controller.toml` written
  once; ufw profile allowed only when ufw is active, deleted on remove (D389). The .deb is 145.4 MB, inside
  the 150 MB budget with the size check on, after the controller bundle drops the numpy/pyarrow/greenlet test
  suites and C/C++ headers/sources (Lead decision; `build_bundle.py STRIP_SITE`; nfpm's xz alone gave 151.6 MB).
- **Upgrade (D393):** pre-upgrade `pg_dump` + health check + restore-and-exit-4 in the controller; pointer
  switch-back stays G5's.
- **Setup page (D392/Q20):** loopback-only plain HTTP setup mode gated by the one-time token; create fabric =
  admin + CA + first invite; 410 afterwards; in-process restart into TLS. No "fabric name" (no such concept)
  and the join address comes from `controller.toml`, not a page field.
- **Shared code:** parent-death shim in `common/death_signal.py`; the sudo re-exec of admin commands in
  `common/system_cli.py` (agent + controller).
