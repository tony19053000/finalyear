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

> **Since V0.7 G4** both ports are TLS (G3) and every REST call but `/healthz`
> and the login needs a **user's API token** (§0a1). The two paragraphs above
> describe V0.3; §0a (the shared fabric token) is now a legacy switch, off by
> default.

### 0a1. Users, logins and API tokens (V0.7 G4, D141/D148)

Every person who uses `compute` has their **own account** on the controller.
An admin can invite laptops, revoke nodes and see every job; a normal user sees,
cancels, follows and downloads **only their own jobs** (someone else's job looks
exactly like one that does not exist: "no such job").

**First admin** (on the controller laptop - it writes the database directly, so
the controller does not even have to run):

```bash
meshforge-controller user create alice --admin      # asks for the password twice
meshforge-controller user create bob                # a normal user
meshforge-controller user list
```

Passwords are 8-1024 characters and are stored only as an scrypt hash. Scripts
can pass one on stdin (`--password-stdin`, first line) - never as an argument.

**Log in from any laptop** (the admin reads the CA fingerprint once with
`meshforge-controller fabric show` and tells the user, e.g. by reading it out):

```bash
compute login --controller https://192.168.1.20:47110 --fingerprint "ab12 cd34 …"
#   User: bob   Password: ********
compute whoami
compute run ./my-project -f
```

