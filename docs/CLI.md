# `compute` - command-line reference (V0.8)

`compute` is the fabric's command-line tool. It talks only to the controller's REST API
(`https://HOST:47110/v1`), the same API the dashboard uses, so everything here works from any laptop with a
plain install (`uv sync`). Setup and login: [OPERATIONS.md](OPERATIONS.md) ("Users and logging in").

This page is the contract for **scripts**: what goes to stdout and stderr, the `--json` documents, the exit
codes and shell completion. Unit tests keep it in step with the code (`tests/unit/cli/test_v08_g6_*.py`).

## 1. Commands

| Command | Does | `--json` |
|---|---|---|
| `compute status` | where the controller is, whether it answers, node counts | yes |
| `compute nodes [NODE_ID] [--watch]` | the node table, or one node's detail and hardware scores | yes |
| `compute jobs [JOB_ID] [--state S] [--limit N]` | recent jobs, or one job (attempts, inputs, transfers) | yes |
| `compute explain JOB_ID [--attempt N]` | why the planner placed the job where it did | yes |
| `compute network` | measured bandwidth/RTT between nodes | yes |
| `compute whoami` | who you are logged in as | yes |
| `compute tokens list [--all]` | your API tokens and browser sessions | yes |
| `compute secret list` | your secrets' names (never values) | yes |
| `compute data ls` / `compute data show NAME` | named inputs (datasets, models) | yes |
| `compute fabric invites` | join-code invites (codes are never shown again) | yes |
| `compute logs JOB_ID [-f] [--stream S]` | the job's own output, byte for byte | no - it is the workload's bytes |
| `compute run [PATH] [--node ID / --local] [-f] ...` | submit a project (prints the job id) | no - stdout is the job id |
| `compute cancel JOB_ID` | cancel a job | no |
| `compute get JOB_ID [--dest DIR] [--force]` | download and unpack a job's outputs | no |
| `compute login` / `logout` | log in (API token kept privately) / revoke it | no |
| `compute tokens revoke ID` | revoke a token | no |
| `compute secret set NAME` / `delete NAME` | store (value from stdin or `--from-file`) / delete | no |
| `compute data push NAME DIR` / `pin REF` / `rm REF` | register / pin / remove a named input | no |
| `compute node prepare|gc [NODE_ID / --all]` | pre-pull images and envs / run a cache GC pass | no |
| `compute node revoke NODE_ID` | revoke a node for good | yes |
| `compute fabric invite [--ttl] [--replace NODE_ID]` | make a single-use join code | yes |
| `compute fabric invites --revoke ID` | revoke an invite | yes |
| `compute fabric join [CODE]` | join this laptop's agent (runs `meshforge-agent join`) | no |

Every command has `--help`. Global options go before the command: `compute --ca ca.crt --token-file F nodes`.

## 2. Output rules

- **stdout carries the result** (a table, the job id, the JSON document); **stderr carries progress, advice and
  errors**, each line starting `compute: `. `compute run ... > id.txt` therefore captures exactly the job id.
- Tables use plain ASCII borders, so they look the same in Windows Terminal, `cmd.exe`, a pipe and a log file.
- **Colour**: none when stdout is not a terminal, none when `NO_COLOR` is set to any non-empty value
  (<https://no-color.org>). Nothing depends on colour: every state is written out in words.
- Text that came from the network (host names, error messages, job names) has its control characters replaced by
  spaces before printing, so it cannot move the cursor or fake a line.
- Error lines never contain a token, join code or private key: they are scrubbed like the logs.

## 3. `--json`

Every read command (and the write commands marked "yes" above) takes `--json`. The rules:

1. **One line** on stdout, compact separators, **keys sorted** - safe for `jq`, `grep` and line-based tools.
2. The document is the controller's REST answer for that call (the field list and types are in its OpenAPI
   description, `GET /v1/openapi.json`), plus a top-level **`schema_version`** (currently **1**).
   `compute status --json` is the one document the CLI builds itself (fields below).
3. **Stability:** fields are only ever *added* within one `schema_version`. Removing, renaming or changing the
   type of a field listed below raises `schema_version`. A script should ignore fields it does not know.
