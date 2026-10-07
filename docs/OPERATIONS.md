# OPERATIONS — running MeshForge in dev

How to keep a controller and an agent running on a laptop, how to check that the
fabric is healthy, and how to remove all of it again. Roadmap **0.3.13**
(background service, *documented*) and **0.3.12** (REST + `compute`).

> **Nothing here is installed for you.** The service units and the Task Scheduler
> template live in [`deploy/`](../deploy/), and
> `uv run python scripts/print_service_setup.py` **prints** the commands so you can
> read them before running them. No script in this repository edits your service
> manager (decision D17). That is deliberate: a tool that quietly registers a
> background service on a shared laptop is not something you can easily undo.

---

## 0. Trust boundary — read this first

In V0.3 the controller exposes:

| Port | Protocol | Who dials it | Authentication |
|---|---|---|---|
| **47100** | gRPC `AgentControl` | agents dial **out** to the controller | none yet |
| **47110** | HTTP REST `/healthz`, `/v1/*` | `compute`, later the dashboard/SDK | **none yet** |

- The REST surface is **read-only**: there is no endpoint that changes a node, a
  session or the database — no delete, no drain, no command (decision D14).
- It is **unauthenticated**, so anyone who can reach port 47110 can read the
  fabric's inventory: hostnames, hardware summaries, peer addresses, utilisation.
- Therefore **keep it LAN-bound.** The defaults are loopback
  (`127.0.0.1:47100` / `127.0.0.1:47110`) and the controller *refuses* a wildcard
  bind (`0.0.0.0`) unless you set `[controller] allow_any_bind = true`. Bind a
  specific LAN address instead, and never expose either port to the internet or to
  a network you do not control.
- Identity, mTLS and authorization arrive in **V0.7** ([ADR-0008](adr/0008-pairing-and-mtls.md)).
  Until then the project's security baseline (`CLAUDE.md` §9) is "trusted LAN
  machines only".

The agent needs **no inbound port** for the control plane — it dials out
([ADR-0002](adr/0002-agent-initiated-grpc-session.md)); only its optional peer
blob server (TCP 47120, V0.5, §0d) listens. Only the controller laptop needs
a firewall rule (inbound TCP 47100, and 47110 if other laptops should run
`compute`).

### 0a. The fabric token (V0.4, D44 — [ADR-0014](adr/0014-interim-execution-trust.md))

From V0.4 the REST surface can make code run on another laptop, so it is **no
longer all open**. Every `POST`/`PUT`/`DELETE`, every `/v1/jobs*` read (job logs
contain user output) and every `/v1/blobs*` call needs
`Authorization: Bearer <fabric token>`; without it the answer is `401` with the
error code `unauthorized`. The node and session reads above stay open.