`compute login` checks the controller **before** it sends your password: with
`--fingerprint` it reads the fabric CA from the TLS handshake and keeps it only
if its full SHA-256 fingerprint matches (and it signed the controller's
certificate) - otherwise it stops ("does not hold the fabric CA with that
fingerprint … nothing was sent"). On a laptop that already has the CA (an
enrolled agent, or `--ca ca.crt`) no fingerprint is needed. The login stores the
API token and `ca.crt` in `~/.config/meshforge/compute/` (Windows
`%APPDATA%\MeshForge\compute\`), readable by you only; every later `compute`
command uses them and the controller URL you logged in to. Override the folder
with `[cli] credentials_dir`.

**Open log follows** (`compute logs -f`, the dashboard later) re-check their
token every `[controller.auth] stream_recheck_interval_s` (default 1 s, one
database query for all open streams): a revoked or expired token or a disabled
user closes the stream within about a second (`4401`); if the database cannot
answer 5 checks in a row every stream is closed `4503` (log in / retry later).
If the re-check loop itself keeps failing, `GET /healthz` answers `"status":
"degraded"` (V0.7 G5, D159 N4) - look for `controller.auth.stream_recheck_crashed`
in the log. Streams opened with the legacy fabric token are **never** re-checked.

**Part uploads** (big outputs, `compute data push`): one user or laptop may have
at most `[controller.blobs] max_resumable_per_owner` (16) unfinished part
uploads at once, under the store-wide 64; past it new ones get `503
resource_busy` while everyone else can still upload (V0.7 G5, D158 T-4).

**Tokens** last 90 days (`[controller.auth] token_ttl_days`). `compute tokens
list` shows yours (`--all`: everyone's, admins only), `compute tokens revoke
TOKEN_ID` ends one, `compute logout` revokes the current one and forgets it. A
token that expired or was revoked, or a disabled user, gets `401 … run compute
login` on the very next call.

**Remedies:**

| Situation | Do this |
|---|---|
| Forgotten password | admin: `meshforge-controller user reset-password NAME` (also ends every token of that user) |
| A laptop with a login was lost | `compute tokens revoke TOKEN_ID` from another laptop (or the admin with `--all`), or disable the user |
| Someone leaves | `meshforge-controller user disable NAME` (their tokens stop at once; `enable` lets them log in again) |
| `429 too many failed logins` | wait the `Retry-After` seconds: after 5 wrong passwords a name is blocked **from that address** (IPv4 address, or IPv6 /64) for 1 s, doubling to 5 min - the same user logging in from their own laptop is not blocked (V0.7 G5, D159 N1); one address is limited to 10 tries a minute (`[controller.auth] login_*`). Tokens already issued keep working |
| A user name with an accent or other non-ASCII letter | user names are ASCII only (`a-z 0-9 . _ -`); a look-alike spelling (e.g. the Kelvin sign for `K`) is just an unknown user (V0.7 G5, D158 T-5) |
| Old scripts still send the fabric token | turn on `[controller.auth] legacy_fabric_token = true` **temporarily**: the fabric token then acts as an admin, with a warning in the log and an audit row; move the scripts to `compute login` and turn it off again (it is removed in V0.8) |
| `meshforge-controller` exits: "the database password is the published development default" | see "Database password" below |

**Database password (D145 d).** A controller in `[security] mode = "mtls"`
refuses to start while its database password is the published dev default
`meshforge-dev`. Before the dev database's first start put a strong one in
`deploy/.env` (`MESHFORGE_DEV_DB_PASSWORD=…`) - or change it later with
`ALTER ROLE meshforge PASSWORD '…'` in `psql` - and give the controller the same
value (`MESHFORGE_DATABASE__PASSWORD=…` or `[database] password` in its
`config.toml`, which must then be private to you).

### 0a1b. Job secrets (V0.7 G6, D144)

A job that needs a password or an API key reads it from a **secret** you stored
on the controller - never from the project, the manifest or the command line.

```bash
compute secret set HF_TOKEN              # type it (not echoed), or pipe it:
printf '%s' "$HF_TOKEN" | compute secret set HF_TOKEN   # one trailing newline is dropped
compute secret set CERT --from-file ./client.pem       # exact bytes of a file
compute secret list                      # names and when they were set - never values
compute secret delete HF_TOKEN
```

```yaml
# compute.yaml
secrets:
  - HF_TOKEN                     # file: /run/secrets/HF_TOKEN (Docker) or $MESHFORGE_SECRETS_DIR/HF_TOKEN
  - {name: DB_PASS, as_env: true}  # also the environment variable DB_PASS
```

- **Rules.** Names: letters, digits and `_`, not starting with a digit
  (`hf_token` is stored as `HF_TOKEN`); values 4 bytes .. 64 KiB. Shorter values
  are refused because every log is scrubbed of each value (a 2-byte value
  would blank half of every log). **One job may name at most 256 KiB of secret
  values in total** (`[controller.jobs] max_job_secret_bytes`, 4 B .. 1 MiB):
  more is refused at submit - and when the job is about to start, if a value
  grew meanwhile - with `invalid_input` `secret_bytes_over_limit`; the cap keeps
  log scrubbing cheap on the controller and on the laptop. `as_env` needs text (UTF-8, no NUL) and may
  not be `PATH`, `HOME`, `PYTHONPATH`, `LD_*` or `MESHFORGE_*`.
- **Only your own.** A job can name only the secrets of the user who submits
  it. A missing one is refused at submit (`invalid_input`
  `secret_missing:NAME`); one deleted while the job waits fails it when it is
  about to start (same message, nothing reaches the laptop, not retried).
- **On the laptop** the values exist only while the attempt runs: files in a
  private folder in RAM (`/dev/shm` on Linux), read-only for Docker jobs,
  wiped (overwritten, deleted) when the attempt ends - also on failure, cancel
  and agent stop; a crashed agent's leftovers are wiped at its next start.
  **The folder is checked before every use** (V0.7 G6 fix round 3): the
  agent's secrets folder (`/dev/shm/meshforge-secrets-<uid>-<tag>`, or the
  one under `secrets_root` / `<data dir>/secrets`) must be a real directory,
  owned by the agent's user, mode `0700`. `/dev/shm` is shared by all users
  of the machine, so someone else can create that name first - as a link to
  one of your folders, for example. The agent then touches nothing in it:
  the log says `agent.execution.secrets_base_refused` with
  `reason = symlink | not_a_directory | foreign_owner | wrong_mode |
  unreadable`, jobs **with secrets fail** `config_error`
  `secrets_base_refused:<reason>` on this laptop, jobs without secrets run.
  Fix: look at the folder (`ls -ld /dev/shm/meshforge-secrets-*`), remove
  what is not yours to keep (`rm` the link; `rmdir` or `chmod 700` your own
  folder) - the next job works, no restart.
  **If writing them hangs** (a stale mount under `secrets_root`): a cancel or
  an agent stop waits for that write at most `[agent.execution]
  secret_write_wait_s` (10 s) and then goes on; the log says
  `agent.execution.secret_write_pending`. The files are not forgotten: the
  moment the hung write returns, the same thread wipes what it wrote. Until
  then the files it already wrote exist (private to your account, in RAM on
  Linux) - accepted, because nothing can remove a file a hung filesystem call
  is still writing; if the agent is killed first, its next start wipes them.

  > **Not in RAM everywhere.** On Windows and macOS, and on a Linux machine
  > without `/dev/shm`, the secret files are **on disk** for the attempt's
  > lifetime: `<data dir>/secrets/...` (Windows:
  > `%LOCALAPPDATA%\MeshForge\secrets`). They are private to your
  > account and overwritten before they are deleted, but a disk can keep
  > copies an overwrite does not reach (SSD wear levelling, a journal, a
  > snapshot, a backup or sync tool watching the folder). Use full-disk
  > encryption on such a laptop, keep the data dir out of synced/backed-up
  > folders, or point `[agent.execution] secrets_root` at a RAM disk.
 They travel only inside the mTLS
  session (`secrets_need_mtls` in `insecure-dev` unless `[security]
  allow_insecure_secrets = true`, for one-machine tests only).
- **Logs** show `[secret:NAME]` where a value (or its base64 / hex /
  URL-encoded form) was printed - on the laptop, in `compute logs`, in the live
  follow and in our own log files. When two values overlap in the output (one
  ends inside the next), the whole stretch is replaced by **one** marker, named
  after the value that starts first; `compute logs` pads it with `*` to the
  original length. **This stops accidents, not a job that leaks
  its secret on purpose** (reversed, split into pieces, written to an output).
  **Also NOT redacted** - only the exact value and the encodings above are:
  - a secret printed JSON- or `repr`-escaped: a multi-line or quoted secret
    (a PEM key) through `json.dumps(...)`, a `repr()` of a bytes secret, a
    structured log line - the escapes (`\n`, `\"`, `\x..`) change the bytes;
  - a secret embedded in a larger base64 blob: HTTP Basic `user:secret`, a
    base64 of a config file that contains the secret (the secret's own base64
    does not appear inside it);
  - any other transformation (base32, double encoding, gzip, a hash is fine).
  A job that logs configuration as JSON should leave its secrets out of it.
- **A log full of `*`** with the line `[meshforge: log redaction failed
  closed - output withheld]` (V0.7 G6 fix round 3) means the scrubber hit an
  internal safety stop for that job and withheld the output rather than risk
  showing a value. The job itself is not affected. The agent or controller
  log has one warning per stream (`agent.execution.log_redaction_failed_closed`
  / `controller.logs.redaction_failed_closed`, with the job and attempt id) -
  please report it with the shape (not the value) of the job's secrets; it is
  a bug in MeshForge. In `compute logs` the note is written over the first
  withheld bytes (the stored log keeps its length); on the laptop it is a
  line of its own. **The log is the only place it shows** (D165 (4)): the
  job and attempt records (`compute jobs <JOB_ID>`, `compute explain`,
  `GET /v1/jobs/...`) have no field for it and the job's state is whatever
  the workload's was - so read the log of a job whose output matters, and
  look for that line, before trusting an empty-looking result.
- **Changing a value:** `compute secret set NAME` again. Running attempts keep
  the value they started with; the next attempt (a new job or a retry) gets the
  new one.
- **The master key** `<data dir>/controller/secrets.key` (32 bytes, created on
  first use, `chmod 600`, the controller refuses a key others can read) encrypts
  every value in the database (AES-256-GCM; the table holds ciphertext only).
  **Back it up together with `controller/ca/`**, and keep the backup as private
  as the CA key: losing it loses every stored secret (users must `secret set`
  again); a key from another installation is refused (`secret_key_mismatch`).
  Rotating the master key is **not** available before V0.17 (it comes with CA
  rotation). A controller (or its administrator) can read every secret - it
  must, to deliver them.

### 0a. The fabric token (V0.4, D44 — [ADR-0014](adr/0014-interim-execution-trust.md)) - legacy since V0.7 G4

> Since V0.7 G4 the controller **ignores** the fabric token unless
> `[controller.auth] legacy_fabric_token = true` (default false); use
> `compute login` (§0a1). The rest of this section describes V0.4-V0.7 G3.

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
- **Since V0.7 G3 every plane is TLS** (`[security] mode = "mtls"`, the default):
  the session port 47100 needs a node certificate, the REST/blob port 47110 is
  HTTPS (TLS 1.3), peer servers 47120 are TLS with a required client certificate.
  **After upgrading, every lab laptop must re-join once** (§0a3) - an agent without
  a certificate stops with `this laptop is not enrolled ... run meshforge-agent
  join`. The controller refuses to start without a CA (run `fabric create`).
  Revoking a laptop: §0a2c. Still to come: per-user logins (G4).

### 0a1d. The dashboard in a browser (V0.8 G1, D169/D170 - [ADR-0017](adr/0017-dashboard-architecture.md))

The controller serves the dashboard at `https://<controller>:47110/` (the same port as the API). Until the
frontend is built (`cd frontend && npm ci && npm run build`, V0.8 G2) that address shows a plain "dashboard not
built" page; the API and `compute` work either way.

**Trust the fabric CA once per machine** - never click through the browser's warning:

```
meshforge-controller ca trust-helper            # path + SHA-256 of ca.crt, steps for Linux, Windows, macOS
meshforge-controller ca trust-helper --os windows
meshforge-controller ca trust-helper --pem > ca.crt   # the (public) certificate, to copy to that machine
```

It only prints; you run the steps, after checking that the fingerprint the OS shows equals the printed one.
On the controller host itself you may instead set `[controller.http] loopback_plain = "127.0.0.1:47111"` and open
`http://127.0.0.1:47111` - plain HTTP that never leaves the machine (only `127.0.0.1` or `[::1]` is accepted; any
other value stops the controller at start). Open it as `localhost`, `127.0.0.1` or `[::1]`: any other host name
(a hosts-file alias, a DNS name pointing at 127.0.0.1) is refused with 400 - that blocks DNS-rebinding attacks.

**Logging in.** The dashboard asks for the same user name and password as `compute login`. It never receives an
API token: it gets a *session* cookie (`HttpOnly`, `Secure`, `SameSite=Strict`) that ends after **12 h unused**
or **7 days** in any case (`[controller.auth] session_idle_hours`, `session_max_days`), at logout, or when the
user's password is reset or the user disabled. Sessions show up in `compute tokens list` as `browser_session`
and can be revoked there like any token (`compute tokens revoke tok_...`); open dashboard tabs then lose their live
stream within a second. Failed dashboard logins count against the same per-address / per-user limits as
`compute login`.

**Security headers.** Every response carries a strict `Content-Security-Policy` (only the controller's own
scripts, styles, fonts and connections; no frames), `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`. If the controller is ever put behind a reverse proxy,
keep them and keep the proxy on the same origin (ADR-0017 "revisit").

**Live updates** come over one WebSocket (`/v1/events/ws`). The bus is in memory: a controller restart makes
every open dashboard reload its data (`resync`). Tuning (rarely needed): `[controller.events] ring_size`
(10 000 events kept for reconnects), `queue_size` (1 000 per tab - a tab that falls further behind is
disconnected and reconnects), `max_subscribers` (256), `watch_interval_s` (0.25), `metrics_interval_s` (1).

### 0a1c. Running the security suite (V0.7 G7)

Every security claim in `docs/THREAT_MODEL.md` names the test that proves it. To check the claims
on your machine:

```bash
# criterion 5: every threat-model row is closed, every named test exists and is marked (no DB/Docker; ~5 s)
uv run python scripts/threat_model_check.py
# the fast part of the security suite (no Docker)
uv run python scripts/pytest_exit_guard.py -m "security and unit" -q
# the whole security suite: unit + component + cluster (needs Docker for PostgreSQL; ~10 min)
uv run python scripts/pytest_exit_guard.py -m security -q
```

`threat_model_check` prints one line per row - ID, status, how many of its tests are collected
under `-m security`, and the problem if any - and exits 1 when a row is open, a named test is
missing or not marked `@pytest.mark.security`, or an accepted row is not listed in the accepted-risks
table. The real-network half (four laptops, packet captures, a Windows agent) is the 🧑 lab
checklist `lab/checklists/V0.7-security.md`.

### 0a2b. TLS operations (V0.7 G3)

- **`compute` and the CA.** `compute` talks `https://` only. It trusts the fabric
  CA from, in order: `--ca PATH`, `[cli] ca_file`, this laptop's agent enrolment
  (`<data dir>/identity/ca.crt`), the controller's own (`<data dir>/controller/ca/ca.crt`).
  On a laptop with neither, copy `ca.crt` from the controller and check its
  fingerprint against `meshforge-controller fabric show`. Old configs with
  `http://...:47110` are refused with the fix (`https://...`).
- **Server certificate renewal** happens while the controller runs (every
  `[controller.tls] check_interval_s`, default 1 h; renewal at 2/3 of its 1-year
  life). Log `controller.tls.server_cert_reloaded`; nothing restarts, open sessions
  continue. A failed check logs `controller.tls.renew_failed` and keeps serving the
  current certificate - fix the cause (disk, permissions) before it expires.
- **`insecure-dev`** (`[security] mode = "insecure-dev"`) is for one development
  machine only: plaintext, the agent's claimed identity trusted, every address must
  be loopback (anything else is refused at start) and every start logs
  `security.insecure_dev`. Never use it on a network.
- **Enrolment port.** Join codes keep pointing at the enrolment listener (47101):
  open it on the LAN firewall profile next to 47100/47110/47120.
- **A cancelled HTTPS request never leaks its socket** (V0.7 G6): the agent opens
  every HTTPS connection (bundles, trees, outputs, peer fetches, probes) through
  a cancel-safe connect (`agent/http_connect.py`). One narrow case is accepted
  (D165 (2)): a connect or TLS handshake still in progress at the moment the
  agent's event loop itself shuts down is cancelled by the loop and its socket
  may be left to the operating system. It happens only while the agent process
  is exiting, and the OS closes every socket of a process that ends - no action
  needed; a `ResourceWarning: unclosed <socket>` in a test run at shutdown is
  this case.

| Symptom | Cause | Remedy |
|---|---|---|
| agent exits 4: `not enrolled in a fabric` | no `node.crt` (fresh laptop, or upgraded from before G3) | `meshforge-agent join` with a new code |
| agent exits 4: `node certificate is valid ... not now` | certificate expired (laptop off > 30 days) or the clock is wrong | fix the clock; else `meshforge-agent join --force` with a new code |
| agent keeps retrying, controller log `session.cert_refused` / agent `UNAVAILABLE ... handshake` | the CA was replaced (`fabric create --force`), or this is another fabric's controller | re-join with a code from this controller |
| controller log `session.cert_superseded` | the node was re-keyed (`invite --replace`) and the old laptop/cert still connects | re-join that laptop (or stop the old one) |
| controller exits 4: `no fabric CA` | mtls mode on a controller without a CA | `meshforge-controller fabric create` |
| `compute`: `no fabric CA certificate to verify the controller` | the CLI found no `ca.crt` | `--ca PATH` or `[cli] ca_file` |
| `compute`: `cli.controller_url must be https://` | an old `http://` config | write `https://host:47110` |
| agent exits (code 4) logging `agent.revoked` with `node_revoked: ...` (no traceback, V0.8) | this node was revoked | the node id is gone for good; to bring the laptop back: `meshforge-agent identity reset --yes`, then `meshforge-agent join` with a new code (a new node) |
| agent exits with `PERMISSION_DENIED ... cert_revoked` | its certificate was replaced by a newer enrolment of the same node (or the node was revoked) | re-join that laptop with `meshforge-agent join --force <code>`; if the node was revoked, as above |
| `join` refused `node_revoked` | no code, not even `--replace`, brings a revoked node id back | `meshforge-agent identity reset --yes`, then join with a new code |
| peers log `agent.peer.client_refused reason=peer_cert_revoked` | a revoked (or re-keyed) laptop still tries to fetch | expected; stop or re-join that laptop |
| agent exits with `PERMISSION_DENIED ... cert_superseded` (controller log `session.cert_superseded_cut`) | this laptop's certificate was replaced by a newer enrolment of the same node (`invite --replace` + join on another machine); its open session is cut within `[controller.revocation] poll_interval_s` (1 s) and its jobs are retried elsewhere | expected on the replaced (lost/stolen) machine; if this laptop should stay, `meshforge-agent identity reset --yes` and join with a new code (a new node) |
| agent log `UNAUTHENTICATED ... cert_expired` (controller log `session.cert_expired`), then the agent stops with `node certificate is valid ... not now` | the node certificate reached its expiry while the session was open: the controller closes a session at its certificate's `not_after` | fix the clock if it is wrong; else `meshforge-agent join --force` with a new code (G4 renews certificates before that) |
| peers log `agent.peer.refused reason=wrong_requester` (or `probe_wrong_requester`) | a peer token was presented by a node other than the one it was minted for (a captured/forwarded token, or a broken client) | expected for a stolen token - nothing is served; if it repeats for a real fetch, report it (the fetching agent must present its own node certificate) |
| peers log `agent.peer.refused reason=expired` or `reason=too_long` (probes: `probe_expired` / `probe_too_long`) for every fetch from one laptop, transfers fall back to the controller | that peer's clock is off: peer tokens live 60 s and the holder allows 30 s of skew | sync the laptop clocks (NTP / Windows time service on); see "Clock skew" below |

### 0a2d. Certificate renewal and new keys (V0.7 G4, D140)

- **Automatic.** A node certificate lives 30 days (`[controller.renewal]
  node_cert_lifetime_s`). The running agent renews it at two thirds of that (day
  20) over its mutual-TLS session, swaps `node.crt` atomically and reconnects at
  once on the new certificate - jobs keep running (the controller sees a short
  reconnect, never a lost node). Logs: agent `agent.cert.renewed` then
  `agent.session.handover`; controller `controller.cert.renewed`, then
  `session.cert_activated` (the old certificate is retired from then on).
  `meshforge-agent identity show` prints `cert_renewal_due`.
- **A new key** (`meshforge-agent rotate-key`, node_id unchanged): stages a key;
  the running agent (or the next start) renews with it within
  `[agent.cert] renew_check_interval_s` (10 min) and switches key, `node.json` and
  certificate together. `rotate-key --cancel` drops a staged key not yet used.
  Two certificates for one node are at most one per
  `[controller.renewal] min_interval_s` (10 min): a rotation right after a
  renewal waits that long (the agent retries on its own).
- **A laptop that was off for more than 30 days** cannot renew (its certificate
  expired): the agent stops with `node certificate is valid ... not now` -
  `meshforge-agent join --force` with a new code. Turn laptops on at least every
  ~20 days to avoid that.

| Symptom | Cause | Remedy |
|---|---|---|
| agent log `agent.cert.renew_failed reason=rate_limited` | a renewal sooner than `min_interval_s` after the last one (e.g. `rotate-key` right after a renewal) | nothing - the agent retries after the time the controller names |
| agent log `agent.cert.renew_failed reason=unavailable` | the controller's database was unreachable | fix the database; the agent retries (doubling up to the check interval) |
| agent log `agent.cert.renew_failed reason=bad_response_or_storage` | the answer did not match this laptop (another CA, key or node) or the identity folder is not writable | check disk/permissions of the identity folder; a wrong answer means the wrong controller - report it |
| agent stops `PERMISSION_DENIED ... cert_superseded` after a renewal | another machine holds this node's newest certificate (`invite --replace`) | as in the table above: re-join this laptop or retire it |

### 0a2c. Revoking a laptop (V0.7 G3, D138)

A stolen, lost or retired laptop is taken out of the fabric with **one** of:

```bash
compute node revoke node_...                       # over HTTPS (fabric token), immediate
meshforge-controller node revoke node_...          # on the controller host, straight into the database
```

What happens: the node is marked `revoked` (an audit row names who did it), its
certificate serial joins the fabric's revocation set, its live session is cut at
once (`PERMISSION_DENIED`, the agent stops and prints how to come back) - within
`[controller.revocation] poll_interval_s` (1 s) for the controller-host command -
and every job running on it is retried on another node (attempt `lost`, detail
`node_revoked`). Every agent is told the serial and its peer server refuses that
certificate from then on; the HTTPS port refuses it too. Revoking twice is harmless.

**There is no un-revoke.** A revoked node id is refused by every enrolment,
`invite --replace` codes included. To bring the same laptop back it takes a new
identity: `meshforge-agent identity reset --yes` on it, then `meshforge-agent join`
with a new code - it becomes a new node (the revoked one stays in the history).
Re-joining a laptop that was *not* revoked (`join --force`) retires its previous
certificate the same way (`superseded`): only the newest certificate of a node
works on any plane.

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

### 0b0. What this laptop allows - the owner policy (V0.7 G5, D142)

The **laptop's owner decides** what runs on it - not the controller. The agent
tells the controller its policy, the planner avoids laptops whose policy refuses
a job, and the agent **checks every job again** before it starts it (so even a
wrong or compromised controller cannot make it run something the owner did not
allow). In `config.toml`:

```toml
[agent.policy]
accept_jobs = true          # run jobs at all (default false; was [agent.execution] enabled)
allow_unsandboxed = false   # also the subprocess/venv runtimes - NO sandbox (see below)
allow_network = false       # a job asking `limits.network: on` may get a network
allowed_users = []          # user names allowed to run here; empty = everyone
# max_ram_bytes = 8589934592   # most RAM one job may use (refused above; also its memory limit)
# max_cpu_percent = 50         # share of this laptop's CPU threads one job may use
# max_vram_bytes = 4294967296  # most VRAM a GPU job may ask for (placement only: no runtime caps VRAM)
# max_cpu_jobs = 1, max_gpu_jobs = (empty: one per GPU)
# accept_gpu_sessions = false  # Remote-GPU sessions (used from V0.11)
# reload_interval_s = 1.0
```

**Change it live, no restart:**

```bash
meshforge-agent policy show                         # what is in force, and where each value comes from
meshforge-agent policy set max_ram_bytes=8GiB max_cpu_percent=50 allowed_users=alice,bob
meshforge-agent policy set allow_network=true
meshforge-agent policy set max_ram_bytes=none       # remove a limit
meshforge-agent policy reset                        # back to config.toml
```

`policy set` checks the whole new policy first (a bad value changes nothing),
then writes `<data dir>/policy.toml` (owner-only). The running agent applies it
within `reload_interval_s` and tells the controller (`agent.policy.changed` in
the agent log, `session.policy_updated` in the controller's). If that file is
broken, unreadable or readable/writable by other users, the agent **accepts
nothing** until it is fixed (`agent.policy.invalid`, and `policy show` says
INVALID) - it never falls back to something looser.

- A tighter policy **does not stop jobs already running** on this laptop
  (cancel them with `compute cancel` if needed); it blocks the next start.
- Two changes need **one agent restart**: turning `accept_jobs` on when the
  agent was started with it off, and turning `allow_unsandboxed` on when it was
  started off - the runtimes are found at start (`policy set` prints a note
  for each). The restart reads the configuration **plus** the live
  `policy.toml`, so `policy set accept_jobs=true allow_unsandboxed=true` then
  one restart is enough (V0.7 G5 fix r1). A broken `policy.toml` at start: the
  runtimes are probed from the configuration alone (log
  `agent.policy.invalid_at_start`) and every job is refused until the file is
  fixed. Turning either **off** is live.
- The agent also re-checks the file after a `chmod`/`chown` alone: making
  `policy.toml` readable or writable by others makes the laptop accept nothing
  until it is `0600` again.
- **Why was my job refused?** `compute explain JOB` (or the 409 at submit)
  names the code per laptop: `policy_not_accepting`, `policy_user_not_allowed`,
  `policy_ram`, `policy_cpu`, `policy_vram`, `policy_unsandboxed`,
  `policy_network`. A job no laptop allows is refused at submit, or fails
  `no_feasible_plan` if every laptop's policy changed while it waited - it
  never hangs. If a laptop's agent refuses a job the controller sent anyway,
  the job fails `unauthorized` with `policy_denied:<code>` and is **not**
  retried.
- **Network (D143):** from V0.7 a job gets **no network** unless its manifest
  asks `limits.network: on` **and** the laptop's owner set `allow_network =
  true`. `limits.network: default` now means none. (Installing a job's
  requirements still uses the network when the environment is built - only the
  requirements file is in that build container; set `[agent.envs] no_index =
  true` + `find_links` for no network at all.)
- **Which caps are really enforced, where (V0.7 G5, D142 item 5).** The
  limits are applied by the runtime, not only used for placement:

  | Cap | Docker (all OSes) | subprocess/venv on Linux | subprocess/venv on Windows | other OS |
  |---|---|---|---|---|
  | `max_ram_bytes` | `--memory` (no swap) | `RLIMIT_AS` = the cap (address space, stricter than RAM); **not** for GPU jobs | job-object memory limit; **not** for GPU jobs | not enforced |
  | `max_cpu_percent` | `--cpus` | a per-job cgroup `cpu.max` when the agent runs **delegated** (the systemd user unit with `Delegate=yes` + `DelegateSubgroup=agent`, systemd >= 254; V0.7 G6); otherwise **not enforced** (lowered priority only) | job-object hard CPU-rate cap | not enforced |
  | processes (`default_pids`) | `--pids-limit` | `RLIMIT_NPROC`: at most `default_pids` more processes/threads than your user had when the job started (shared with your other programs; root is exempt) | job-object active-process limit | not enforced |
  | `max_vram_bytes` | placement only | placement only | placement only | placement only |

  Whatever is not enforced on this laptop is **said**, never claimed: the agent
  advertises it as `not_enforced` (e.g. `max_vram_bytes:no_runtime_limit`,
  `max_cpu_percent:unsandboxed_linux`, `max_ram_bytes:unsandboxed_gpu_jobs`),
  and `compute nodes` lists it in its `not enforced` column (V0.7 G6). **Linux
  CPU share (V0.7 G6):** at start the agent logs
  `agent.execution.cpu_cgroup_ready` or `agent.execution.cpu_cgroup_unavailable`
  with a reason (`not_delegated` = started from a terminal or an older unit;
  `no_cpu_controller`, `not_writable`, `cannot_enable_cpu`, `no_cgroup_v2`).
  Delegated, each job runs in `<agent's service cgroup>/mf-<attempt id>`
  (removed after the run; `cgroup.kill` also ends a child that escaped with
  `setsid`; leftovers of a crashed agent are removed at the next start). A job
  cgroup that cannot be created refuses the job `config_error` /
  `limits_not_applied`. Re-copy `deploy/systemd/meshforge-agent.service`
  (it gained `Delegate=yes` + `DelegateSubgroup=agent`) and
  `systemctl --user daemon-reload && systemctl --user restart meshforge-agent`.
  and `compute nodes <node id>` / `GET /v1/nodes/<node id>` shows it under
  `owner_policy`. On Windows, if the job object cannot take a required RAM cap
  the job is refused (`config_error`, `limits_not_applied`) rather than run
  without it. A job that goes over `RLIMIT_AS` gets `MemoryError` (reported
  `oom`).
- The old `[agent.execution] enabled / allow_unsandboxed / max_cpu_jobs /
  max_gpu_jobs` keys still work for one release (`agent.config.deprecated_key`
  warning at start); a `[agent.policy]` key wins over its old spelling.

### 0b. Running other people's jobs on this laptop (V0.4, D45/D51 — [ADR-0014](adr/0014-interim-execution-trust.md))

An agent **runs nothing** unless its owner opts in (`[agent.policy] accept_jobs
= true`, section 0b0). The other execution settings, in that laptop's
`config.toml`:

```toml
[agent.execution]
grace_s = 3.0               # cancel/timeout: SIGTERM, then SIGKILL after this (<= 3 s)
# workdir_root = ""         # empty = <data dir>/work
# default_memory_mb = 4096, default_pids = 1024, default_cpus = (no limit),
# default_timeout_s = 3600, max_timeout_s = 86400, run_as = "auto"
# keep_workdirs = false     # true = keep each finished job's folder + its logs
# max_concurrent_attempts = 4
# max_written_bytes = 8589934592   # most disk a running job may add to its folder (V0.7 G5)
# output_check_interval_s = 1.0    # how often that is measured
# free_disk_read_failure_limit = 5 # failed free-disk reads in a row that stop a job (V0.7 G6)
# free_disk_read_timeout_s = 2.0  # a free-disk read still running after this counts as failed (stale mount)
# secret_write_wait_s = 10.0      # longest a cancel / agent stop waits for a job's secret files being written (0.1 .. 120)
```

**How many jobs at once (V0.6, D105).** The controller schedules by the slots
this laptop advertises in every Hello (`[agent.policy]`, live with `policy set`): `max_cpu_jobs` CPU slots and
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
  cannot see your processes, has no network unless its manifest asks
  `limits.network: on` and you allow it (`[agent.policy] allow_network`), and is capped
  in memory, processes and (optionally) CPU. It gets exactly the GPUs it was
  assigned, none otherwise. The images are pinned by digest in
  `[agent.execution.images]` (`python_cpu`, `cuda_pytorch`); a value without
  `@sha256:` is refused. The first job pulls them (the PyTorch one is ~4 GB) - pull
  ahead on a fast link with `meshforge-agent prepare` (see "Job environments" below).
- **The sandbox is checked before every job starts (V0.7 G5, D143).** After
  Docker creates a container the agent reads back what Docker really made and
  refuses to start it unless it is the sandbox. The check is an allow-list
  (fix r1, D160): not privileged, no capabilities, no-new-privileges and no
  other security option (the daemon's default seccomp profile only - a custom
  `seccomp=` profile is refused too), `/proc` masked, a process limit, memory
  and memory+swap exactly the planned cap, `/tmp` the only tmpfs with exactly
  the planned size, its own PID/UTS/user/IPC/cgroup namespaces, no sysctls,
  published ports, extra groups or devices, **only the job folder mounted**
  (never the Docker socket or another host path), a read-only system, network
  only `none` or the default bridge (never `host`) and `none` unless allowed,
  and no create option the agent does not use itself. Otherwise the job fails `config_error` with
  `sandbox_violation: <what>` and the container is removed unstarted (agent log
  `agent.execution.sandbox_violation`). A Docker daemon that does not apply a
  seccomp profile by default is not used at all (`docker_seccomp_off` /
  `docker_seccomp_unconfined` in `agent.execution.no_runtime`): fix the daemon
  (`docker info` must list `name=seccomp,profile=builtin` or similar).
- **Output folder cap (V0.7 G5).** A running job may add at most
  `max_written_bytes` (8 GiB) to its folder (outputs, scratch files, its logs).
  The agent measures it every `output_check_interval_s`; past it the job is
  stopped and fails `output_error` with `output_limit: ...` (not retried; agent
  log `agent.execution.output_limit`). A very fast writer can overshoot by what
  it writes in one interval. **Known limit (accepted, THREAT_MODEL WS-E8):** a
  file the job deletes while keeping it open is not counted (the space is
  freed when the job ends; the job's timeout bounds it). **Free-disk guard while
  running (G5 fix r2):** the same poll reads the free space of the workdir's
  disk; below the agent's staging floor (1 GiB) the job is stopped and fails
  `disk_full` with `disk_floor: ...` (agent log `agent.execution.disk_floor`).
  It is also a poll, so a fast writer can pass the floor by one interval's
  writes; a write that then hits "no space left" fails `disk_full` too.
  **The poll can lag (V0.7 G6, D161 N2):** the free-disk read happens in the
  same loop as the folder walk, and that loop waits 5x the last walk between
  rounds; a folder with millions of files, or a slow disk / network share,
  can stretch the check from 1 s to many seconds. Keep `workdir_root` on a
  local disk. **A free-space read that keeps failing never fails open:**
  a read that **hangs** (a stale network mount) counts as failed after
  `free_disk_read_timeout_s` (2 s) - at every check, without starting a second
  read (at most 4 such stuck reads exist in the whole agent);
  `[agent.execution] free_disk_read_failure_limit` (5) failures in a row stop
  the job with `disk_full` / `disk_unknown: ...` (agent log
  `agent.execution.disk_unknown`; each failure logs
  `agent.execution.free_disk_read_failed`); one good read resets the count.
  `/tmp` inside the container is a separate tmpfs of
  `tmp_size_mb` (512 MiB) that fills up with "no space left" inside the job.
- **Containers are labelled** `meshforge.agent=<node id>`. If the agent is killed, its
  containers are removed the next time it starts; you can also list them with
  `docker ps -a --filter label=meshforge.managed=1`.
- **Subprocess runtime — NOT a sandbox.** With `[agent.policy] allow_unsandboxed = true` a laptop
  without Docker can still run jobs, but the job runs **as you**: it can read and
  change your files, see your processes and use the network. Time limits and
  cancel are enforced; memory, process and CPU caps only as far as the table in
  section 0b0 says (Linux: rlimits; Windows: the job object). Turn it on only on
  a machine you use for the lab, never on one with personal data.
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
  the environment or packing outputs, or free disk fell below the 1 GiB floor
  while the job ran (`disk_floor: ...`) (an auto job is retried on another laptop,
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
- **Wheels only by default (V0.7 G6, WS-I4).** Every environment build installs
  with `--only-binary :all:` (`[agent.envs] only_binary = true`), so no
  package's own build code (an sdist's `setup.py` / build backend) runs while
  building - also in the Docker build container, which keeps the package-index
  network even when this laptop allows jobs none. A requirement that has no
  wheel for this platform (an sdist-only package, a `git+`/URL source
  reference) then fails `env_error` / `setup_failed` with "installs are
  wheels-only ... needs only_binary = false". **Escape hatch:** set
  `[agent.envs] only_binary = false` on a laptop where you accept that build
  code runs (in the Docker build container, or as you for the venv/subprocess
  runtimes), or put a pre-built wheel into a `find_links` wheelhouse.
  **The job's own `requirements.txt` cannot switch this off** (V0.7 G6 fix
  round 1): while `only_binary = true`, a file with an **option line** (any
  line starting with `-`: `--no-binary NAME`, `--index-url ...`,
  `--extra-index-url ...`, `--find-links ...`, `--pre` ...), with an option
  other than `--hash=...` after a requirement, or with a direct reference that
  is not the `https://` URL of a `.whl` file (a `.tar.gz`, a `git+` URL, a
  wheel over plain `http://`) is
  refused before anything is installed: `env_error` / `requirements_rejected`,
  naming the line. A job that needs another package index (for example the
  PyTorch wheel index) gets it from the laptop owner's `[agent.envs] index_url`
  or a `find_links` wheelhouse, not from its own file.
  **A wheel URL must be pinned** (V0.7 G6 fix round 3): while
  `only_binary = true`, a line with the `https://` URL of a wheel is accepted
  only with its digest -
  `pkg @ https://host/pkg-1.0-py3-none-any.whl --hash=sha256:<64 hex digits>`
  (`sha384:` / `sha512:` also; `md5:` / `sha1:` do not count; get it with
  `sha256sum pkg-1.0-py3-none-any.whl`). Without it the line is refused
  (`requirements_rejected`, naming the line). Why: "https only" is a rule about
  the text of the line - the installer follows a redirect from that URL to
  plain `http://`, and the host can serve another file tomorrow. With the
  digest the installer refuses every file but the pinned one, whichever way it
  arrived (checked with real uv: redirect to http + right hash installs, wrong
  hash fails, right hash + a file swapped on the http leg fails). Requirements
  by name (`pkg==1.0`, from the owner's index or wheelhouse) need no hash.
  *Test coverage note:* all of this is decided by one gate before any
  installer starts, for uv and for the pip inside the Docker build container
  alike. A URL that only looks like a wheel (`.../pkg.tar.gz/.whl`) over
  `http://` is therefore refused at the gate and the pip in the container is
  never shown one - our Docker test proves "refused before a build container
  exists", and the look-alike URLs themselves are tried against real uv only
  (accepted, D164).
- **No environment variables in a requirements file** (V0.7 G6 fix round 2):
  a line containing `${VAR}` or `$VAR` is refused always - `only_binary` on or
  off, uv and Docker pip alike - with `env_error` / `requirements_rejected`,
  naming the line. pip and uv would fill it in from this laptop's environment
  (`HOME`, a proxy URL with a password ...) and send it to the host the line
  names. Write the value out; a private index belongs in the owner's
  `[agent.envs] index_url`.
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

### 0d2. Benchmark conditions on an agent (V0.9 G4, D217)

A benchmark run may ask the agent to load the CPU/GPU, empty its cache for the job, or shape its network
first (`docs/BENCHMARKS.md` "Conditions"). The agent applies, verifies and reverts each one itself:

```toml
[agent.conditions]
enabled = true              # off = advertise none (campaigns needing one on this node are refused/skipped)
netem = false               # Linux only; needs the one sudoers rule in lab/netem/README.md
netem_interface = ""        # e.g. "wlp2s0": the interface toward the controller (never lo)
tc_command = ["sudo", "-n", "/usr/sbin/tc"]
verify_window_s = 2.0       # how long a load is measured before it counts as applied
setup_allowance_s = 900     # load processes end by themselves at time limit + 30 s + this
```

The agent itself still runs unelevated; `netem` is the only condition that needs root, through that single
opt-in rule. Leftovers of a crashed agent (load processes, a netem qdisc) are undone at its next start from
`<data dir>/conditions/active.json` (logged `agent.conditions.swept`). A netem is deleted only when its
parameters match what the entry recorded (`netem_expect`, V0.9 G5): a netem someone else installed with
other parameters is left alone (`agent.conditions.netem_not_ours`).

### 0d3. Running a campaign unattended (V0.9 G5, D219/D246)

`compute bench start <camp_id>` hands a created campaign to the controller's **campaign runner** (a
supervised task inside the controller; nothing else to run). It then submits the runs itself, a few at a
time, until every run ended - across controller restarts (all its state is in PostgreSQL; after a
`kill -9` the next process continues, and no run is submitted twice):

```
compute bench start <camp_id> [--per-node 1] [--cooldown 5] [--retries 2]
compute bench status <camp_id>      # counts, done %, ETA, infrastructure failure rate
compute bench pause <camp_id>       # stop submitting; running jobs finish
compute bench resume <camp_id>
compute bench cancel <camp_id>      # cancel its benchmark jobs, skip the open runs
```

* **Throttle:** at most `--per-node` benchmark jobs at once per node (all campaigns together), and
  `--cooldown` seconds after **any** benchmark job on a node (any campaign, fillers and warm-ups included)
  reached its end before that campaign submits there again - the cool-down is per node, so a second campaign
  cannot start right after another one's run on the same machine. Each tick (`[controller.bench] tick_s`,
  2 s) submits at most `max_submit_per_tick` (4) runs and examines at most `max_examine_per_tick` (32).
* **Retries:** an infrastructure failure (node lost, transfer error, timeout, env build...) puts the run back
  in the queue up to `--retries` times; a workload failure, an unverified condition, a cancel are final.
  A requeued run gets a new job (`bench:<run_id>:<n>`); `status` counts the retries.
* **Budget:** past the spec's `budget.max_hours` (from `start`) nothing more is submitted; the open runs are
  `skipped` and the campaign ends `done` / `budget`.
* **User jobs come first:** a node with a user's job running (or queued for it) gets no benchmark; benchmark
  jobs are the lowest priority and never age (D237). G3's manual `compute bench submit` is refused
  (`409 campaign_running`) once a campaign is started.
* **Known limit (D251 N2):** a tick examines at most `max_examine_per_tick` planned runs. When more runs than
  that at the head of a campaign are refused one by one (an owner RAM / CPU limit), the tick that hits the cap
  remembers where it stopped and the next tick continues from there, wrapping to the head - no run starves.
  After a tick that submitted its budget the scan starts at the head again, so such a head costs a capped
  walk every other tick (submission throughput about halves while it lasts). The cursor deliberately does not
  keep scanning past the cap within one tick: the cap is what keeps a tick short however large the campaign.
* **Before using a campaign's data:** `compute bench report <camp_id> --strict` exits 1 unless criteria 1
  (infrastructure failures < 2 %) and 3 (every configuration >= 3 repetitions with a CV) are met. Flagged
  configurations (CV above `[controller.bench] quality_cv_threshold`, default 0.10; too few repetitions; a
  required field missing) are listed first. **Limit:** the report is recomputed on every request over every
  record of the campaign (streamed, not cached; 10 000 records take well under a second of statistics).
* **One active runner per database.** Each tick takes a PostgreSQL advisory transaction lock; a second
  controller pointed at the same database skips its ticks while the first one runs them (debug log
  `controller.bench.tick_locked_out`), so two controllers never fill a node's slot twice. The lock goes with
  the connection, so a killed controller never leaves it held.
  The lock keeps one pooled connection in an open transaction for the whole tick: keep the database pool
  at ≥ 2 connections and do not set PostgreSQL `idle_in_transaction_session_timeout` shorter than a tick
  (if it fires, the tick fails and the runner retries with backoff - safe, but noisy).
* `/healthz` says `degraded` (`bench-runner`) when the runner failed 5 ticks in a row; it logs
  `controller.bench.runner_crashed` and retries with back-off.

```toml
[controller.bench]
runner = true               # off = only manual `compute bench submit`
tick_s = 2.0
max_submit_per_tick = 4
max_examine_per_tick = 32
orphan_scan_s = 30          # orphan-job backstop scan at most this often (a cancel triggers one at once)
quality_cv_threshold = 0.10 # `compute bench report` flags a configuration above this CV
```

**What the laptop owner sees and controls (the owner guard).** A benchmark never runs while the owner is
using their laptop:

```toml
[agent.bench]
activity = true             # watch for owner activity (off = only the manual switch)
idle_threshold_s = 300      # input within the last 5 min = the owner is active (0 = ignore idle time)
foreground_cpu_percent = 30 # or: programs that are not the fabric's use > 30 % CPU ...
sustain_s = 10              # ... for 10 s
clear_after_s = 60          # activity clears only after 60 s quiet
poll_s = 2
preempt_revert_timeout_s = 10  # longest wait for conditions to revert when a benchmark is stopped
```

* `meshforge-agent bench pause` (= `meshforge-agent policy set pause_benchmarks=true`) pauses benchmark
  jobs on this laptop; `meshforge-agent bench resume` lets them run again. User jobs are not affected.
* While paused or active: the controller dispatches no benchmark job here; a running one is **stopped**
  (its load generators and network shaping are reverted first) and ends `owner_preempted` - the campaign
  requeues it, it does not count as a failure. A revert that hangs (a `tc` that never returns) is waited
  for at most `preempt_revert_timeout_s`: the agent logs `agent.bench.revert_timeout`, stops the benchmark
  anyway (its report says `reverted = false`), keeps retrying the revert in the background and leaves the
  condition marker, so the next agent start reverts it if it never finished.
* Input idle time comes from Windows (`GetLastInputInfo`) or Linux logind (`loginctl`; needs a desktop that
  sets the idle hint - GNOME, KDE). Without one the agent logs `agent.bench.idle_source_unknown` and only the
  CPU rule applies. Only idle time and per-process CPU are read - never keystrokes, window titles or what a
  program shows - and only the bit ("owner active", reason `idle` / `cpu`) leaves the laptop.
* On Windows with Docker Desktop, the WSL VM's CPU (`vmmem`) is ignored while this agent runs a job (that
  CPU is the job); the idle-time rule still sees the owner.

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
- **Replayable decisions (V0.14 G1, D292).** Every stored decision also keeps
  the planner's exact inputs (`plan_decision.replay`: the job, and the nodes,
  links and history the planner read for it - JSON, `replay_version` 1), so
  the decision can be re-run later, with the same or another policy, and gives
  the same answer (the V0.14 planner evaluator uses this). A replay larger
  than `[controller.planner] replay_max_bytes` (65 536 bytes; 1 KiB - 1 MiB) is
  not kept: the row has `replay = null`, the controller logs
  `controller.jobs.replay_over_cap` with the size, and the job is placed
  normally - a replay never delays or fails a job. Typical sizes: a few KiB
  for a 4-laptop fabric.
