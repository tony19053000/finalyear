# Spike SP2 — controller-managed PostgreSQL 16 (task 0.17.0, D390 / D393)

**Question:** can the controller ship its own PostgreSQL 16 and run it as a child —
`initdb` once, loopback + unix socket only, generated password, clean stop, refuse a
data dir of another major — without leaving an orphan postmaster when the controller
crashes, and can it take the `pg_dump` / `pg_restore` backups the upgrade path (D393)
needs? Which relocatable binaries, and how big are they?

Script: `lab/spikes/sp2_managed_pg.py` · measurement code: `lab/spikes/_sp2/` ·
prototype (not wired in): `src/meshforge/controller/db_managed/` · tests:
`tests/unit/controller/test_db_managed_pure.py`,
`tests/component/controller/test_db_managed_procs.py`,
`tests/component/controller/test_db_managed_cluster.py` (`slow`).

```bash
uv run python lab/spikes/sp2_managed_pg.py          # prints the JSON pasted in §9
uv run python scripts/pytest_exit_guard.py tests/component/controller/test_db_managed_cluster.py --run-slow -q
```

Measured on the team's native Linux machine: Ubuntu 24.04.4, kernel 6.8, glibc 2.39,
CPython 3.12.13 (2026-10-24).

## 1. Answer in one table

| Question | Result | Evidence |
|---|---|---|
| Binary source (Linux x86_64) | **theseus-rs/postgresql-binaries 16.15.1**, `x86_64-unknown-linux-gnu`, sha256 `0c42e7c0…2dbf74` | §2 |
| Size | archive **39.4 MB** (gz) · unpacked **111.7 MB** · trimmed **68.9 MB** (17.7 MB as `.txz`) | §2.3 |
| `initdb` once (scram, generated pw, 0600 file) | **0.36–0.43 s**; second start skips it; password file `0600`, data dir `0700` | §9 `lifecycle` |
| start → ready (no client needed) | **41 ms** (`postmaster.pid` line 8 = `ready`); warm start total **65–74 ms** | §9 |
| listens on | `127.0.0.1:<port>` + `<socket dir>/.s.PGSQL.<port>` only (`ss -ltnx`) | §9 |
| stop | smart shutdown, exit 0, **10–30 ms**; escalation smart→fast→immediate→SIGKILL(group) | §9 |
| other major's data dir | refused: `ClusterVersionMismatchError` (`config_error`), clear message | §9 `version_mismatch` |
| stale `postmaster.pid` (dead pid / pid reused by an unrelated process / garbage) | all three removed and started; unrelated process untouched | §9 `stale_pidfile` |
| controller SIGKILLed, **no** death signal | **postmaster survives** (7 processes keep running, reparented to the user subreaper); next start finds it, stops it with SIGINT in **55 ms**, starts fresh; committed row kept | §9 `crash_without_death_signal` |
| controller SIGKILLed, **with** `PR_SET_PDEATHSIG=SIGINT` | postmaster does a **fast shutdown in 40 ms**, no process and no pid file left; next start `clean` | §9 `crash_with_death_signal` |
| postmaster itself SIGKILLed | controller sees exit −9; next start: crash recovery, **156–160 ms** kill→ready, committed row kept | §9 `postmaster_killed` |
| port taken by another program | typed `port_in_use`, nothing left running, lock released | §9 |
| second controller on the same data root | typed `locked` | §9 |
| Alembic migrations (0001→0034) on the managed DB | **0.33–0.40 s** fresh, 0.07–0.08 s no-op; node + job store round trip OK | §9 `migrate_and_smoke` |
| `pg_dump` / `pg_restore` (bundled binaries) | 9.6 MB DB: dump **60 ms** (103 KB), restore **120–130 ms**; 134 MB DB (500 k rows): dump **1.8 s** (22.8 MB), restore **0.8 s**; identical row counts; dump file `0600` | §9 |

**Verdict: D390 is feasible as written, with one change to the plan (the binary source)
and three design points G2 must keep** (§7). SP2 does not block G2.

## 2. Q1 — where the relocatable PostgreSQL 16 binaries come from

### 2.1 Candidates evaluated

