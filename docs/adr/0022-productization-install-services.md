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