- **Planner calibration (V0.14 G2, D293/D301/D306).** The planner corrects its
  own predictions from what really happened. For each predicted term with an
  honest measured phase - `transfer` <-> `stage`, `env` <-> `env`, `compute` <->
  `run`, `output` <-> `collect` (a split job: only its lanes' `stage` and
  `env`) - a **factor** is the median `actual / predicted` over the last
  `calibration_window_runs` (200) finished runs of that key, **node + workload
  family**, else **node**, else **global**, shrunk toward 1 by `n / (n + k)`
  (`calibration_shrink_k` 5: one odd run cannot swing it) and clamped to
  `calibration_clamp` ([0.2, 5]). A term that is `measured` (a cached env, 0 s
  queue) is never scaled. Only runs that finished `done` on the node their
  decision chose count (pinned and campaign runs too); a Mode 3 set's blended
  terms never do. `compute explain` / the stored candidates then show each
  scaled term with `detail.calibration` (`factor`, `n`, `key_level`,
  `version`, `raw_s` = the uncalibrated prediction), and
  `plan_decision.calibration_version` names the version (`plan_decision.replay`
  carries its factors, so a replay reproduces the decision exactly).
  - **Versions and safety.** Every fit is a row of `planner_calibration`
    (`active` / `rejected` / `superseded`). A new fit is judged on the **newest
    20 %** of matched runs **of each workload** (at least one per workload with
    two or more runs), which it was not fitted on: it goes live only if its
    mean `|ln(predicted / actual)|` there is at most **1.05 x** the active
    version's (or no calibration's); otherwise it is stored `rejected` with the
    reason. At most one row is ever `active` (a unique index; refreshes run one
    at a time under a database lock). Fewer than 2 matched runs, nothing to
    fit, or the same matched runs and settings as the newest version
    (`no_new_runs`: a refit would only duplicate it) writes nothing.
  - **Refresh:** every `calibration_refresh_s` (1800 s, first one interval
    after start) and on demand: `compute planner calibrate` (admin) prints the
    new version or why nothing was written; `compute planner calibration
    [--version N]` shows the factors with `n` and key level and what the
    planner uses now. A failed refresh is logged
    (`controller.planner.calibration_refresh_failed`) and the active version
    stays; dispatch never waits for it - the dispatcher re-reads the active
    version id once per tick and keeps the one it had if that read fails
    (`controller.planner.calibration_read_failed`).
  - **`[controller.planner] calibration`**: `"auto"` (default: the active
    version), `"off"` (no calibration, no refresh task), or a version number
    such as `"7"` to **pin** it - the controller refuses to start if that
    version does not exist, cannot be read, or is `rejected` (a rejected fit
    failed the gate and is never used; `superseded` versions can be pinned)
    - never a silent fallback. An active version whose factors cannot be
    read is logged once (`controller.planner.calibration_unreadable`) and
    the planner plans uncalibrated until a new version is active. With
    `off` or a pin, `compute planner calibrate` still fits and gates, but the
    planner keeps what the setting says. `calibration_window_runs` 1-1000,
    `calibration_shrink_k` > 0, `calibration_clamp` 0 < low < 1 < high <= 1000,
    `calibration_refresh_s` 10 s - 7 days.