| Source | What it ships | Linux x86_64 | Windows x64 | Verdict |
|---|---|---|---|---|
| **zonky `embedded-postgres-binaries`** (Maven Central jar → `.txz`), 16.15.0 | **only `initdb`, `pg_ctl`, `postgres`** — no `pg_dump`/`pg_restore`/`psql`; OpenSSL **1.1.1** (EOL 2023), ICU 60 | 14.7 MB jar, 59 MB unpacked, old glibc floor | 16.15.0 exists, same 3 programs | **Rejected:** D393 needs `pg_dump`/`pg_restore`; EOL OpenSSL. |
| **theseus-rs `postgresql-binaries`** (GitHub releases), 16.15.1 | full client + server set (`pg_dump`, `pg_restore`, `pg_upgrade`…), contrib, `$ORIGIN` RUNPATHs, OpenSSL 3, ICU 72, timezone data, per-asset `.sha256`, SBOM + provenance attestation | 39.4 MB `.tar.gz`, **needs glibc ≥ 2.36** | 47.7 MB `.zip`, built from source with MSVC | **Chosen.** |
| EDB "binaries" zips | full Windows install incl. pgAdmin 4 + StackBuilder | **no Linux binaries** (discontinued) | 332 MB zip (`postgresql-16.15-1-windows-x64-binaries.zip`, HTTP 200) | Windows fallback only (needs heavy trimming). |
| PGDG apt `.deb`s (`postgresql-16`, `postgresql-client-16`) | distro build | links system `libicu`, `libssl3`, `libldap`… per distro release; installs a system cluster/service | — | Rejected: not relocatable, differs per distro, clashes with D390's "no shared system PostgreSQL". |
| conda-forge `postgresql` 16.15 | relocatable via conda prefix rewrite, old glibc floor | spread over ~15 packages (needs micromamba to assemble) | yes | **Fallback** if we must support glibc < 2.36 (§6 R1). |
| PyPI `pgserver` 0.1.4 / `postgresql-wheel` 14.1.2 | PG in a wheel | last release 2024-06 / 2021-12 | — | Rejected: stale, behind on security releases. |
| Build from source ourselves in CI | full control (glibc floor, trim at build) | ~10 min CI job per release | MSVC job too | **Long-term fallback** (§6 R2). |

### 2.2 The pin (in `src/meshforge/controller/db_managed/binaries.py`)

| Target | URL | sha256 (verified by download, 2026-10-24) |
|---|---|---|
| Linux x86_64 | `https://github.com/theseus-rs/postgresql-binaries/releases/download/16.15.1/postgresql-16.15.1-x86_64-unknown-linux-gnu.tar.gz` | `0c42e7c05f0cb596b82ac07990c96fc261ec2e8106055d557c76442d712dbf74` |
| Windows x64 (later, D390) | `…/16.15.1/postgresql-16.15.1-x86_64-pc-windows-msvc.zip` | `1b56bcc86b33f6fd741f22ed670b9ed3d7a4feb25b45ae4ec012c442e15a7cb5` |

16.15 = the same minor as the dev Compose image `postgres:16.15-alpine3.24`, so the
managed and the external (dev) paths run the same server version.

**Licence.** PostgreSQL itself: PostgreSQL Licence (BSD-like, permissive; `COPYRIGHT`
kept in the bundle). theseus build scripts: PostgreSQL-style licence (`LICENSE`).
Bundled shared libraries are Debian 12 builds with their Debian copyright files in
`dependency-notices/` (kept): OpenSSL 3 (Apache-2.0), ICU 72 (ICU/MIT), krb5 (MIT),
libxml2 (MIT), zlib, zstd/lz4 (BSD), liblzma (PD/0BSD), keyutils (LGPL-2+),
libstdc++/libgcc (GPL-3 **with the GCC Runtime Library Exception**). The trim drops
`psql` and with it **GNU readline (GPL-3)**, plus Berkeley DB and the bundled CPython
(PL/Python). A short licence review of the final `.deb` is a G2 item.

### 2.3 Size and trim

| | bytes | |
|---|---|---|
| archive (`.tar.gz`) | 39 375 696 | 39.4 MB |
| unpacked | 111 688 454 | 111.7 MB |
| **trimmed** | **68 870 091** | **68.9 MB** (of which `libicudata.so.72` 30 MB) |
| trimmed as `.txz` | 17 698 664 | 17.7 MB (what a `.deb` would roughly carry) |
| trimmed + `strip --strip-unneeded` | 67 335 219 | −1.5 MB only → **not done** (keeps upstream hashes reproducible) |

