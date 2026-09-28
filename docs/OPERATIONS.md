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

The agent needs **no inbound port** at all — it dials out
([ADR-0002](adr/0002-agent-initiated-grpc-session.md)). Only the controller laptop needs
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
```

**Check it took effect.** Restart the agent, then `compute nodes <node id>` (or
`GET /v1/nodes/<node id>`) must list the capability `exec` plus `exec-docker`
and/or `exec-subprocess` (and `exec-gpu` if a GPU can be handed to jobs). The agent
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
kill work. Jobs are not retried until V0.6.

- **Docker runtime (the sandbox, preferred).** Needs Docker Engine (Linux) or Docker
  Desktop with the WSL2 backend (Windows), and for GPU jobs the NVIDIA Container
  Toolkit (Linux) / Docker Desktop's GPU support. The job sees only its own folder
  (at `/work`), runs as a non-root user with no capabilities and a read-only system,
  cannot see your processes, has no network unless its manifest asks, and is capped
  in memory, processes and (optionally) CPU. It gets exactly the GPUs it was
  assigned, none otherwise. The images are pinned by digest in
  `[agent.execution.images]` (`python_cpu`, `cuda_pytorch`); the first job pulls them
  (the PyTorch one is ~4 GB). Pull ahead with `docker pull <image>` on a fast link.
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

---

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
| Agent log repeats connect/backoff | firewall on the controller laptop, or the wrong address | open inbound TCP 47100 **on the controller only**; agents need no inbound rule |
| Controller exits **4** at startup | PostgreSQL unreachable or bad `[database]` settings | `docker compose -f deploy/docker-compose.dev.yml up -d --wait`, then check `[database]` |
| Agent exits **5** immediately | another agent process already owns this identity | stop the other one, or give this one its own `data_dir` |
| A node shows `STATE suspect` and `CONN no` | the node's connection dropped without a goodbye (Wi-Fi blip, laptop suspended, agent killed) | Normal for a few seconds: the controller keeps the node's lease running and marks it `offline` when the lease runs out (3 × heartbeat, 6 s by default, from the last heartbeat) unless the agent reconnects first — then it goes straight back to `online`. `GET /v1/sessions?node_id=…` shows that session's `end_reason` as `stream_lost`. A cleanly stopped agent (`systemctl --user stop`, Ctrl-C) is `offline` at once with end reason `agent_closed` (ARCHITECTURE §12.3, D19) |
| A node shows `STATE online` but `CONN no` for more than a moment | the controller cannot write to PostgreSQL, so the `offline`/`suspect` it decided in memory is still queued | Check the controller log for `registry.write_failed`; the queued writes land in order once the database answers. `CONN` / `connected` is in-memory and right meanwhile (D16) |
| `sample_age_s` / `AGE` keeps growing | the agent's resource monitor stopped, or reports are being suppressed | check the agent log for `agent.resources.*` events |
| `systemctl --user` works but everything dies at logout | linger not enabled | `loginctl enable-linger "$USER"` |

For what the three liveness fields mean (`STATE`, `CONN`, `AGE`) see
[`ARCHITECTURE.md` §12.3](ARCHITECTURE.md) and the module docstring of
`meshforge/controller/api/models.py`.