- **Where it lives:** the controller creates it on its first start in
  `<data dir>/controller/fabric.token` (Linux `~/.local/share/meshforge/controller/`,
  Windows `%LOCALAPPDATA%\MeshForge\controller\`), readable by you only
  (`0600` / owner-only ACL). Override with `[controller] token_file`. A token file
  other users can read, or one that is not a valid token, stops the controller
  from starting (exit code 4) — fix it with `chmod 600` or `token rotate`.
- **See it:** `meshforge-controller token show` (prints only the token on stdout).
- **Replace it** (e.g. it leaked): `meshforge-controller token rotate`. A running
  controller switches to the new token within about a second; the old one stops
  working. Hand the new token to every laptop again.
- **On each laptop that runs `compute`** set one of, highest precedence first:
  `compute --token-file FILE …`, `MESHFORGE_CLI__TOKEN=<token>`, `[cli] token = "…"`
  in its `config.toml`, or `[cli] token_file = "…"`. On the controller laptop
  itself `compute` finds the controller's own token file and needs nothing.
- **Never** paste the token into a URL or a chat log; `compute` sends it only as
  a header and never prints or logs it. The controller logs only a 12-character
  fingerprint (`controller.token.ready` / `.rotated`) so you can tell tokens apart.

This is one shared secret for the whole fabric — no per-user identity, no audit.
It keeps "anyone on the Wi-Fi" from running code on your laptops; it is not a
substitute for V0.7's mTLS and user identity.

### 0a2. The fabric CA (V0.7 G1, D134 — [ADR-0016](adr/0016-v07-security-design.md))

V0.7 replaces the shared fabric token with real identities: every laptop will get a
certificate signed by **your fabric's own certificate authority (CA)**, and every
connection will be encrypted and checked (that part arrives in later V0.7 groups —
see "not enforced yet" below). The CA is created once, on the controller laptop:

```bash
meshforge-controller fabric create        # once; prints the fabric id and fingerprints
meshforge-controller fabric show          # any time; --json for scripts
```

- **What it makes:** `<data dir>/controller/ca/ca.key` + `ca.crt` (the CA, valid 10
  years) and `<data dir>/controller/tls/server.key` + `server.crt` (the controller's
  own certificate, valid 1 year). Both folders are `0700`, every file `0600`
  (owner-only ACL on Windows). Nothing secret is printed.
- **The join fingerprint** (`fabric show`, 32 hex characters) is what join codes will
  carry so a new laptop can tell the real controller from an impostor. Reading it
  aloud to compare is a valid check.
- **Back up `ca.key` offline** (an encrypted USB stick, a password manager). It never
  needs to leave this laptop otherwise. Anyone holding it can mint laptops into your
  fabric.
- **It refuses to overwrite** (exit 5). `fabric create --force` replaces the CA — do
  this only if `ca.key` leaked or was lost: every laptop must then re-join with a new
  code, and a running controller must be restarted. (Automatic CA rotation is V0.17.)
- **The server certificate looks after itself.** Every controller start checks it and
  issues a new one (with a new key) when it is missing, damaged, from an old CA,
  two thirds through its life, or when the addresses changed. Its names are
  `meshforge-controller`, the `grpc_bind` / `http_bind` IPs and the
  `[controller.api] advertise_url` host. If the controller binds `0.0.0.0`
  (`allow_any_bind`), list the addresses laptops use in
  `[controller.tls] extra_sans = ["192.168.1.10", "controller.lan"]`.
- **A damaged CA stops the controller** (exit 4, `controller.start_failed` names the
  file and the fix): loose permissions → `chmod 600` the file / `chmod 700` the
  folder; a missing half, a garbled file or a key that does not match → restore
  from your backup, or `fabric create --force` (everyone re-joins). No CA at all is
  fine in this version.
- **A crash during `fabric create --force` repairs itself** (D149c). A replace moves
  the old `ca/` aside as a hidden `.ca.old-<hex>` before the new one is moved in; if
  the machine dies exactly between the two steps, the next controller start,
  `fabric show` or `fabric create` **restores** that old CA (log
  `controller.fabric.interrupted_replace_restored`) — it never deletes the only copy
  of the key your laptops trust. Run `fabric create --force` again if you still
  want the new CA. If several `.ca.old-*` folders exist and `ca/` is missing, nothing
  is touched and the command names them: rename the one you want to `ca` by hand
  (the newest is usually right; `fabric show` then confirms its fingerprint).
- **Not enforced yet (V0.7 G1/G2):** every port except the enrolment port is still
  plaintext and the fabric token above still protects REST; laptops can join (§0a3)
  but a laptop that has not joined still connects. TLS/mTLS and revocation come in
  G3, per-user logins in G4. `fabric show` says `tls_enforced: false` until then.
  Before G3 lands, every lab laptop must have joined once.

### 0a3. Joining a laptop to the fabric (V0.7 G2, D135/D136)

On the **controller laptop** (needs the CA from §0a2 and the database), make one
code per laptop:

```bash
meshforge-controller fabric invite            # prints ONE code: mf1-xxxxx-xxxxx-...
meshforge-controller fabric invite --ttl 2h   # 1 min - 24 h, default 15 min
compute fabric invite                         # same, over REST, from any laptop with the fabric token
meshforge-controller fabric invites           # list (never shows codes again); --revoke inv_... to cancel
```

On the **new laptop**:

```bash
meshforge-agent join            # paste the code at the hidden prompt (keeps it out of history)
meshforge-agent identity show   # enrolled: True, fabric_id, cert_not_after
```

- **What the code is:** the controller's enrolment address, a single-use secret and
  the first half of the CA fingerprint (the "CA pin" printed with it). The laptop
  checks the controller's CA against the pin **before** it sends the secret; a
  fake controller learns nothing. Treat the code like a password until it is used
  or expires; it works once.
- **The address in the code** is `[controller.enroll] advertise`, else the
  enrolment bind address (default: the `grpc_bind` IP, port **47101**; open it in
  the firewall like 47100). If the controller binds `0.0.0.0`, set
  `[controller.enroll] advertise = "192.168.1.10:47101"` or pass `--address`.
- **Reinstalled laptop / new key:** if a laptop kept its `node_id` but has a new
  key, a normal code is refused `node_key_conflict`; make one with
  `fabric invite --replace <node_id>`. To re-join with the same key (e.g. after
  deleting the certificate) use `meshforge-agent join --force` with a new code.
- **The certificate** (`node.crt`, valid 30 days) and the CA (`ca.crt`) sit beside
  `node.key` in the agent's identity folder, owner-only. `identity reset` removes
  them too (the node gets a new `node_id` and must join again).

| Message on the laptop | Meaning | Fix |
|---|---|---|
| `this is not a valid code (a character is wrong)` / `wrong length` | typo or truncated copy | copy the whole code again |
| `does not hold the fabric CA your code names` (`pin_mismatch`) | wrong address, another fabric, or the CA was replaced after the code was made | check the address; ask for a new code |
| `refused ... (token_used / token_expired / token_revoked)` | the code was already used, ran out, or was cancelled | ask for a new code |
| `refused ... (node_key_conflict)` | this `node_id` is enrolled with another key | admin: `fabric invite --replace <node_id>` |
| `refused ... (rate_limited)` | too many attempts from this address | wait a minute |
| `already enrolled` (exit 5) | this laptop joined before; the code was not used | `meshforge-agent join --force` if you mean to re-join |
| `cannot reach the controller` (exit 6) | controller down, wrong IP, firewall on 47101 | start it / open the port |

### 0b. Running other people's jobs on this laptop (V0.4, D45/D51 — [ADR-0014](adr/0014-interim-execution-trust.md))

An agent **runs nothing** unless its owner opts in. In that laptop's `config.toml`:

```toml
[agent.execution]
enabled = true              # run jobs the controller sends (default false)
allow_unsandboxed = false   # see "subprocess runtime" below before changing this
grace_s = 3.0               # cancel/timeout: SIGTERM, then SIGKILL after this (<= 3 s)
# workdir_root = ""         # empty = <data dir>/work
# default_memory_mb = 4096, default_pids = 1024, default_cpus = (no limit),
# default_timeout_s = 3600, max_timeout_s = 86400, run_as = "auto"
# keep_workdirs = false     # true = keep each finished job's folder + its logs
# max_concurrent_attempts = 4
# max_cpu_jobs = 1          # V0.6: jobs without a GPU run at once (0 = none)
# max_gpu_jobs = (empty)    # V0.6: GPU jobs at once; empty = one per GPU, 0 = none
```

**How many jobs at once (V0.6, D105).** The controller schedules by the slots
this laptop advertises in every Hello: `max_cpu_jobs` CPU slots and
`max_gpu_jobs` GPU slots (a `gpu: required` job takes a GPU slot; `preferred` a
GPU slot when one is free, else a CPU slot; others a CPU slot). Their sum must
not exceed `max_concurrent_attempts` (the agent refuses to start otherwise).
The controller also caps every node at `[controller.jobs] max_attempts_per_node`
(default 8). `max_cpu_jobs = 0` makes a GPU-only node: CPU jobs that nothing
else can run are refused at submit with the reason `no_cpu_slots`.

**Check it took effect.** Restart the agent, then `compute nodes <node id>` (or
`GET /v1/nodes/<node id>`) must list the capability `exec` plus `exec-docker`
and/or `exec-subprocess` / `exec-venv` (and `exec-gpu` if a GPU can be handed to jobs). The agent
log says `agent.execution.ready` with the same list, or `agent.execution.no_runtime`
with the reason (e.g. `docker_permission_denied`: add your user to the `docker`
group). A job placed on a node without `exec` is refused at submit (`409
no_feasible_plan`).

**Where things are on this laptop.** Each job runs in
`<data dir>/work/<attempt id>/` (owner-only, `0700`); its output is also written to
`.meshforge/stdout.log` / `stderr.log` there. The folder is deleted once the
controller has stored the job's final status (keep it with `keep_workdirs = true`).
Leftovers of a killed agent are removed at its next start.

**Stopping the agent while a job runs** stops the job first (within `grace_s`), tells
the controller `node_lost / agent_shutdown`, and only then disconnects. Killing the
agent instead leaves the job to the controller: the node goes OFFLINE at the lease
edge (≈ 6 s) and the job fails `node_lost` - never earlier, so a Wi-Fi blip does not
kill work. Jobs are not retried until V0.6 (G3).

- **Docker runtime (the sandbox, preferred).** Needs Docker Engine (Linux) or Docker
  Desktop with the WSL2 backend (Windows), and for GPU jobs the NVIDIA Container
  Toolkit (Linux) / Docker Desktop's GPU support. The job sees only its own folder
  (at `/work`), runs as a non-root user with no capabilities and a read-only system,
  cannot see your processes, has no network unless its manifest asks, and is capped
  in memory, processes and (optionally) CPU. It gets exactly the GPUs it was
  assigned, none otherwise. The images are pinned by digest in
  `[agent.execution.images]` (`python_cpu`, `cuda_pytorch`); a value without
  `@sha256:` is refused. The first job pulls them (the PyTorch one is ~4 GB) - pull
  ahead on a fast link with `meshforge-agent prepare` (see "Job environments" below).
- **Containers are labelled** `meshforge.agent=<node id>`. If the agent is killed, its
  containers are removed the next time it starts; you can also list them with
  `docker ps -a --filter label=meshforge.managed=1`.
- **Subprocess runtime — NOT a sandbox.** With `allow_unsandboxed = true` a laptop
  without Docker can still run jobs, but the job runs **as you**: it can read and
  change your files, see your processes and use the network. Memory, process and CPU
  caps are *not* enforced; time limits and cancel are. Turn it on only on a machine you use
  for the lab, never on one with personal data.
- **Found the controller by mDNS?** Then any machine on the Wi-Fi that pretends to be
  a controller could send this laptop work. With execution enabled, set
  `[agent] controller_address` explicitly (the agent warns with
  `agent.execution.unpinned_controller` at start when you have not). V0.7 fixes this with mTLS.
- **Running as root?** Then jobs run as `nobody` (never root) and each job folder is
  handed to that user; an explicit `run_as` other than your own uid needs a root
  agent (a normal agent refuses it with `config_error` rather than open the folder
  to every local user).
- **Controller on another address than agents dial?** Set `[controller.api]
  advertise_url = "http://<lan ip>:47110"` on the controller; otherwise agents fetch
  bundles from the host they dialled for gRPC, on the controller's REST port.
- **Windows note:** cancel on Windows has no graceful signal for a background
  process; the job is ended after `grace_s` together with everything it started (a
  kill-on-close job object — also when the agent itself dies).
- **Logs and outputs on this laptop (V0.4 G5).** Each stream's local log stops at
  `[agent.execution] max_log_bytes` (64 MiB) and ends with a
  `[meshforge: output truncated …]` line. After a successful run the declared outputs
  and both logs are uploaded to the controller (TCP 47110) with a per-job upload
  grant (at most 3 blobs / 2 GiB, `[controller.jobs] max_output_*`); a job's folder
  is kept until the controller has acknowledged its final status - the agent
  re-sends that status every `terminal_resend_s` (5 s) until it has.
- **Uploads across a controller restart (V0.6 G1).** Outputs larger than one part
  go up in parts (`[agent.execution] upload_part_bytes`, 8 MiB); if the controller
  restarts mid-upload, the agent waits for a fresh upload grant (sent after it
  reconnects) and sends only the parts the controller does not have yet. It keeps
  trying for `upload_retry_s` (120 s, counted from the first failed upload;
  `0` = fail at once); after that the job fails `output_error` (retryable).
  The two log files (best effort) get only `log_upload_retry_s` (10 s) of
  retries; a log that cannot be uploaded is reported without its final copy.
  Agent log: `agent.execution.upload_retry` (with `refused` / `refreshed`).
- **Killed agent, subprocess jobs (V0.6 G1, Linux).** Each job runs in its own
  process group watched by a tiny helper process; when the job's main process
  ends - also because the agent was killed with `kill -9` - everything it started
  is killed too. A job that deliberately detaches with `setsid` can escape it
  (not a sandbox: use the Docker runtime for untrusted code).
- **Why did it fail? (V0.6 G1).** `oom` = out of RAM (the kernel or Docker killed
  it, or Python raised `MemoryError`); `vram_oom` = a CUDA out-of-memory error in
  the job's output; `disk_full` = no space on this laptop while fetching, building
  the environment or packing outputs (an auto job is retried on another laptop,
  a pinned one is not - V0.6 D126); `env_error` = the environment could not be
  built - the controller then avoids that environment on this laptop for
  `[controller.jobs] env_bad_ttl_s` (1 h).

### 0b2. Job environments and `meshforge-agent prepare` (V0.5, D85-D87)

A job's requirements (manifest `env.requirements`) are installed **once per
laptop** into an environment named by its *env hash* and reused by every later job
with the same requirements, base and Python - the second job installs nothing.

With `allow_unsandboxed = true` the agent also offers the **venv runtime**
(capability `exec-venv`): an unsandboxed job in its own cached uv venv - its
`env.python` version and only its packages. Order of preference: docker > venv >
subprocess. The venv runtime provides only the `python-cpu` base: a `cuda-pytorch` job
(it needs torch) runs in Docker's image or, without Docker, on the agent's own
environment (subprocess runtime; install torch there with `uv sync --extra gpu`).

- **Where they are:** `<data dir>/envs/` (`[agent.envs] dir`): `venv/<hash>/` (venv
  runtime), `subprocess/<hash>/` (subprocess runtime), `uv-cache/`, `locks/`.
  Docker keeps them as images: `docker images meshforge-env`. A folder ending in
  `.building` is a build in progress (or one a crash interrupted - it is removed by
  the next build of that hash).
- **Read-only to jobs.** Environments are made read-only before first use (Docker:
  the job's root filesystem is read-only anyway). On the unsandboxed runtimes this
  is a permission guard, not isolation: a job running as you could `chmod` it back.
  On Windows only the files are read-only (Windows ignores that bit on folders, so
  a job could add files to an environment).
- **Disk.** One environment may be at most `[agent.envs] max_env_bytes` (16 GiB);
  a build is refused (`disk_full`) when less than `free_disk_floor_mb` (2 GiB) is
  free. Environments are cleaned up by GC (§0e): least recently used first, down
  to `[agent.gc] envs_bytes`, never one a running job uses.
- **Offline / mirror installs.** `[agent.envs] find_links = "/path/to/wheels"` +
  `no_index = true` installs only from a local wheelhouse (the Docker build then
  has no network at all); `index_url = "https://mirror/simple"` uses a mirror
  (no credentials in the URL).
- **What is refused** (`env_error / requirements_rejected`): `-r`/`-c`/`-e` lines,
  local paths, `file:` URLs and `name @ <anything but an http(s) URL or a git+/hg+/svn+/bzr+
  URL over http(s)/ssh/git>` (so `git+file://` is refused too), and `--index-url`/`--extra-index-url`/`--find-links`
  lines whose value is not an http(s) URL - their contents are not covered by the env hash, so two different
  environments could share one. Inline those requirements, or serve the wheel from
  `[agent.envs] find_links`. Name spelling does not matter: `Requests == 2` and
  `requests==2` share one environment. Hashed files from `pip-compile
  --generate-hashes` work (hash-checking install).
- **Damaged environments are rebuilt.** Each environment remembers its file list
  (names and sizes); one whose files no longer match (deleted, added, resized) is
  logged as `agent.envs.cache_damaged`, set aside as `<hash>.evicted-…` (deleted by a
  later build after `[agent.envs] evicted_keep_s`, 1 day) and rebuilt before any job
  uses it. Changing `find_links` (or its wheels), `index_url` or `no_index` gives new
  environments (the package sources are part of the env hash).
- **Python version (venv runtime).** `env.python: "3.11"` uses a Python 3.11
  already on this laptop (`uv python list --only-installed`); none = `python_not_found`.
  Set `[agent.envs] allow_python_downloads = true` to let uv fetch one.

**Prepare a lab laptop before a run** (local only, no controller needed):

```
meshforge-agent prepare                        # both bases
meshforge-agent prepare --base python-cpu      # CPU-only laptop: skip the ~4 GB image
meshforge-agent prepare --json
```

It prints `docker: available|unavailable: ...`, the `uv` version, and per base
`present, <size>` or `pulled, <size>, pulled in <s>` (or `failed (<reason>)`, exit
1). A laptop without Docker pulls nothing and exits 0 (the venv runtime needs no
image).

**Prepare nodes from the controller laptop** (V0.5 G5; the agents must be online
with execution enabled):

```
compute node prepare <NODE_ID> --image python-cpu --image cuda-pytorch
compute node prepare --all --envs envs.yaml      # build environments too
```

`envs.yaml`:
```yaml
envs:
  - base: python-cpu        # or cuda-pytorch
    python: "3.12"          # optional
    requirements: requirements.txt   # path relative to envs.yaml
    runtime: venv           # optional: docker | venv | subprocess
```

Each node pulls the images and builds each environment through the runtime a job
would use, so the next job with those requirements starts with `env_cache=hit`.
Output per item: `hit` (already there), `pulled` / `built` (done now) or `failed
(<failure_type>/<reason>: ...)` - e.g. `env_error/docker_unavailable`,
`no_feasible_plan/runtime_unavailable` (the node has no such runtime),
`timeout/pull_timeout`, `env_error/requirements_rejected`. Exit 1 when anything
failed. Running it again only reports `hit`. The controller remembers the newest
256 requests in memory; after a controller restart `compute node prepare` reports
the request unknown, but the work on the node is done and the next run says `hit`.

### 0e. Caches, GC and named inputs (V0.5 G5, D91-D93)

**Every laptop's caches are bounded.** An agent keeps the blobs it fetched
(`<data dir>/cas`) and the environments it built (`<data dir>/envs`, Docker images
`meshforge-env:*`). A GC pass runs at start, every `[agent.gc] interval_s` (10
min), after every job's staging, and when the controller asks; it evicts the
least recently used items while the cache is over its quota:

```toml
[agent.gc]
cas_bytes  = 20_000_000_000   # blob cache quota (0 = unlimited)
envs_bytes = 30_000_000_000   # env dirs + env images together (0 = unlimited)
min_age_s  = 300              # never evict anything used in the last 5 minutes
```

Never evicted: anything a running job uses (its project and input trees, its
environment), pinned named inputs (below), base images, and items used within
`min_age_s`. When only such items are left the agent logs `agent.gc.over_quota`
- raise the quota or free disk. The GC index is `<data dir>/gc/gc.sqlite`; it is
safe to delete it while the agent is stopped (it is rebuilt from the disk at
start; last-use times restart from "now"). Each pass logs `agent.gc.pass` with
what it evicted. `compute node gc <NODE_ID>|--all` runs a pass now.

**The controller's blob store** has the same policy (`[controller.gc] cas_bytes`,
100 GB by default, `min_age_s` 1 day): it never evicts what an unfinished job or
a pinned named input references. A finished job's outputs **can** be evicted
after a day under quota pressure - `compute get` them before (afterwards a download
can return 404). Keep `[controller.gc] min_age_s` at 10 minutes or more: it is
what protects blobs uploaded but not yet referenced by a submitted job (a slow
upload-then-submit otherwise fails with a clean 409 and must be re-run). The
quota is soft: it is enforced by GC passes, not by refusing writes. Pin the
named inputs you need - an unpinned one can be evicted once no job uses it. The store is also
**scrubbed**: every blob is re-hashed at `scrub_bytes_per_s` (32 MiB/s) once a
day, and at once when a job failed `integrity_error`; a corrupt blob is removed
(`controller.blobs.scrub_corrupt`) and the next `compute run` / `compute data
push` uploads it again.