Trim rule (`lab/spikes/_sp2/bundle.py`): keep `postgres initdb pg_ctl pg_dump pg_restore
pg_controldata pg_resetwal pg_upgrade` (the last four for a future `pg_upgrade`);
server modules `plpgsql.so`, `dict_snowball.so` and the encoding conversions; `share/`
minus `doc/`, contrib extension scripts and psql/pg_service samples; licence files; then
**exactly the shared libraries those ELF files load** (`ldd` closure inside the bundle:
19 libraries). Dropped: `psql`, contrib modules, PL/Python + CPython 3.11, headers,
static libs, ECPG. Every component test runs on the trimmed tree, so a file the trim
forgets fails `initdb`/start/dump there.

Budget check (brief §4: controller < 250 MB incl. PostgreSQL): PostgreSQL takes
68.9 MB unpacked / ~18 MB compressed.

## 3. Q2 — the prototype (`meshforge.controller.db_managed`)

| Module | Role |
|---|---|
| `binaries.py` | the pins; `PgBinaries.check()` — required programs present, `postgres --version` major = 16 (a wrong glibc fails here, typed `binaries_unusable`) |
| `layout.py` | `<root>/{managed.lock,password,data/,run/,log/,backups/}`, dirs `0700`; socket-path rules (≤ 107 bytes, absolute, no `,`/`"`) |
| `secret.py` | password: `secrets.token_urlsafe(32)` (256 bit), written once `0600` via the existing atomic writer, read with the existing owner/mode check; `pgpass_line` |
| `pidfile.py` | pure parsing of `postmaster.pid` (8 lines, status `ready`), `PG_VERSION`, `postgres --version` |
| `commands.py` | pure argv/env builders; parent-death shim |
| `procs.py` | `/proc` scan for postgres processes whose cwd is our data dir; `stop_foreign` escalation for non-children |
| `cluster.py` | `ManagedCluster.start()/stop()/reconcile()/dump()/restore()` |
| `errors.py` | `ManagedDbError(reason=…)` + `ClusterVersionMismatchError` |

Behaviour (all measured in §9):

* **initdb once**: `initdb --auth-local=scram-sha-256 --auth-host=scram-sha-256
  --pwfile=<root>/password --encoding=UTF8 --locale=C --data-checksums` into
  `data.initdb-tmp/`, then renamed to `data/` — a crash mid-initdb never leaves a half
  cluster (a leftover tmp dir is deleted and initdb re-run; tested). The password is
  never on argv (`/proc/<pid>/cmdline` is world-readable) and never in the
  environment: `pg_dump`/`pg_restore` get a one-use `PGPASSFILE` (0600, inside the
  0700 socket dir, deleted after). Test: password absent from logs, server logs,
  postmaster cmdline and environ.
* `C` locale: byte-order collation, no dependency on host locales, and no ICU
  collation-version drift when a later bundle ships a newer ICU. Trade-off: `upper()`
  of non-ASCII text follows C rules. Our schema has no locale-dependent ordering
  that the dev image (`en_US.utf8`) gives differently in tested paths — G2 should run
  the full component suite once against a managed instance to confirm.
* **start**: `postgres -D data -c listen_addresses=127.0.0.1 -c port=N
  -c unix_socket_directories=<run> -c unix_socket_permissions=0700 -c timezone=UTC
  -c logging_collector=on …` as a **direct child** in its own session; forced with
  `-c` on every start so a hand-edited `postgresql.conf` cannot open it to the LAN.
  Fresh environment (inherited `PGHOST`/`PGDATA`/`PGPASSWORD` would change behaviour).
* **ready** = `postmaster.pid` line 8 says `ready` and line 1 is our child's pid — no
  client connection, no `pg_isready`. Then the app database is created if missing.
* **refuses** `PG_VERSION` ≠ 16 (`ClusterVersionMismatchError`, `config_error`, names
  both versions and the remedy) and a non-empty data dir without `PG_VERSION`
  (`data_dir_invalid`; the files are left untouched — tested).
* **stop**: SIGTERM (smart, 5 s) → SIGINT (fast, 30 s) → SIGQUIT (immediate, 10 s) →
  SIGKILL to the whole process group. A postmaster that already died (exit by
  signal) is reported `already_exited`, not as a clean stop.