- **Risk, objectives, hopeless links (V0.14 G3).** Each estimate may end with
  a `risk` term: the chance the node fails the attempt - from its own
  node-caused attempt ends (lost, `node_lost`, `owner_preempted`, `disk_full`,
  `env_error`, `transfer_error`; never the job's own failures) over
  `[controller.planner] risk_window_days` (7; 0 = no risk term), smoothed by
  `risk_prior` ([1, 19]: a new laptop is assumed to fail 5 % of attempts) -
  times what a failure would cost. A job may say what the `cost_model` policy
  optimises: manifest `objective:` or `compute run --objective`
  (`min_time` - fastest, the default; `min_network` - fewest bytes over the
  network; `prefer_local` - stay on the submitting laptop unless another is
  predicted `prefer_local_margin` (25 %) faster); `[controller.planner]
  default_objective` applies otherwise, and `plan_decision.objective` records
  it. A laptop whose freshly measured link would carry more than
  `min_useful_bytes` (64 MiB) at less than `min_useful_bw_bytes_s` (1 MB/s) is
  skipped for that job (`link_infeasible`, the job waits for a better link
  rather than crawling); a stale or unmeasured link never causes it, and a job
  pinned to a node never gets it. A split job's decision lists laptops whose
  sets were not costed because one faster laptop would finish everything before
  they could start, so adding them changes nothing (`pruned`; the chosen plan is
  always the one without pruning). `compute explain` names the job's objective
  next to the policy. A decision slower than
  `decision_latency_budget_ms` (100) is logged as
  `controller.jobs.decision_over_budget` (once per job).
- **Evaluating the planner (V0.14 G4, D297-D299, D313-D319).** `compute
  planner eval --campaign ID [--campaign ID ...] [--holdout FAMILY]
  [--allow-leak] [--md report.md] [--json]` (admin; `POST
  /v1/planner/eval-reports`) answers "how good are the planner's choices?" on
  finished benchmark campaigns. For every labelled group (`compute bench
  labels`) it re-plans each run's stored decision - the shadow request of the
  forced run, i.e. exactly what the planner saw - under every policy (the six
  rule policies, `cost_model` uncalibrated and calibrated x each objective, and
  the evaluation-only `oracle`), maps each pick to the campaign configuration
  that measured it, takes the most frequent pick per group and reports its
  **regret**: how much slower its median `total_ms` is than the best
  configuration's (0 when the pick is the best or in the label's tie set).
  `insufficient` / `pending` groups, groups whose runs have no replay
  (`no_replay`) and picks the campaign never measured (`unmeasured`) are
  listed, never counted as 0. The calibrated rows use a **leave-one-family-out
  refit** (fitted on every matched run except that family's and its
  workloads'), never the live version - so criterion 1 is never measured on
  training data; `--allow-leak` (debugging) uses the live version and stamps
  the report and every verdict `LEAKED`. The report gives per policy the mean
  regret with a seeded bootstrap 95 % CI (10 000 resamples over groups),
  the share within 15 %, per family, and the two verdicts: criterion 1 (`PASS`
  when calibrated `cost_model` is within 15 % of the oracle in >= 80 % of
  counted groups) and criterion 2, **paired** (D322): for each rule policy the
  per-group regret difference (rule minus calibrated `cost_model`) over the
  groups counted for both, with a bootstrap CI - `PASS` only when every such
  CI lies entirely above 0, `FAIL` when one lies entirely below 0, else
  `not_separable`; the verdict lists each comparison and every policy's
  `unmeasured` count (a policy cannot look good by being unmeasured on the
  hard groups). Under 10 counted (criterion 2: common) groups ->
  `too_few_groups`, never `PASS`. A non-finite median (a guard; labels never
  produce one) excludes its group with the reason. Reports are stored (`planner_eval_report`,
  migration 0032) and listed by `GET /v1/planner/eval-reports`; `GET
  /v1/planner/eval-reports/{id}?format=markdown` renders one; `compute planner
  reports [--limit N] [--json]` lists them (id, time, campaigns, held-out
  family, leaked, evaluated groups, verdicts). The same
  campaigns give the same report bytes. **Harness campaigns prove the code
  path only; the criteria need real-laptop campaigns (0.14.8).**
  - **Calibration regret check (D319).** A new calibration version that passes
    the error check is also replayed on its held-out runs' labelled groups:
    if they cover >= 10 groups and calibrated `cost_model`'s mean regret there
    is more than 1.05 x the active version's, it is stored `rejected`
    (`holdout_regret ...`). With fewer groups the check is skipped and the
    active row's `reason` says `regret check skipped (too_few_groups: n < 10)`
    (`compute planner calibration` prints it as `note:`). The numbers are in
    the row's `fit_inputs` (`regret_check`, `regret_groups`,
    `holdout_regret_new` / `_active`). The check (label reads and replays) runs
    **before** the refresh takes its database lock; under the lock the refresh
    only re-checks that the runs and the active version did not change (if they
    did, it judges the new fit again there). The x 1.05 boundary is compared
    exactly in decimal.
  - **Decision latency (V0.14 G6, D324; criterion 3).** Every dispatch
    decision stores how long the planner took (`plan_decision.decision_ms`,
    migration 0033: snapshot cut, plan, a campaign run's shadow plan and the
    replay; NULL on older rows). `compute explain` prints it as `decided in
    X ms` and the Plan tab shows it. Each evaluation report has a `latency`
    block over the evaluated campaigns' `dispatched` decisions (their run jobs
    of every generation, fillers and warm-ups): count, rows without a time
    (counted, never as 0), nearest-rank p50 / p99 / max, decisions over
    `decision_latency_budget_ms`, per snapshot size (every registered node
    row, offline and revoked ones too) and per number of **live** snapshot
    nodes (online, connected, accepting jobs at decision time; G6F, D332);
    and a **criterion 3** verdict: `PASS` iff the p99 of the decisions whose
    snapshot held **at least 4 live nodes** is strictly below the budget,
    `too_few_decisions` below 30 of them. `compute planner eval` prints `decision latency: ...`,
    `compute planner reports` a `CRITERION 3` column, the Planner page a
    "Decision latency" row. A report made before D332 judged all snapshot rows
    (it says "nodes", not "live nodes"): its criterion 3 verdict is not comparable
    with a new one - re-run the evaluation.
  - **Evaluating the planner on lab data (0.14.8).** The procedure, with what
    to paste back, is [lab/checklists/V0.14-planner.md](../lab/checklists/V0.14-planner.md).
    In short (admin, on the controller laptop):

    ```bash
    compute bench create v014.yaml && compute bench start <CAMP> --per-node 1 --cooldown 2
    compute bench status <CAMP>                      # until done (complete)
    compute planner calibrate && compute planner calibration
    compute planner eval --campaign <CAMP> --md eval-all.md          # criteria 1-3, every family held out in turn
    compute planner eval --campaign <CAMP> --holdout <FAMILY> --md eval-<FAMILY>.md
    compute planner reports
    compute explain <JOB> --json                     # criterion 4: == GET /v1/jobs/<JOB>/decisions
    ```

    Criteria 1 and 2 need >= 10 counted groups per evaluation and campaigns
    run on a V0.14 build (decisions without a replay are `no_replay`);
    criterion 3 needs >= 30 decisions made while all 4 laptops were online
    and accepting jobs (the report's `by_live_node_count` shows it).
- **ML training dataset (V0.15 G1, D333-D335; ADR-0021).** `compute planner ml
  dataset [--json]` (admin; `GET /v1/planner/ml/dataset?format=summary`)
  prints what the ML planner would train on; `compute planner ml dataset --out
  ds.parquet` also downloads it (`GET /v1/planner/ml/dataset`) and writes
  `ds.parquet.json`, the sidecar (versions, digest, counts, schema). Rows: one
  per matched (decision, outcome) run - the **same** matching the calibration
  fits on - and learnable term: a whole job's `transfer` / `env` / `compute` /
  `output`; a split job's per-lane `lane_stage` / `lane_env` / `chunk_rate`
  (seconds per item from the lane's committed chunks) and its `combine`. Each
  row's features come from the decision's **replay snapshot** (what the
  planner saw: node scores, load, cache, links, job size, the analytical raw
  prediction and its source; a missing input is NaN, never 0) and its target is
  `y = ln(actual / raw)` (0 = the analytical model was exact). Nothing is
  imputed: a decision without a replay (`missing_replay`), an unreadable one,
  a skipped or missing phase, a term exact by construction (nothing to move, a cached env - D345; a transfer over a probed link IS learned), a prediction under 0.05 s, a
  lane without a committed chunk ... is **counted by reason** in the summary's
  `skipped`. The file is byte-identical for the same runs (its `digest` is the
  dataset's identity for V0.15 training); at most `[controller.planner]
  ml_dataset_max_runs` (50 000) newest runs are read - `truncated: true` says
  older ones were left out. It is built on the first request (bounded, paged
  reads; a few seconds per ten thousand runs) and **cached** (V0.15 G2, D355):
  the next request reuses it until a cheap watermark changes (a new matched
  run, any attempt that ends), and concurrent requests share one build. The
  summary's `counts.node_failures` (D354) is how many attempts ended
  node-caused (the risk term's own definition) in the dataset's window - a
  failure classifier is only worth building once a real dataset has 50.
  Harness data proves the pipeline; the V0.15 criteria are judged on the lab
  campaign of 0.15.11 only.
- **Comparing ML models (V0.15 G2, D337/D338/D347-D353).** Offline, on any
  machine with the dataset file: install the extras once (`uv sync --extra
  controller --extra ml`; Linux and Windows use the CPU-only `xgboost-cpu`
  wheel, ~55 MB of wheels in all), then `compute planner ml compare --dataset
  ds.parquet --out report/` (no controller needed; without the extras it
  exits 2 with that install hint). Per term it compares ridge, kNN, tree,
  forest, LightGBM and XGBoost with the raw analytical model and the V0.14
  calibration: the numbers are **held-out families only** (leave one family
  out) and the model is chosen on the other families (grouped 5-fold, one
  standard error rule), so a model never scores on a family it was chosen on.
  Read the table: `status` `trained` (a learned model is the pick on all
  data), `no learned model` (calibration or raw wins - a real outcome) or
  `insufficient data` (< 200 rows or < 3 families: the planner keeps
  calibration); `MALE` = mean absolute log error (0.1 is about 10 %); `vs cal`
  = `better` / `not separable` / `worse` / `no learned model` (paired
  bootstrap over families; `better` needs at least 5 families, a gain whose
  95 % CI clears 0.02 log - `--min-effect` - and a learned model picked in at
  least half the held-out folds and on all data);
  `cover` = share of held-out values inside the 90 % conformal interval (the
  model would be rejected under 80 % pooled, or under 70 % on any family with
  20+ held-out rows); `usable` = share where ML would be used
  (in distribution and interval at most x2 wide). `report.json` is
  byte-identical for the same dataset and library versions; `report.md`
  summarises it, with the top features and the failure-classifier line.
  `--terms` / `--algorithms` cut the run time (about 6-7 min for 20 000 rows
  on one core; a harness dataset takes seconds).
  `--timeout S` (default 3600) stops a comparison that runs too long (exit 1).
- **ML model registry and retraining (V0.15 G3, D339, D358-D361).** With the
  `ml` extra installed on the controller and `[controller.planner] ml =
  "auto"` (the default) the controller retrains once a day
  (`ml_retrain_s`, 86 400; the lab may use 3 600) in a child process (`nice`d,
  killed after `ml_retrain_timeout_s`, 3 600) - skipped when no run ended since
  the last set (`no_new_runs`) and when every term has too little data.
  `compute planner ml train --wait` retrains now (admin). Each training run
  stores one **model set** (one model per term, or `none` with the reason)
  and goes live only through the **activation gate**: on the newest fifth of
  every workload's runs (held out, the calibration's own split) the set must
  be no more than 5 % worse than the set in use (or the calibration), keep
  90 % intervals honest (>= 80 % coverage), be within 0.05 log error of the
  calibration on every family, and beat the calibration somewhere by more
  than 0.02 - else it is stored `rejected` with the rule that failed
  (`compute planner ml models`, `--set VERSION` for the numbers; the planner
  regret part of the gate is `pending_g4` until V0.15 G5). **Rollback:**
  `compute planner ml pin VERSION` (any active or superseded set; refused for
  a rejected one or one whose artefacts the GC removed - the GC keeps the
  active, the pinned and the `ml_keep_superseded` = 5 newest superseded sets);
  `compute planner ml unpin VERSION` returns to the automatic choice. A pin
  in configuration (`ml = "sha256:..."`) must name an existing, non-rejected,
  loadable set or the controller does not start; `ml = "off"` plans exactly as
  V0.14. A controller without the `ml` extra starts normally, logs
  `controller.planner.ml_unavailable` once and never trains (`train` answers
  409); it can still serve ridge / kNN / tree / forest sets (numpy only), not
  LightGBM / XGBoost ones (`library_missing`). Same dataset + same code and
  library versions -> the same set version (byte-identical artefacts).
  Details operators may meet (V0.15 G3 fix r2):
  * a `rejected` set judged again under other gate settings (or another
    reference) is updated **in place** - it keeps its original `created_at`,
    so `compute planner ml models` still lists it where it was first trained
    (newest `created_at` first); the move shows in `status_changed_at` and the
    previous verdicts in the gate's `history` (newest 20 kept);
  * `compute planner ml train` works whatever `ml` is: with `ml = "off"` or a
    configured pin it still trains, gates and stores a set (and may make it
    `active`), but the planner keeps planning with nothing / the configured
    pin until the setting changes - only `ml = "auto"` retrains on the
    schedule;
  * a retrain holds **one** connection of the controller's database pool for
    its whole run (the cross-controller session advisory lock), plus short
    ones for its reads and the store - size `[controller.database]` pools with
    that one in mind;
  * a retrain whose child exits 0 but leaves an unreadable `set.json` / a
    missing artefact, or a document the database refuses, fails
    `child_output_invalid` (nothing stored; logged
    `controller.planner.ml_retrain_failed`), not `crashed`; a learned
    artefact that is empty or cannot be loaded makes the set `rejected`
    `unloadable_artefact: <term> (<reason>)`;
  * after an upgrade that changes the feature version (D366: 2), a set
    trained on the old features is not used: one
    `controller.planner.ml_set_stale_features` info line, the planner keeps
    the calibration until the next retrain (`compute planner ml train`)
    stores a set of the new version.
- **The planner with an ML model (V0.15 G4, D336/D363).** When a model set
  serves (the registry above: pinned, else active; with none, or `ml = "off"`,
  every decision is exactly V0.14's), each learnable term of each candidate uses the
  model only where it is trusted - in distribution and its interval at most
  `[controller.planner] ml_max_interval_log` wide (ln 2: x2) - else the
  calibration factor, else the raw estimate. `compute explain` shows it per
  term (V0.15 G5b, D373 - the dashboard's Plan tab shows the same text):
  `ML 12.00 s -> 9.40 s (3a1f00aa3a1f, x0.78, +-18 %)` or `ML not used: out of
  distribution (range:hw_cpu_gflops) (...)` / `no model for this term` /
  `interval too wide` / `exact by construction` / `below the trained range` /
  `predictor error`; a Mode 3 lane adds `; rate: ML ... ms/item -> ...`; the
  decision line names the set (`ML set 3a1f00aa3a1f`, plus `- fell back to
  calibration: predictor error` after a failure). The decisions API carries
  these as typed fields (`TermOut.ml`, `DecisionOut.ml_model_version` /
  `ml_fallback`; null for decisions made before V0.15). **A broken or slow model never stops
  planning:** an error, a malformed answer or a `predict` call slower than
  `ml_predict_budget_ms` (20) makes that decision fall back to calibration
  (rows say `ml_fallback: predictor_error`), the controller logs
  `controller.jobs.ml_predictor_error` at most once a minute - if you see it,
  check the model version it names. Measured (D365): with a model on every
  term (LightGBM 400 trees, OOD reference 20 000 rows) a 4-node decision takes
  about 10-13 ms p99. A model is **warmed up** when it is loaded (one
  representative batch, outside the budget), so the first decision after an
  activation is not a cold ~28 ms call that would fall back; a model whose
  warm-up fails is not used (`controller.ml.model_warm_up_failed`, the planner
  keeps calibration). **A CPU-saturated controller can lose ML for some
  decisions** - a `predict` call that misses the 20 ms budget because the
  machine is busy falls back to calibration (`budget_exceeded`, counted in
  `ml_predictor_errors` and logged at most once a minute). That is the
  intended D363 behaviour: planning never waits for the model.
  **The 20 ms budget is wall clock** (it includes any time the controller
  process was descheduled), so a model whose `predict` normally takes close to
  the budget can flip some decisions to `predictor_error` on a busy machine.
  Watch it on `GET /v1/planner/ml/models` (`in_use.ml_predict_last_ms` - the
  last call - and `in_use.ml_predictor_errors_total` - fallbacks since the
  controller started) or the dashboard's Planner -> Models tab; a last time
  regularly above ~half the budget means raise `ml_predict_budget_ms` or use a
  smaller model.
- **Is the model better where it is used? (V0.15 G5b, D374).** Planner ->
  **Models** in the dashboard shows the set in use, the set history with gate
  reasons (incl. the regret check), each term's cross-validation and held-out
  numbers, and (admins) a predicted-vs-actual scatter per term on log axes -
  calibration points for every run, ML points where ML corrected the term, the
  y = x line - with the fallback rate by reason. The data is `GET
  /v1/planner/ml/predictions?term=&set=&limit=` (admin; newest 500 matched runs
  by default, at most 2000; read from the stored decisions, ~300 ms budget at
  10 000 decisions). Pin / unpin / train are there too (admin, each asks for
  confirmation) and do what the `compute planner ml` commands do.
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
- **Split jobs (V0.10, Mode 3 - in progress).** A manifest with `split:` names its
  items (`over: {glob | list | list_file | range | lines}`, see
  `meshforge.common.manifest`). The controller resolves them **once, at submit**
  (bytewise-sorted paths for a `glob`), stores the list and refuses a bad one with
  `400 invalid_input` naming the YAML path (`split.over.glob: matched no files`).
  `compute run --dry-run` prints the job and the items it would resolve (count,
  first/last, chunking) without uploading or submitting anything.
  **Running (V0.10 G4, D259-D267):** the planner picks a node set and a chunk
  size (`compute explain`); each node runs one **lane** that stages the project
  once (minus the `glob` item files), then pulls chunks and runs the entrypoint
  once per chunk with `{items_file}` (a JSON array of the chunk's items; for
  `lines` the header + the chunk's records), `{chunk_index}` and `{out}` (also
  `MESHFORGE_CHUNK_ITEMS/INDEX/OUT`). Each chunk's `out/` is packed (at most
  `split.max_chunk_output_bytes`, default 256 MiB) and committed once; when the
  last chunk commits the job is `collecting` with its parts index
  (`job.split_parts_digest`) and owes its **combine** (V0.10 G5, D268): one
  `combine` attempt on an agent - the submitter's node if it can run it, else the
  node that produced the most chunk results, else the planner's pick (never the
  controller) - reads every chunk's result in chunk order as `parts/00000/...`
  (read-only) and writes the job's output. `split.combine`: `concat` (default;
  bytes in chunk order), `merge-csv` (`*.csv`: the header once - headers must be
  byte-identical after a leading BOM and the line ending, else
  `combine_error: header_mismatch`; other files concatenated), `merge-json`
  (`*.json`: arrays concatenated / objects merged, a duplicate key, mixed types,
  invalid JSON or a file over `[agent.split] combine_json_max_bytes` (256 MiB) is
  a `combine_error`; merge-json parses one part file at a time, and its peak
  memory is about **3-8x that file's size** - measured 2.9x for an array of
  100-byte strings, 6x for integers, 8x for small objects - so at the 256 MiB
  default plan for up to ~2 GiB of RAM on the combine node, or lower the cap),
  or `{script: [argv]}` (your program, in the job's runtime,
  with `{parts}` / `{out}` and `MESHFORGE_COMBINE_PARTS/OUT`; a non-zero exit is
  `combine_error: script_failed`). A `combine_error` fails the job and is never
  retried (the same parts fail the same way anywhere); a lost combine node or a
  transfer failure is retried on another node under `retry.max_attempts`. A
  combine the node refuses `resource_busy` (it is still at its attempt limit,
  typically while the job's own lane ends) is no failure: it is placed again
  after `[controller.split] combine_busy_backoff_s` (1 s), on the same preferred
  node, without using a retry; only after `combine_busy_max` (100) such
  refusals of one job do they count against `retry.max_attempts` (log
  `controller.split.combine_busy` per refusal). The
  output is then the job's: `compute get <JOB_ID>` downloads it and
  `GET /v1/jobs/{id}` shows it as `output_digest`. A chunk that returned two
  different results (a late or duplicate copy) while `split.deterministic` is
  on (the default) is listed as the job warning `nondeterministic_chunk` (CLI,
  dashboard, API); the first committed result is kept. Only agents
  with the `exec-split` capability (V0.10+) get a lane (`no_split_support`
  otherwise). A lane whose node is lost gives its chunks back at once; a chunk
  leased by a hung or partitioned lane goes back after `visibility_timeout_s`
  (60 s; the lane renews every third of it); a chunk failing on
  `max_failed_nodes` (2) nodes or `retry.max_attempts` times fails the job
  (`chunk_failed: chunk N ...`, no partial output); with no lane left the job is
  re-planned for the remaining chunks (bounded by `retry.max_attempts` rounds).
  **Stragglers (V0.10 G6, D266):** once no chunk is pending, a lane that asks
  for work may get a **speculative copy** of a chunk still running on another
  node - the one predicted to finish last - when its predicted remaining time
  is more than `speculation_factor` (1.5) x the idle node's expected time for
  it. Expected times come from this job's committed chunks on each node (p50 of
  seconds per item), else from the planner's predicted rate plus
  `chunk_overhead_s`; a node with neither gets no copy. A copy never runs on the
  node that holds the original, nor on a node where that chunk already failed;
  at most `speculative_max_frac` (0.10) of a job's chunks ever get one
  (`split.speculative: false` turns it off for a job). The first committed result
  wins; the other copy gets `CancelChunk` (its process is killed, its upload is
  never referenced); a late result of the loser is ignored (`already_done`) and,
  if its bytes differ, raises the `nondeterministic_chunk` warning. Logs:
  `controller.split.speculated` (with elapsed / expected / remaining / copy
  seconds and their sources) and `controller.split.speculation_resolved`
  (`winner`: `copy` | `original`).
  A `done` result whose digest the lane's own upload grant never stored is not
  committed: the first `max_unverified_results` (2) per (chunk, lane) are put
  back uncounted (a controller restart forgets what grants stored) and that
  lane does not get that chunk again for `wait_retry_s`; the next one is a
  counted, retryable failure (`output_error`, `chunk_unverified: chunk N ...`)
  under the same two-node / `retry.max_attempts` rules - so lanes that never
  really upload end the job `failed` in bounded time. The counters live in the
  controller process (a restart resets them; the job stays bounded).
  Planner knobs: `split_margin` (0.10), `target_chunk_s` (20),
  `max_overhead_frac` (0.05), `min_chunks_per_node` (4), `chunk_overhead_s`
  (1.5), `tail_frac` (0.5); `wait_retry_s` (2) is how long an idle lane waits
  while the rest is leased elsewhere. Caps (`[controller.split]`): `max_items` (100 000) items and
  `max_chunks` (10 000) chunks per job; `list_file` up to 64 MiB, `lines` files
  up to 256 MiB with lines up to 16 MiB (the file is scanned while blob GC
  waits), and the stored item list up to 64 MiB (counted while the items are
  resolved; never above `[controller.blobs] max_blob_bytes`).
- **Progress (V0.10 G7, D270).** `compute watch <JOB_ID>` (and `compute run -f`
  for a split job; `--no-progress` streams the logs instead) shows a bar and one
  line per node; the dashboard's job page has a **Chunks** tab (a grid of every
  chunk coloured by state, a per-node table, ETA, elapsed vs predicted).
  `GET /v1/jobs/{id}/split[?map=true]` is the summary and
  `GET /v1/jobs/{id}/chunks?state=&node=&cursor=&limit=` the chunks themselves
  (keyset pages of at most 500, index order); both read the rows on every
  request, so what they show equals the database (owner or admin only, like
  every job read). The event `split.progress` (WebSocket `/v1/events/ws`, the
  job's owner and admins) carries the same summary: the controller re-reads a
  split job when its chunks, lanes or state change, at most
  `[controller.split] progress_event_hz` (2) times a second per job - changes in
  between are merged into the next event, the last state is always sent - and
  re-reads every running split job every `progress_resync_s` (5 s; also the
  rebuild after a restart). A lane's items/s is measured over its last
  `progress_rate_window` (8) committed chunks; the ETA is the remaining items
  over the live lanes' measured rates and stays empty until a lane has
  committed (nothing assumed). `progress_max_jobs` (1 000) bounds the jobs whose
  last event is remembered. The CLI polls every `[cli] progress_interval_s` (1 s).
- **Chunk records and tries (V0.10 G8, D271/D261).** Every chunk try that ends
  with an applied result is a `chunk_record` row, written in the result's own
  transaction (docs/TELEMETRY.md). The agent sends the end of each chunk
  process's stderr (already redacted of the job's secrets; at most
  `[agent.split] chunk_stderr_tail_bytes`, 16 KiB, max 64 KiB) with its result;
  the controller keeps at most `[controller.split] chunk_stderr_tail_bytes`
  (16 KiB) of a failed try and `chunk_stderr_ok_bytes` (2 KiB) of a done one.
  `GET /v1/jobs/{id}/chunks/{n}/tries[?limit=20]` lists a chunk's tries (node,
  outcome, phases, stderr tail); the dashboard's chunk panel shows them.
  `compute telemetry export --table chunks -o chunks.parquet` exports the
  records (never the stderr). The dashboard refetches a job's chunk map at most
  once every 2 s (one trailing refetch, at once when the job ends).
- **Speculation reads (V0.10 G8, D285).** An idle lane's speculation check
  reuses a job's per-node chunk times while none of its chunks committed, at
  most `[controller.split] speculation_cache_s` (5 s; 0 = off), for at most
  `speculation_cache_jobs` (256) jobs.

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
that holder, that attempt, for at most 60 s (since V0.7 G3; a longer transfer
gets fresh tokens every 20 s while it runs), from a client presenting a node
certificate of this fabric that is not revoked **and is the node the token was
minted for** (a token captured by another laptop is refused `wrong_requester`).

**Clock skew (peer and probe tokens).** The controller stamps each token's
expiry with **its** clock; the peer that serves the blob checks it with **its
own** clock and allows 30 s of skew. So every laptop that serves blobs must keep
its clock within **30 s** of the controller's: a peer whose clock lags by more
than 30 s refuses fresh tokens (`too_long`), one that runs ahead by more than
about 70 s (90 s less the up-to-20 s age of the token in use) refuses them as
`expired`. Symptom: that peer's log shows `agent.peer.refused reason=expired`
or `too_long` for every fetch and transfers fall back to the controller (slower,
but jobs still succeed). Remedy: turn on time sync everywhere (Linux
`timedatectl set-ntp true`; Windows Settings > Time > "Set time automatically" /
`w32tm /resync`) and compare `date -u` on the laptops. The requesting laptop's
clock does not matter. The controller keeps the
signing key in `<data dir>/controller/peer_signing.key` (keep it private, like
`fabric.token`). The controller lists a peer only when the address it advertises
is the address its connection comes from; for a NAT'd lab setup set
`[controller] trust_peer_endpoints = true` on the controller. Behind a port
forward, `[agent.peer] advertise_port` sends the forwarded port instead of the
bound one (peers check the certificate, not the address).

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

### 0f. Run records and the telemetry dataset (V0.9, D211-D213 - [TELEMETRY.md](TELEMETRY.md))

Every finished attempt (done, failed, lost, fenced) gets one **run record** in PostgreSQL, written by a
background sweep in the controller - nothing to turn on. The dataset for the planner is exported from those
records as one Parquet file, **admin only**:

```
compute telemetry export -o all.parquet                    # every record
compute telemetry export --campaign CAMPAIGN_ID -o c.parquet
compute telemetry export --since 2026-10-18T00:00:00Z -o recent.parquet
```

The same records always give the same file, byte for byte. Tuning (rarely needed), in the controller's
config:

```toml
[controller.telemetry]
# record_interval_s = 2.0     # how often finished attempts without a record are looked for
# record_batch = 200          # records written per sweep transaction (a backlog sweeps again at once)
# export_max_rows = 100000    # most records one export may hold (it is built in memory)
```

| Limit | Default | Range | What it bounds |
|---|---|---|---|
| `[controller.telemetry] record_interval_s` | 2.0 s | 0.1 - 60 | delay from an attempt ending to its record (a terminal report also wakes the sweep) |
| `[controller.telemetry] record_batch` | 200 | 1 - 5 000 | rows per sweep transaction |
| `[controller.telemetry] export_max_rows` | 100 000 | 1 - 10 000 000 | records in one export; more -> `413`, narrow it with `--campaign` / `--since`. The records are loaded (about 2.5 KB each, ~0.25 GB at the default), the file is streamed one row group at a time (V0.9 G4) |
| skipped records excluded by id (code: `MAX_SKIPPED_IDS`) | 1 000 | fixed | records the sweep could not build or PostgreSQL refused, each raised as a `telemetry_record_skipped` alert and retried after a restart; past 1 000, **one** `skip_cap_reached` alert, then only log lines, and the sweep scans forward to the newest and only then starts again from the oldest, at most once a minute (`PAST_CAP_RESCAN_S`) |
| Parquet row group (code: `ROW_GROUP_SIZE`) | 65 536 rows | fixed | the file layout; the export builds Arrow data one row group at a time |
| clock-offset window (`session/clocks.py`) | last 32 Ping/Pong exchanges, at most 300 s old | fixed | the node clock offset stored with each record; a bound above 250 ms raises a `clock_uncertain` alert |
| `compute_iterations` from a workload (agent) | 1 - 100 000 | fixed | a benchmark's iteration count; anything else is stored as null |

**When something is wrong.** `GET /v1/alerts` (dashboard: the Alerts panel on Overview, admins) shows `telemetry_record_skipped` (a run is
missing from the dataset - the alert names the attempt and why) and `clock_uncertain` (a laptop's clock offset
cannot be trusted; its records still carry their monotonic durations). The controller log has
`controller.telemetry.record_skipped`, `...skip_cap_reached`, `...sweep_failed` (database unavailable; the next
sweep retries everything).

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
| A job fails at once with `disk_full` / `disk_floor: ...` (or right at start) | the laptop's workdir disk already had less free than the 1 GiB staging floor when the job started (V0.7 G5/G6): staging checks the floor before fetching, the running-job poll checks it again every `output_check_interval_s` | free disk on that laptop (`df -h <data dir>/work`), or move `[agent.execution] workdir_root` to a bigger local disk; an auto job is retried elsewhere meanwhile (D126) |
| A job fails `disk_full` / `disk_unknown: ...` | the agent could not read the free space of the workdir's disk `free_disk_read_failure_limit` times in a row (a vanished mount, an unreachable or hanging network share - `cause=timeout` in the log -, an I/O error) | check the agent log `agent.execution.free_disk_read_failed` for the OS error; keep `workdir_root` on a local disk |
| `compute run` refused `invalid_input` `secret_missing:NAME`, or a job fails with it | the submitting user has no secret `NAME` (or it was deleted before the job started) - secrets are per user | `compute secret list`; `compute secret set NAME` as the user who submits |
| A job fails `config_error` `secrets_need_mtls` | controller or agent runs `[security] mode = "insecure-dev"` | use mTLS (the default); for one-machine tests only: `[security] allow_insecure_secrets = true` on both |
| A job fails `integrity_error` `secret_unreadable` / `config_error` `secret_key_mismatch` / `secret_key_unusable` | the stored ciphertext does not match its owner/name (database edited or restored from elsewhere), the database belongs to another `secrets.key`, or the key file is loose/missing-and-unwritable | restore the matching `secrets.key` from backup, or `compute secret set NAME` again; check the controller log `controller.jobs.secrets_refused` |
| A job fails `config_error` `secrets_base_refused:<reason>`; agent log `agent.execution.secrets_base_refused` | the laptop's secrets folder is a link, not a directory, another user's, or not mode `0700` (someone else on the machine created `/dev/shm/meshforge-secrets-<uid>-...` first, or its mode was changed) | §0a1b: inspect it, remove the planted name or `chmod 700` your own folder; the next job works without a restart. Jobs without secrets are not affected |
| Agent log `agent.execution.secret_write_pending` | writing a job's secret files did not finish within `[agent.execution] secret_write_wait_s` (a hung mount under `secrets_root`) | the cancel / stop went on; the files are wiped when the write returns, or at the next agent start. Check the mount; prefer the default `/dev/shm` |
| `compute logs` shows `*` and `[meshforge: log redaction failed closed - output withheld]` | the secret scrubber stopped itself for that attempt (a bug, fail closed) | §0a1b; report the warning `...redaction_failed_closed` from the agent / controller log |
| Agent log `agent.fs.cleanup_failed` / `agent.envs.cleanup_failed` | a job folder or build leftover could not be deleted (files owned by another user, a read-only mount) | it is removed before the next build / at the next agent start; if it repeats, check who owns `path` under the data dir |
| A job fails `env_error` `requirements_rejected`: "a wheel URL must pin its file with `--hash=...`" | wheels-only laptop, a `pkg @ https://...whl` line without a strong digest | add `--hash=sha256:<sha256sum of the wheel>` to that line (§0b2) |
| A job fails `invalid_input` `secret_not_text:NAME` | an `as_env` secret is not UTF-8 text | read it from its file instead (drop `as_env`) |
| A job stays `queued` | no node can take it **now** (V0.6): every capable node is busy (`slots_full`, `node_cap`), offline / suspect, or its environment failed there recently (`env_known_bad`) | `compute explain <JOB_ID>` lists each node's reason; free a slot, start an agent, raise `[agent.execution] max_cpu_jobs` / `max_gpu_jobs`, or cancel it |
| `compute run` exits with `no_feasible_plan` and a list of nodes | **no** enrolled node could ever run it (V0.6 D104): no `exec`, no GPU / too little VRAM / RAM / CPUs, no runtime for `env.base`, the owner allows no jobs of that kind | each line names the node and the reason; change the manifest's `resources:` or enable execution / a runtime on a node |
| `compute run` prints `local agent cache not seeded (...)` | the CLI could not copy the project into this laptop's agent cache: `no_cache` (the agent never ran with this data dir), `other_user` (the agent's cache belongs to another OS user - e.g. the agent runs as a service account or you ran `compute run` as root; V0.7 G5 never writes into it), `not_writable`, `over_quota` (project > `[cli] seed_max_bytes`), `disk_full`, `changed` (a file changed while copied) | nothing breaks: a job placed here downloads the project; fix the reason to save that download |
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