**Named inputs** - register a dataset or model once, use it by name:

```
compute data push imagenet-mini ./data/imagenet-mini --pin   # -> imagenet-mini@1
compute data ls
compute data show imagenet-mini
compute data pin imagenet-mini@1 --off
compute data rm imagenet-mini@1        # refused while an unfinished job uses it
```

In a job's `compute.yaml`:
```yaml
inputs:
  - ref: imagenet-mini@1                # or imagenet-mini@sha256:<tree id>
    mount: data/imagenet                # a folder the project does not use
```

The input is fetched like the project (only what the node lacks, from peers
where possible) and copied **read-only** to `data/imagenet` in the job's
folder; the job reads it, cannot change it. Pushing the same folder again
uploads nothing and answers the same version; a changed folder becomes the next
version. `--pin` keeps it on every node's cache and on the controller (GC never
evicts it) - pin the inputs you use often, unpin the old versions.

**What a transfer cost:** `compute jobs <JOB_ID>` lists, per attempt and tree,
the cache hits and the bytes and time per source (controller or peer node).

### 0c. Submitting jobs and getting results (V0.4, `compute`)

From any laptop with the fabric token (§0a) and the controller's URL
(`MESHFORGE_CLI__CONTROLLER_URL=http://<controller ip>:47110`):