4. **Errors are not JSON:** a failing command prints nothing on stdout, one `compute: ...` line on stderr, and
   exits with the code from section 4. Check the exit code first. The one exception is `compute status`: telling
   you whether the controller answers is its job, so it always prints its document and still exits 3 or 6.

Guaranteed fields per command (`a[].b` = field `b` of every item of list `a`; `a.b` = field `b` of object `a`):

| Command | Source | Guaranteed fields |
|---|---|---|
| `compute status` | built by the CLI | `controller_url`, `reachable`, `detail`, `live_sessions`, `nodes` |
| `compute nodes` | `GET /v1/nodes` | `nodes`, `count`, `nodes[].node_id`, `nodes[].hostname`, `nodes[].state`, `nodes[].connected`, `nodes[].sample_age_s`, `nodes[].cpu_percent`, `nodes[].ram_used_bytes`, `nodes[].ram_total_bytes`, `nodes[].gpus`, `nodes[].scores`, `nodes[].not_enforced` |
| `compute nodes NODE_ID` | `GET /v1/nodes/{node_id}` | `node_id`, `hostname`, `os`, `platform`, `state`, `connected`, `last_seen_at`, `sample_age_s`, `agent_version`, `hardware`, `scores`, `owner_policy`, `not_enforced` |
| `compute jobs` | `GET /v1/jobs` | `jobs`, `count`, `next_cursor`, `jobs[].job_id`, `jobs[].name`, `jobs[].state`, `jobs[].placement`, `jobs[].failure_type`, `jobs[].created_at` |
| `compute jobs JOB_ID` | `GET /v1/jobs/{job_id}` | `job_id`, `name`, `state`, `failure_type`, `failure_detail`, `placement`, `created_at`, `updated_at`, `attempts`, `inputs`, `transitions` |
| `compute explain JOB_ID` | `GET /v1/jobs/{job_id}/decisions` | `job_id`, `decisions`, `count`, `decisions[].policy`, `decisions[].outcome`, `decisions[].chosen_node_id`, `decisions[].candidates`, `decisions[].actual` |
| `compute network` | `GET /v1/network` | `now`, `stale_after_s`, `links`, `controller_links`, `failures`, `prober` |
| `compute whoami` | `GET /v1/auth/whoami` | `user`, `user.name`, `user.role`, `legacy`, `token_id`, `token_expires_at` |
| `compute tokens list` | `GET /v1/auth/tokens` | `tokens`, `count`, `tokens[].id`, `tokens[].user`, `tokens[].name`, `tokens[].kind`, `tokens[].state`, `tokens[].current`, `tokens[].expires_at` |
| `compute secret list` | `GET /v1/secrets` | `secrets`, `count`, `secrets[].name`, `secrets[].created_at`, `secrets[].updated_at` |
| `compute data ls` | `GET /v1/data` | `inputs`, `count`, `inputs[].ref`, `inputs[].name`, `inputs[].version`, `inputs[].tree_id`, `inputs[].size`, `inputs[].files`, `inputs[].pinned` |
| `compute data show NAME` | `GET /v1/data/{name}` | `name`, `versions`, `versions[].ref`, `versions[].tree_id`, `versions[].pinned` |
| `compute fabric invites` | `GET /v1/fabric/invites` | `invites`, `invites[].id`, `invites[].state`, `invites[].expires_at`, `invites[].used_by_node` |
| `compute fabric invites --revoke ID` | `POST /v1/fabric/invites/{invite_id}/revoke` | `id`, `state`, `revoked_at` |
| `compute fabric invite` | `POST /v1/fabric/invites` | `code`, `invite_id`, `address`, `ca_pin`, `expires_at` |
| `compute node revoke NODE_ID` | `POST /v1/nodes/{node_id}/revoke` | `node_id`, `state`, `already_revoked`, `session_killed` |

`compute status --json`: `controller_url` (the address used), `reachable` (`false` only when the controller
could not be reached at all - exit 6), `detail` (the error line, else `null`), `live_sessions` (from
`/healthz`), `nodes` (the `compute nodes` document without `schema_version`, or `null`).