* **one manager per cluster**: `flock` on `managed.lock`, held for the cluster's life
  in the controller process; released by the kernel when the controller dies, so a
  crash never blocks the next start.

## 4. Q3 — crash behaviour (measured) and the design chosen

**Measured.** With a plain child, `kill -9` of the "controller" leaves the postmaster
and its 6 children running (re-parented to pid 2903, the user's systemd subreaper;
`postmaster.pid` stays). PostgreSQL has no parent-watch of its own.

**`PR_SET_PDEATHSIG` *is* usable for PostgreSQL — with SIGINT, not SIGKILL.** The
agent's workload shim (`agent/process.py`) arms SIGKILL; for a postmaster that would
be wrong (it leaves backends and shared memory behind — PostgreSQL docs, "Shutting
Down the Server"). Armed with **SIGINT** it is PostgreSQL's *fast shutdown*: measured
**40 ms** from the controller's death to postmaster exit, log `received fast shutdown
request` + shutdown checkpoint, no pid file, next start `clean`. The death signal
survives `execve` (not for set-uid binaries; ours is not), so it is armed by a tiny
`python -I -S -c` exec shim (same pid as the postmaster, no `preexec_fn` in a threaded
controller) that also exits 137 if the controller already died before `prctl` (tested).

**Chosen: three layers, cheapest first.**

1. **Death signal (SIGINT)** — covers controller SIGKILL / OOM kill / crash on Linux.
   Limit: it fires when the *thread* that forked exits, so `start()` must run on the
   event-loop thread (documented in `ManagedCluster.start`), never in `to_thread`.
2. **Restart reconciliation** (`ManagedCluster.reconcile`, under the lock, before
   every start) — covers what (1) cannot (a start from the wrong thread, non-Linux,
   anything we did not foresee). A process is "ours" when `/proc/<pid>/exe` is
   `postgres` **and** `/proc/<pid>/cwd` is our data dir (every postmaster and backend
   `chdir`s there), so a pid reused by an unrelated process is never signalled
   (tested: an unrelated live process named by the pid file is left alone and the
   stale file removed — PostgreSQL alone would refuse to start in that case).
   Orphan postmaster → SIGINT → SIGQUIT → SIGKILL (**55 ms** measured), stray
   backends → SIGQUIT/SIGKILL, then start fresh.
   **Adopt was rejected**: a postmaster that is not our child cannot be `wait()`ed for,
   has no death signal tied to the new controller, and may run with an older
   version's settings; stopping it costs one checkpoint (tens of ms).
3. **systemd** (G2 unit): the postmaster lives in the controller service's cgroup, so
   `systemctl stop` / a crash-restart of the unit kills whatever is left. G2 should use
   `KillMode=mixed` (SIGTERM to the controller only, which stops PostgreSQL in order;
   SIGKILL to the rest after `TimeoutStopSec`) rather than the default
   `control-group`, which would SIGTERM every backend directly.

**Postmaster dies while the controller runs** (measured): the child exits −9; restart
runs crash recovery and is ready in 156–160 ms with committed data intact. G2 must watch
the child (`proc.wait()` task) and decide: restart it with backoff, or exit the
controller non-zero so systemd restarts both (simpler; recommended).

## 5. Q4 / Q5 — migrations, store smoke, backup round trip

* `upgrade_to_head()` (the controller's own startup path) against
  `ManagedCluster.database_settings()` (TCP `127.0.0.1`, generated password): fresh
  0001→0034 in 0.33–0.40 s, no-op re-run 0.07–0.08 s. Then `Registry.upsert_node_on_hello`
  + `JobStore.create_job` + two transitions → `queued`. No change to the store was
  needed.
* `pg_dump --format=custom` over the unix socket with the bundled binary, restored by
  `pg_restore --single-transaction --exit-on-error` into a fresh database: identical
  row counts in all 31 (32) tables. Small DB (9.6 MB on disk, schema + 6 rows): dump
  60 ms / restore 120–130 ms. 134 MB DB (500 k rows): dump 1.8 s (22.8 MB file) /
  restore 0.8 s. D393's pre-upgrade backup fits easily inside its 60 s health window.
* **Found and fixed:** `pg_dump` creates its output with the umask (`0664` here) — a
  backup is the whole database (tokens, encrypted secrets). The prototype pre-creates
  the file `0600` (pg_dump truncates and keeps the mode) and writes to
  `.<name>.partial` → rename. Test asserts `0600`.

## 6. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **glibc ≥ 2.36 floor** (Debian 12+, Ubuntu 23.04+/24.04). Ubuntu 22.04 (glibc 2.35) — common in WSL2 — cannot run this build: the bundled `libstdc++` (pulled in by ICU) needs `arc4random` (2.36), `libgcc_s` needs 2.35. | `PgBinaries.check()` fails typed (`binaries_unusable`) with the loader message; G2 states the requirement in the `.deb` (`Depends: libc6 (>= 2.36)`). If the team needs 22.04: build from source in CI on an older base, or assemble from conda-forge. |
| R2 | Upstream rebuilt release assets in place recently (releases 16.6.1–16.15.1 all show publish dates 2026-10-18…24 under the same tags). A future rebuild changes the bytes. | The **sha256 is the identity**, the URL is not: a changed asset fails the build loudly. G2/SP3 must **mirror** the pinned archive (own GitHub release or CI cache) so builds stay reproducible. |
| R3 | Bundled OpenSSL / ICU / libxml2 security updates now come from us, not the distro. | Track PostgreSQL minor releases (quarterly) + theseus rebuilds; bump the pin like any dependency (Dependabot-style reminder in G2). Exposure is low: loopback + 0700 socket only, no TLS used. |
| R4 | Death signal is per *thread*. | `start()` documented event-loop-only; reconciliation catches the rest. A unit test cannot catch a misuse — G2 code review item. |
| R5 | `C` locale differs from the dev image (`en_US.utf8`). | G2: run the full component suite once against a managed instance. |
| R6 | `/proc`-based identification is Linux-only. | Windows controller is out of scope (D390); a Windows port needs a process query by image path + `postmaster.pid`. |
| R7 | Two clusters on one host must use different ports and socket dirs. | Port is configuration; G2 picks a default (e.g. 5433, avoiding a system PostgreSQL on 5432) and surfaces `port_in_use` in `doctor`. |

## 7. What G2 (0.17.3) must still do

1. Settings: `[controller.db] mode = "managed" | "external"`, `port` (default ≠ 5432),
   `root` (`/var/lib/meshforge/db`), optional `socket_dir` (`/run/meshforge/db` via
   systemd `RuntimeDirectory=`); wire `ManagedCluster.start()` before `check_connection`
   / `upgrade_to_head` in `server.py` **on the event-loop thread**, `stop()` after the
   engine is disposed; managed mode replaces the dev default password (STATUS tech debt).
2. Supervise the postmaster while running (exit → log critical, exit the controller
   non-zero; systemd restarts). Unit: `KillMode=mixed`, `TimeoutStopSec` ≥ fast-shutdown
   timeout + margin.
3. Move `lab/spikes/_sp2/bundle.py` (fetch/verify/trim) into `scripts/build_bundle.py`;
   mirror the archive; pin by sha256; `Depends: libc6 (>= 2.36)`; licence review of the
   trimmed tree (keep `COPYRIGHT`, `LICENSE`, `dependency-notices/`).
4. Decide the role model: the prototype connects as the initdb superuser
   (`meshforge`). Least privilege would add a non-superuser owner role for the app DB
   and keep the superuser for migrations/backup — a small ADR-level choice.
5. D393 backup: `cluster.dump(backups/pre-<ver>.dump)`, keep last 3, restore via
   `drop_database` + `create_database` + `restore` (all prototyped and tested).
6. Add a `doctor` check: binaries usable, glibc, port free, password file private,
   data dir major.
7. Shared code: the agent and the controller now each have an exec shim for the
   death signal (different signal, different extras). Consider moving a generic one to
   `common/` when G2 touches it (not done here: out of scope).
8. Run the existing component suite once against a managed instance (R5).

## 8. Reproduce

```bash
uv sync --extra controller --extra agent
uv run python lab/spikes/sp2_managed_pg.py                 # ~15 s after the first download
uv run python scripts/pytest_exit_guard.py tests/unit/controller/test_db_managed_pure.py \
    tests/component/controller/test_db_managed_procs.py -q
uv run python scripts/pytest_exit_guard.py tests/component/controller/test_db_managed_cluster.py \
    --run-slow -q                                          # downloads ~39 MB once
```

The cache is `.cache/meshforge-pg/` (gitignored) or `$MESHFORGE_PG_CACHE`; offline,
the slow tests skip with the reason.

## 9. Raw result (`sp2_managed_pg.py`, 2026-10-24, this machine)

```json
{
  "host": {
    "platform": "Linux-6.8.0-147-generic-x86_64-with-glibc2.39",
    "libc": "glibc 2.39",
    "python": "3.12.13"
  },
  "bundle": {
    "url": "https://github.com/theseus-rs/postgresql-binaries/releases/download/16.15.1/postgresql-16.15.1-x86_64-unknown-linux-gnu.tar.gz",
    "sha256": "0c42e7c05f0cb596b82ac07990c96fc261ec2e8106055d557c76442d712dbf74",
    "archive_bytes": 39375696,
    "unpacked_bytes": 111688454,
    "trimmed_bytes": 68870091,
    "prepare_ms": 65.0
  },
  "lifecycle": {
    "server_version": "16.15",
    "first_start": {
      "initdb_ms": 384.5,
      "ready_ms": 41.2,
      "total_ms": 489.1,
      "password_created": true
    },
    "first_stop": {
      "mode": "smart",
      "ms": 28.2,
      "exit": 0
    },
    "warm_start": {
      "initdb_ms": null,
      "ready_ms": 41.0,
      "total_ms": 67.2,
      "password_created": false
    },
    "warm_stop": {
      "mode": "smart",
      "ms": 9.9,
      "exit": 0
    },
    "password_file_mode": "0o600",
    "data_dir_mode": "0o700",
    "listeners": [
      "200 /tmp/mfpg-wxlo0eoz/.s.PGSQL.41353",
      "200 127.0.0.1:41353"
    ],
    "pidfile_after_stop": false
  },
  "version_mismatch": {
    "refused": true,
    "error": "ClusterVersionMismatchError",
    "failure_type": "config_error",
    "message": "the database in /tmp/sp2-oo1xd1dw/mismatch/data was created by PostgreSQL 15, but this MeshForge bundles PostgreSQL 16; refusing to start it. Restore a backup taken with pg_dump, or upgrade the data with pg_upgrade"
  },
  "stale_pidfile": {
    "dead_pid": {
      "reconcile": "stale_pidfile_removed",
      "start_ms": 70.6,
      "unrelated_process_untouched": true
    },
    "reused_pid": {
      "reconcile": "stale_pidfile_removed",
      "start_ms": 68.1,
      "unrelated_process_untouched": true
    },
    "corrupt": {
      "reconcile": "corrupt_pidfile_removed"
    }
  },
  "crash_without_death_signal": {
    "die_with_parent": false,
    "postmaster_survived_parent_kill": true,
    "postmaster_exit_after_ms": null,
    "processes_in_data_dir_after_watch": 7,
    "pidfile_left": true,
    "restart_reconcile": "orphan_stopped",
    "restart_reconcile_signal": "SIGINT",
    "restart_reconcile_ms": 55.0,
    "restart_total_ms": 120.0,
    "committed_rows_after_restart": 1,
    "server_log_shutdown_lines": [
      "LOG:  database system was shut down at 2026-10-24 10:52:33 UTC",
      "LOG:  database system is ready to accept connections",
      "LOG:  received fast shutdown request",
      "LOG:  checkpoint complete: wrote 934 buffers (5.7%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.008 s, sync=0.013 s, total=0.023 s; sync files=306, longest=0.001 s, average=0.001 s; distance=4283 kB, estimate=4283 kB; lsn=0/1B5F988, redo lsn=0/1B5F988",
      "LOG:  database system was shut down at 2026-10-24 10:52:37 UTC",
      "LOG:  database system is ready to accept connections",
      "LOG:  received smart shutdown request",
      "LOG:  checkpoint complete: wrote 6 buffers (0.0%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.002 s, sync=0.002 s, total=0.005 s; sync files=5, longest=0.001 s, average=0.001 s; distance=10 kB, estimate=10 kB; lsn=0/1B62190, redo lsn=0/1B62190"
    ]
  },
  "crash_with_death_signal": {
    "die_with_parent": true,
    "postmaster_survived_parent_kill": false,
    "postmaster_exit_after_ms": 40.5,
    "processes_in_data_dir_after_watch": 0,
    "pidfile_left": false,
    "restart_reconcile": "clean",
    "restart_reconcile_signal": null,
    "restart_reconcile_ms": 4.4,
    "restart_total_ms": 70.3,
    "committed_rows_after_restart": 1,
    "server_log_shutdown_lines": [
      "LOG:  database system was shut down at 2026-10-24 10:52:37 UTC",
      "LOG:  database system is ready to accept connections",
      "LOG:  received fast shutdown request",
      "LOG:  checkpoint complete: wrote 934 buffers (5.7%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.010 s, sync=0.015 s, total=0.027 s; sync files=306, longest=0.001 s, average=0.001 s; distance=4283 kB, estimate=4283 kB; lsn=0/1B5F988, redo lsn=0/1B5F988",
      "LOG:  database system was shut down at 2026-10-24 10:52:37 UTC",
      "LOG:  database system is ready to accept connections",
      "LOG:  received smart shutdown request",
      "LOG:  checkpoint complete: wrote 6 buffers (0.0%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.002 s, sync=0.002 s, total=0.007 s; sync files=5, longest=0.001 s, average=0.001 s; distance=10 kB, estimate=10 kB; lsn=0/1B62190, redo lsn=0/1B62190"
    ]
  },
  "port_in_use": {
    "reason": "port_in_use",
    "message": "PostgreSQL cannot listen on 127.0.0.1:47773: the port is in use by another program; choose another port",
    "processes_left": 0,
    "lock_released": true
  },
  "concurrent_start": {
    "reason": "locked"
  },
  "postmaster_killed": {
    "stop_after_kill": "already_exited",
    "exit_code": -9,
    "backends_left_after_postmaster_kill": 0,
    "restart_reconcile": "stale_pidfile_removed",
    "restart_ready_ms": 122.2,
    "kill_to_ready_ms": 159.8,
    "committed_rows_after_recovery": 1,
    "server_log": [
      "LOG:  database system was shut down at 2026-10-24 10:52:39 UTC",
      "LOG:  database system is ready to accept connections",
      "LOG:  database system was interrupted; last known up at 2026-10-24 10:52:39 UTC",
      "LOG:  database system was not properly shut down; automatic recovery in progress",
      "LOG:  redo starts at 0/1730D80",
      "LOG:  redo done at 0/1B5BAB8 system usage: CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s",
      "LOG:  checkpoint complete: wrote 925 buffers (5.6%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.009 s, sync=0.013 s, total=0.025 s; sync files=302, longest=0.001 s, average=0.001 s; distance=4267 kB, estimate=4267 kB; lsn=0/1B5BAE0, redo lsn=0/1B5BAE0",
      "LOG:  database system is ready to accept connections",
      "LOG:  received smart shutdown request",
      "LOG:  checkpoint complete: wrote 4 buffers (0.0%); 0 WAL file(s) added, 0 removed, 0 recycled; write=0.001 s, sync=0.001 s, total=0.004 s; sync files=4, longest=0.001 s, average=0.001 s; distance=8 kB, estimate=3841 kB; lsn=0/1B5DD58, redo lsn=0/1B5DD58"
    ]
  },
  "migrate_and_smoke": {
    "target_revision": "0034",
    "revision_before": null,
    "migrate_ms": 401.8,
    "noop_migrate_ms": 80.8,
    "job_created": true,
    "job_state": "queued",
    "job_id": "job_01M5PRN59WK3SNSMTKBKCP5171"
  },
  "dump_restore": {
    "dump_ms": 58.3,
    "restore_ms": 132.6,
    "dump_bytes": 102891,
    "dump_file_mode": "0o600",
    "source_db_bytes": 9608215,
    "tables": 31,
    "rows_total": 6,
    "identical_row_counts": true
  },
  "dump_restore_500k_rows": {
    "dump_ms": 1811.1,
    "restore_ms": 793.2,
    "dump_bytes": 22835271,
    "dump_file_mode": "0o600",
    "source_db_bytes": 133872663,
    "tables": 32,
    "rows_total": 500006,
    "identical_row_counts": true
  }
}
```