```bash
compute run workloads/examples/cpu-deterministic -f                    # the planner places it (V0.6)
compute run workloads/examples/cpu-deterministic --policy least_loaded --priority 5
compute run workloads/examples/cpu-deterministic --node <NODE_ID> -f   # pinned: Mode 1 on that node
compute run workloads/examples/cpu-deterministic --local -f            # pinned: this laptop's agent
compute explain <JOB_ID> [--attempt N]   # why it went where it went (V0.6)
compute jobs                       # recent jobs (--state running, --json)
compute logs <JOB_ID> [-f]         # stdout to stdout, stderr to stderr; -f follows live
compute cancel <JOB_ID>            # idempotent
compute get <JOB_ID>               # outputs -> ./outputs/<JOB_ID>/ (--dest, --force: replaces only after a verified download)
```

- `run` reads `compute.yaml` (or infers `main.py`), packs the folder (`.gitignore` /
  `.computeignore` respected), uploads it only if the controller does not have it,
  and prints the job id. It also prints an **idempotency key** on stderr: after a
  network error, re-run the same command with `--idempotency-key <key>` and you get
  the same job back instead of a second one.
- **Automatic placement (V0.6, D102-D107).** Without `--node`/`--local` the
  controller's planner picks the node on every dispatch tick: it rejects nodes
  that cannot run the job (no `exec`, no GPU / VRAM / RAM / CPU threads, no
  runtime for `env.base`, owner slot settings, recent environment failure,
  offline, slots full) and ranks the rest with the job's **policy** -
  `random`, `round_robin`, `least_loaded`, `strongest_suitable`,
  `resource_score`, `cache_affinity` or `cost_model` (default; lowest predicted
  completion time). Precedence: `--policy` > the manifest's `policy:` >
  `[controller.planner] default_policy`. A job **no** node could ever run is
  refused at once (`409 no_feasible_plan`, each node's reason printed); one that
  only has to wait (busy, offline) stays `queued`. `priority:` (-10..10, or
  `--priority`) orders the queue; waiting raises a job one level per
  `[controller.planner] priority_aging_s` (600 s), so nothing starves.
- **Local.** When this laptop runs an enrolled agent, `run` names it as the
  job's Local candidate and first copies the project into that agent's cache
  (`[cli] seed_local`, up to `seed_max_bytes`, 2 GiB; one stderr line says
  whether it did and why not) - so running here needs no download, and
  `cost_model` prefers it for a job whose transfer would dominate.
- **Retries (V0.6 G3, D110, [ADR-0015](adr/0015-retry-and-duplicate-execution.md)).**
  A job whose attempt is lost (its laptop went offline, the agent restarted
  and forgot it, the controller restarted and the laptop never came back) or
  fails with a *retryable* error (`transfer_error`, `integrity_error`,
  `timeout`, `vram_oom`, `resource_busy`, an upload blip...) goes back to
  `queued` by itself - `compute jobs` shows it queued again with a new attempt
  later. The manifest controls it:
  `retry: {max_attempts: 3, backoff_s: 5, on_lost: true}` (1-10 attempts in
  total; the wait doubles per retry up to 300 s). After the last attempt the
  job fails with the last error and `retries_exhausted after N attempts` in its
  detail. An automatically placed retry avoids the laptop it failed on for
  `[controller.planner] retry_exclude_s` (600 s; for `vram_oom`: every GPU no
  larger). When no other laptop could ever run it, the avoided laptop is used
  again as soon as it can take the job right now (V0.6 D129: a lost or
  timed-out laptop first, a too-small GPU last; a laptop that ran out of disk
  only after the 600 s) - until then the job waits `queued` rather than
  wasting attempts; a `--node`/`--local` job retries on its own laptop only. **Duplicate runs:** a laptop whose Wi-Fi drops keeps
  running its job while the retry runs elsewhere; only one result is ever
  kept, and the controller logs `controller.jobs.execution_overlap` when the
  old laptop comes back. A job that must never run twice (it sends e-mails,
  charges money...) should say `retry: {on_lost: false}`: it then fails
  `node_lost` instead of being retried after a loss.
- `compute explain <JOB_ID>` prints every candidate of every planner decision:
  rank, Local/offload, slot, score, the predicted time split into queue /
  transfer / env / compute / output **with where each number came from**
  (`measured`, `history`, `scaled`, `assumed` - V0.6 G2 still assumes the
  bandwidth and an unknown run time; G4 measures links), the reason for each
  rejected node, and - once it ran - the actual phase times next to the
  prediction (`GET /v1/jobs/<id>/decisions`, `--json`).
- `run -f` streams the logs and exits like the job: `0` succeeded; the workload's own
  exit code (1-125) when it failed; `126` when the fabric failed it (the failure type
  is printed: `node_lost`, `deadline_exceeded`, `output_error` …); `128` cancelled.
  **Ctrl-C only detaches** (exit 130) - the job keeps running; the message shows the
  `compute cancel` command.
- `get` checks the SHA-256 of what it downloads before unpacking, and unpacks as
  safely as the agent does (no paths outside the folder). Declared outputs that
  matched nothing are listed on stderr; they are not an error.
- Live logs are best effort (a flood is dropped and the gap recorded); the stored log
  becomes the complete one when the job ends. A follower that reads too slowly is
  disconnected (`slow_consumer`); `compute logs <JOB_ID>` then prints the stored log.

---

### 0d. Peer blob serving between laptops (V0.5 G4, D97-D101)

An agent can serve the job files it already caches to the other agents, so a
second laptop running the same project fetches most bytes from a nearby peer
instead of the controller. **Off by default.** To turn it on, on each agent laptop:

```toml
[agent.peer]
bind = "192.168.1.21"     # this laptop's LAN IP (never 0.0.0.0 unless allow_any_bind)
# port = 47120            # default
```

and open **inbound TCP 47120 on the LAN / private firewall profile only** - never
on a public profile, never to the internet:

```
# Linux (ufw)
sudo ufw allow from 192.168.1.0/24 to any port 47120 proto tcp
# Windows (PowerShell as administrator)
New-NetFirewallRule -DisplayName "MeshForge peer blobs" -Direction Inbound `
  -Protocol TCP -LocalPort 47120 -Profile Private -Action Allow
```

What a peer serves: read-only `GET`/`HEAD` of blobs in its own cache, only to a
request that carries a token the controller signed for that exact project (tree),
that holder, that attempt, for at most 10 minutes. The controller keeps the
signing key in `<data dir>/controller/peer_signing.key` (keep it private, like
`fabric.token`). The controller lists a peer only when the address it advertises
is the address its connection comes from; for a NAT'd lab setup set
`[controller] trust_peer_endpoints = true` on the controller.

Check it: `compute nodes <node_id> --json` (or `GET /v1/nodes/<id>`) shows
`peer_endpoint` and the `cache` counts per kind; the agent log line
`agent.cas.staged` lists per source the bytes and units it fetched and why a
source was dropped. A peer that is unreachable, slow, refuses or serves bad bytes
is dropped for that transfer and the others (the controller always among them)
finish - nothing is ever used before its SHA-256 is checked.

Tuning (`[agent.fetch]`): `per_source` requests in flight per source (2; each
holds up to one chunk in memory), `slow_source_factor` (4), `endgame_after_s`
(0.5), `request_timeout_s` (60). `[controller.jobs] max_peer_sources` (4) caps how
many peers one attempt is given.

**Network probes (V0.6 G4, D111).** With peer serving on, the controller also
measures how fast each laptop downloads from each other laptop: now and then it
asks one laptop to fetch a few MiB of zeros from another's peer port (the same
TCP 47120, a separate short-lived "probe" token). `compute network` prints the
result as a matrix (bandwidth, RTT, age; `(stale)` after an hour; `fail:<code>`
when a pair cannot reach each other - usually the firewall rule above), and the
planner uses fresh links to predict transfer times (`compute explain` shows
`transfer ... (measured)` with `bw_from`). Bounded so personal Wi-Fi is not
flooded - `[controller.network]`: `probe_interval_s` (900: each pair at most every
15 min - also across a controller restart: the stored measurement times are read
back when probing starts), `probe_gap_s` (15, jitter `probe_jitter` 0.5: one probe started at a
time), `probe_budget_bytes_per_min` (48 MiB across the fabric), `probe_bytes` /
`probe_max_bytes` / `probe_fast_s` (4 MiB, then 8 MiB if that took < 0.5 s),
`rtt_samples` (3), `busy_cpu_percent` (80: a laptop busier than this, or running
a GPU job, is not probed), `cpu_sample_max_age_s` (30: a laptop whose last
resource report is older than this is not probed either - its load is unknown,
not low), `probe_timeout_s` (60), `token_ttl_s` (60), `ewma_alpha` (0.3),
`stale_after_s` (3600; must be at least 1.5 x `probe_interval_s`, or the
controller refuses to start - otherwise every link would be stale between two
probes), `min_bw_bytes_s` (1024: a probe that measured less is refused and shown
as `fail:bw_below_floor`; the planner never predicts with less either, so no
estimate is ever infinite), `transfer_min_bytes` / `transfer_history`
(controller -> laptop speed from past downloads: the median of the last 20 of at
least 1 MiB). `probe_bytes` must be at least 1. `enabled = false` turns probing
off. On an agent: `[agent.probe] enabled` (true; false = refuse),
`request_timeout_s` (20, one request) and `deadline_s` (the whole probe; empty =
2 x `request_timeout_s` = 40 s - keep it below the controller's
`probe_timeout_s`; a probe past it reports `fail:deadline`);
`[agent.peer] probe_max_bytes` (8 MiB; 0 = answer no probes).

**Reading a transfer estimate.** `compute explain` costs every missing tree
(the project, each input) by its own source and adds the times up, so one cell
can name several: `12.40s (history, from controller (last transfer 2h ago) +
1a2b3c4d)` = part comes from the controller (speed from past downloads, the
newest of them 2 hours old - that history never goes stale, so check its age)
and part from the laptop ending `1a2b3c4d` over a probed link. `from default`
means no measurement at all for that part (`assumed`). `at the bandwidth floor`
means a stored link was slower than `min_bw_bytes_s` and was costed at that
floor. `compute explain JOB --json` lists the trees one by one
(`detail.items`).

## 1. Run it by hand first

Always get the manual run working before turning anything into a service: a
service that fails to start tells you far less than a terminal does.

**Controller laptop** (Linux recommended — it also hosts PostgreSQL):

```bash
uv sync --extra controller
docker compose -f deploy/docker-compose.dev.yml up -d --wait   # PostgreSQL
uv run meshforge-controller                                     # migrates, then serves
```

To serve the other laptops, bind the LAN address explicitly:

```bash
MESHFORGE_CONTROLLER__GRPC_BIND=192.168.1.20:47100 \
MESHFORGE_CONTROLLER__HTTP_BIND=192.168.1.20:47110 \
uv run meshforge-controller
```

**Every agent laptop:**

```bash
uv sync --extra agent
uv run meshforge-agent run --controller 192.168.1.20:47100
# or omit --controller and let mDNS find it on the LAN
```

**Check it:**

```bash
curl -sS http://127.0.0.1:47110/healthz            # {"status":"ok","sessions":N}
uv run compute status                              # URL, reachability, counts
uv run compute nodes                               # the operator table
uv run compute nodes --json | jq .                 # for scripting
uv run compute nodes node_01J...                   # one node's detail
uv run meshforge-agent hardware show               # this laptop's own probes (local only)
```

`compute` reads `[cli]` settings, so point it at another laptop with

```bash
export MESHFORGE_CLI__CONTROLLER_URL=http://192.168.1.20:47110
```

Exit codes worth scripting against: `0` ok, `2` bad usage, `4` bad configuration,
**`6` the controller could not be reached or timed out**.

---

## 2. Linux — systemd **user** unit

A *user* unit, not a system one: the agent runs as the person using the laptop,
keeps its identity in `~/.local/share/meshforge/identity`
([ADR-0013](adr/0013-node-identity-storage.md)) and needs no root.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/meshforge-agent.service      ~/.config/systemd/user/
cp deploy/systemd/meshforge-controller.service ~/.config/systemd/user/   # controller laptop only
$EDITOR ~/.config/systemd/user/meshforge-agent.service     # set WorkingDirectory
systemctl --user daemon-reload
systemctl --user enable --now meshforge-agent
```

### The one step people forget

```bash
loginctl enable-linger "$USER"
```

Without linger, **your user's systemd manager is shut down when you log out**, and
the agent dies with it; with linger the manager starts at boot and keeps running
after logout, so the laptop can contribute while nobody is logged in. This also
earns the telemetry the V0.15 planner needs (`docs/SESSION_HANDOFF.md` §0b) and
V0.3's 24-hour soak evidence.

### Day-to-day

```bash
systemctl --user status  meshforge-agent
systemctl --user restart meshforge-agent
systemctl --user stop    meshforge-agent
journalctl --user -u meshforge-agent -f              # follow
journalctl --user -u meshforge-agent --since "1 hour ago"
```

**Logs** go to the journal (the unit sets `StandardOutput=journal`). The processes
log structured JSON by default; for a readable journal on a dev laptop add
`Environment=MESHFORGE_LOG__FORMAT=console` to the unit.

**Restart policy.** `Restart=always` with `RestartSec=5`, except:

| Exit code | Meaning | Unit behaviour |
|---|---|---|
| `5` CONFLICT | another agent process owns this node identity | agent unit does **not** restart |
| `4` CONFIG_ERROR | bad settings / database unreachable at startup | controller unit does **not** restart |

Both are conditions a restart loop cannot fix; fix the cause and
`systemctl --user restart …`.

---

## 3. Windows — Task Scheduler

The agent runs **natively on Windows**, not inside WSL
([ADR-0009](adr/0009-native-windows-agent.md)), and V0.3 uses **Task Scheduler** —
no third-party service wrapper. In `cmd.exe`:

```bat
if not exist "%LOCALAPPDATA%\MeshForge" mkdir "%LOCALAPPDATA%\MeshForge"
set MF_LOG=%LOCALAPPDATA%\MeshForge\agent.log
schtasks /create /tn MeshForgeAgent /sc onlogon /rl LIMITED ^
  /tr "cmd /c cd /d \"C:\path\to\meshforge\" && uv run meshforge-agent run >> \"%MF_LOG%\" 2>&1"
```

Then:

```bat
schtasks /run   /tn MeshForgeAgent          :: start now, without logging off
schtasks /query /tn MeshForgeAgent /v /fo LIST
schtasks /end   /tn MeshForgeAgent          :: stop the running instance
```

- **Where the output goes:** the task has no console, so the command line above
  redirects stdout+stderr to `%LOCALAPPDATA%\MeshForge\agent.log`. Nothing rotates
  that file in V0.3 — check its size occasionally, or set
  `MESHFORGE_LOG__LEVEL=WARNING`. The Task Scheduler's own history (last run time,
  last result) is in `schtasks /query /v` and in Task Scheduler's UI.