Example:

```bash
compute jobs --state running --json | jq -r '.jobs[].job_id'
```

## 4. Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | OK | done |
| 1 | FAILURE | the command ran and failed for a reason not listed below (the controller refused for another reason, a local file could not be read or written, a node failed to prepare, an internal error) |
| 2 | USAGE_ERROR | wrong arguments or options, or the controller rejected a value we sent (`invalid_input`) |
| 3 | AUTH | not logged in, login expired or revoked, wrong password, not allowed (401/403), or the controller's CA does not match the fingerprint given |
| 4 | CONFIG_ERROR | the local configuration is unusable (`config.toml`, the token file, the CA file, the credentials folder) |
| 6 | UNAVAILABLE | the controller could not be reached, did not answer in time, or cannot answer right now (503) - retrying later may work |
| 8 | NOT_FOUND | the job, node, token, secret, input or invite does not exist |
| 126 | JOB_FAILED | `run -f`: the fabric could not run or finish the job (stderr names the failure type) |
| 128 | JOB_CANCELLED | `run -f`: the job was cancelled |
| 130 | INTERRUPTED | Ctrl-C while following (`run -f`, `logs -f`): the job keeps running |
| 1-125 | (workload) | `run -f`: the job's own program exited with this code (clamped to 125) |

`compute fabric join` runs the agent's `join` and exits with the agent's code (same table: 4 configuration,
5 already enrolled, 6 controller unreachable).

The numbers are shared with `meshforge-controller` and `meshforge-agent`, which is why "unreachable" is 6 and
not found is 8 (4 and 5 were already taken by configuration and "already enrolled"). Source of truth:
`src/meshforge/cli/exits.py`.

```bash
compute jobs "$JOB" --json > job.json
case $? in
  0) ;;                       # use job.json
  3) compute login ;;         # log in again
  6) sleep 10; retry ;;       # controller down or busy
  8) echo "no such job" ;;
  *) exit 1 ;;
esac
```

## 5. Errors

Every expected failure is **one line** on stderr that says what failed, why, and what to do, for example:

```text
compute: cannot reach the controller at https://192.168.1.20:47110 (ConnectError) - is it running and is the address right? `compute status` shows which address is used ([cli] controller_url, or the one `compute login` stored)
compute: the controller refused the request (401): you are not logged in, or your login expired or was revoked - run `compute login`
compute: the controller refused the request (404): no such job - check the id or name (`compute jobs`, `compute nodes`, `compute data ls`...)
```

`compute` never prints a Python traceback for these. An unexpected error (a bug) is also one line,
`compute: internal error (TypeName) - this is a bug ...`, exit 1; run the command again with `COMPUTE_DEBUG=1` to
see the traceback when reporting it.

## 6. Shell completion

Completion for commands, subcommands and options:

```bash
compute --install-completion     # bash, zsh, fish or PowerShell - detected from the shell you run it in
compute --show-completion        # print the script instead, to install it yourself
```

Restart the shell afterwards. Per shell:

- **bash** - writes `~/.bash_completions/compute.sh` and sources it from `~/.bashrc`.
- **zsh** - writes `~/.zfunc/_compute` and adds it to `fpath` in `~/.zshrc`.
- **fish** - writes `~/.config/fish/completions/compute.fish`.
- **PowerShell** (Windows, native) - adds a `Register-ArgumentCompleter` block to your PowerShell profile
  (`$PROFILE`) once; running it again does not add a second copy. It **never changes the execution policy**
  (Typer's own installer sets it to `Unrestricted`; `compute` replaces that step). If PowerShell then says
  running scripts is disabled, allow your own profile once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Completion runs `compute` itself with no network access, so it works with the controller down.

If installing fails - a file it must write is not writable, or PowerShell is missing or does not print a profile
path - `compute` prints one what / why / what-to-do line and **exits 4** (`CONFIG_ERROR`, §4), whatever the
shell. Re-running the install never adds a second line to `~/.bashrc`, whatever encoding the file is in.