- A richer template with restart-on-failure and no execution time limit is
  [`deploy/windows/meshforge-agent-task.xml`](../deploy/windows/meshforge-agent-task.xml)
  — edit the three marked fields, then
  `schtasks /create /tn MeshForgeAgent /xml "…\meshforge-agent-task.xml"`. If
  `schtasks` refuses to parse it, convert the file to UTF-16
  (`Get-Content f.xml | Set-Content -Encoding Unicode f-u16.xml`).
- Use `/rl LIMITED`: the agent needs no elevation in V0.3.

### Does it survive logoff?

**Unverified — 🧑 needs a lab check.** An `/sc onlogon` task runs inside the
interactive logon session with an `InteractiveToken` principal, and the expectation
is that Windows ends it when that user logs off (it is not a service). The project
has **not** measured this on the team's Windows laptops, and stating it as fact
without a test is exactly the kind of confidently-wrong documentation this project
forbids.

**The check (5 minutes, on a Windows agent laptop):**

1. Register the task as above and `schtasks /run /tn MeshForgeAgent`.
2. On the controller laptop: `uv run compute nodes` — this laptop is `CONN yes`.
3. **Log off** the Windows user (do not shut down, do not restart).
4. Wait 30 s, then on the controller: `uv run compute nodes` again.
5. Paste back both tables plus
   `schtasks /query /tn MeshForgeAgent /v /fo LIST` from after step 4.

*Interpretation:* `CONN no` after logoff ⇒ the task does **not** survive logoff.
*If it does not survive and you need it to:*

- **NSSM** (`nssm install MeshForgeAgent …`) is the documented fallback — it runs
  the same native Python process as a real Windows service. It is a third-party
  binary, which is why it is a fallback and not the default.
- A task with *"Run whether user is logged on or not"* (stored credentials) is the
  built-in alternative, but it runs without the interactive desktop; the agent's
  identity path (`%LOCALAPPDATA%`) then belongs to the account you configure, so
  the node may enrol as a **different** node. Verify with
  `meshforge-agent identity show` under that account before relying on it.

Either way the agent stays a **native Windows process** (ADR-0009); nothing here
moves it into WSL. Real installation and service registration is V0.17
productization.

---

## 4. Uninstall

```bash
# Linux
systemctl --user disable --now meshforge-agent
systemctl --user disable --now meshforge-controller
rm -f ~/.config/systemd/user/meshforge-{agent,controller}.service
systemctl --user daemon-reload
loginctl disable-linger "$USER"     # only if nothing else of yours needs linger
```

```bat
:: Windows
schtasks /end    /tn MeshForgeAgent
schtasks /delete /tn MeshForgeAgent /f
```

Neither removes your data. The node identity stays at
`~/.local/share/meshforge/identity` (Linux) or `%LOCALAPPDATA%\MeshForge\identity`
(Windows); delete it only if you want the laptop to enrol as a brand new node, and
prefer `meshforge-agent identity reset`. The dev database is removed with
`docker compose -f deploy/docker-compose.dev.yml down -v` (that **deletes the
volume**).

---

## 5. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `compute` prints `cannot reach the controller …` and exits **6** | controller not running, or `[cli] controller_url` points elsewhere | `systemctl --user status meshforge-controller`; check `MESHFORGE_CLI__CONTROLLER_URL`; `curl http://HOST:47110/healthz` |
| `compute status` says reachable, `nodes` is empty | no agent has ever enrolled | run the agent by hand and watch its log; check it can reach TCP 47100 on the controller |
| Agent log repeats connect/backoff | firewall on the controller laptop, or the wrong address | open inbound TCP 47100 **on the controller only**; agents need no inbound rule (except 47120 when peer serving is on, §0d) |
| `agent.cas.source_dropped` for a peer on every job | the peer's port 47120 is blocked, or it advertises an address the controller cannot reach | open TCP 47120 on the peer's LAN profile (§0d); check `peer_endpoint` in `GET /v1/nodes/<id>`; the job still succeeds from the controller |
| Agent log `agent.gc.over_quota` | everything left in the cache is in use, pinned or used in the last `min_age_s` | raise `[agent.gc] cas_bytes` / `envs_bytes`, unpin old named inputs (`compute data pin NAME@V --off`), or free disk |
| A job fails `internal_error` with reason `gc_lease_failed` | the agent could not write its GC index (`<data dir>/gc/gc.sqlite`: disk full, permissions) | free disk / fix permissions; with the agent stopped the index can be deleted (rebuilt at start). A corrupt index is moved aside automatically at start (`gc.index_quarantined`, file `gc.sqlite.corrupt-*`, safe to delete) |
| Log shows `gc.index_unusable_in_use` then `agent.gc.unavailable` / `controller.gc.unavailable`; the cache grows past its quota | the GC index is corrupt or foreign, but another live process on the same data dir still holds it, so it was not moved aside; GC is off for this process (nothing is evicted — safe, but the quota is not enforced) | stop the other process (or every process on that data dir) and restart; the next start moves the file aside and rebuilds it |
| Controller log `controller.blobs.scrub_corrupt` | a blob on the controller's disk no longer matches its name (disk error, hand edit) | nothing to do for jobs: it is removed and re-uploaded by the next `compute run` / `data push`; check the disk if it repeats |
| `compute node prepare` item `failed (no_feasible_plan/runtime_unavailable)` | the node has no runtime for that env (e.g. `runtime: docker` without Docker, `cuda-pytorch` asked of the venv runtime) | drop `runtime:` to let the node choose, or install Docker there |
| Agent log `agent.peer.start_failed` | port 47120 taken or `bind` is not this laptop's IP | free the port or set `[agent.peer] port`; the agent runs without serving peers meanwhile |
| A job fails `output_error` with "the upload did not succeed within 120 s of retries" | the agent could not reach the controller's blob port (TCP 47110), or the restarted controller never sent a new upload grant (the agent never reconnected) | check that the controller is up and the agent reconnected (`compute nodes`); raise `[agent.execution] upload_retry_s` on a slow or flaky link |
| A job is `queued` again with a second (third...) attempt | its attempt was lost or failed retryably (V0.6 G3); `compute explain <JOB_ID> --attempt N` shows why the retry went where it went (`retry_excluded` = it avoids the node it failed on) | nothing, normally. If a job ran twice and must not, give it `retry: {on_lost: false}`; check the controller log for `controller.jobs.execution_overlap` |
| Jobs fail `disk_full` | auto jobs are retried on another laptop and the full one is avoided for `retry_exclude_s` (V0.6 D126), so this means the job was **pinned** to the full laptop, or no other laptop could run it and the laptop was still full after `retry_exclude_s` (retries exhausted; D129 waits the window out instead of retrying at once) | free disk on that laptop and resubmit |
| A job stays `queued` | no node can take it **now** (V0.6): every capable node is busy (`slots_full`, `node_cap`), offline / suspect, or its environment failed there recently (`env_known_bad`) | `compute explain <JOB_ID>` lists each node's reason; free a slot, start an agent, raise `[agent.execution] max_cpu_jobs` / `max_gpu_jobs`, or cancel it |
| `compute run` exits with `no_feasible_plan` and a list of nodes | **no** enrolled node could ever run it (V0.6 D104): no `exec`, no GPU / too little VRAM / RAM / CPUs, no runtime for `env.base`, the owner allows no jobs of that kind | each line names the node and the reason; change the manifest's `resources:` or enable execution / a runtime on a node |
| `compute run` prints `local agent cache not seeded (...)` | the CLI could not copy the project into this laptop's agent cache: `no_cache` (the agent never ran with this data dir), `not_writable`, `over_quota` (project > `[cli] seed_max_bytes`), `disk_full`, `changed` (a file changed while copied) | nothing breaks: a job placed here downloads the project; fix the reason to save that download |
| Controller exits **4** at startup, log `controller.start_failed` names `controller/ca` or `controller/tls` | the fabric CA or its folder cannot be trusted (V0.7 G1): loose permissions, a missing half, a garbled file, a key that does not match | follow the message: `chmod 600` / `chmod 700`, restore `ca/` from backup, or `meshforge-controller fabric create --force` (every laptop re-joins) — §0a2 |
| Controller exits **4** at startup | PostgreSQL unreachable or bad `[database]` settings | `docker compose -f deploy/docker-compose.dev.yml up -d --wait`, then check `[database]` |
| Agent exits **5** immediately | another agent process already owns this identity | stop the other one, or give this one its own `data_dir` |
| A node shows `STATE suspect` and `CONN no` | the node's connection dropped without a goodbye (Wi-Fi blip, laptop suspended, agent killed) | Normal for a few seconds: the controller keeps the node's lease running and marks it `offline` when the lease runs out (3 × heartbeat, 6 s by default, from the last heartbeat) unless the agent reconnects first — then it goes straight back to `online`. `GET /v1/sessions?node_id=…` shows that session's `end_reason` as `stream_lost`. A cleanly stopped agent (`systemctl --user stop`, Ctrl-C) is `offline` at once with end reason `agent_closed` (ARCHITECTURE §12.3, D19) |
| A node shows `STATE online` but `CONN no` for more than a moment | the controller cannot write to PostgreSQL, so the `offline`/`suspect` it decided in memory is still queued | Check the controller log for `registry.write_failed`; the queued writes land in order once the database answers. `CONN` / `connected` is in-memory and right meanwhile (D16) |
| `sample_age_s` / `AGE` keeps growing | the agent's resource monitor stopped, or reports are being suppressed | check the agent log for `agent.resources.*` events |
| `systemctl --user` works but everything dies at logout | linger not enabled | `loginctl enable-linger "$USER"` |

For what the three liveness fields mean (`STATE`, `CONN`, `AGE`) see
[`ARCHITECTURE.md` §12.3](ARCHITECTURE.md) and the module docstring of
`meshforge/controller/api/models.py`.
