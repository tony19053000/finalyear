# ADR-0014: Interim trust for remote execution (fabric token + agent opt-in) until V0.7

> **Status:** Superseded by [ADR-0016](0016-v07-security-design.md) (2026-10-07) · was Proposed (2026-09-28) · **Deciders:** team + Lead agent
>
> **V0.7 G4 note (2026-10-11):** decision 1 is replaced by per-user API tokens (D141, `compute login`;
> ARCHITECTURE §16 G4). The fabric token is still created and rotatable, but the REST API accepts it
> only behind `[controller.auth] legacy_fabric_token = true` - **off by default** - and then as an admin
> with a warning log and a `legacy_token_used` audit row. Its code is deleted in V0.8 (Q4). Decisions 2
> and 3 (agent opt-in, scoped fetch tokens) stand.

## Context
V0.4 (roadmap 0.4.3–0.4.6) is the first version where a REST request makes code run on another laptop. CLAUDE.md §9:
the system is remote code execution by design and must never be designed for *unauthenticated* arbitrary code
execution. Real identity (pairing, mTLS, authorization) is V0.7 (ADR-0008). In V0.3 the REST surface is read-only
and unauthenticated (brief D14), and agents trust whichever controller they connect to — including one found by
mDNS. Both are acceptable for telemetry, not for execution. Details: `docs/briefs/V0.4-remote-execution.md` §2b.

## Decision
1. **Fabric token (D44).** The controller creates a 32-byte random token on first start (`fabric.token` in its data
   dir, owner-only permissions, atomic create), shown by `meshforge-controller token show`, replaced by `token rotate`.
   Every mutating REST call, every `/v1/jobs*` read and every `/v1/blobs*` call requires `Authorization: Bearer
   <token>` (constant-time compare; failure = `unauthorized`, 401). Node/session GETs stay open (D14). The CLI reads
   it from config, env or a file — never from a URL, never logged.
2. **Agent opt-in (D45).** `[agent.execution] enabled = false` by default. Only an enabled agent advertises `exec`;
   the controller never dispatches to a node without it; a disabled agent refuses `StartAttempt` with
   `FAILED(unauthorized)`. Unsandboxed (subprocess) execution needs a second opt-in, `allow_unsandboxed`. Enabled
   together with mDNS discovery logs a loud warning at start.
3. **Scoped fetch tokens (D48).** Agents download bundles with a per-attempt token bound to that attempt's digests
   and lifetime; they never hold the fabric token.

## Alternatives considered
- **No auth until V0.7** — anyone on the Wi-Fi could run code on four laptops. Rejected (CLAUDE.md §9).
- **Pull V0.7 pairing + mTLS forward** — the right end state, but ~11 tasks before any Mode 1 feature, against a pace
  already at half of plan (SESSION_HANDOFF §0d).
- **Per-user API keys** — needs user accounts and a key-management surface; V0.7/V0.8 material.
- **Pin the controller address when execution is enabled (forbid mDNS)** — safer against a rogue controller, but it
  breaks the zero-config lab setup; a warning plus V0.7's mTLS is the proportionate step.

## Trade-offs
+ Small (one file, one dependency-free check), honest, and V0.7 replaces it without changing call sites (same header).
+ Every laptop owner decides whether their machine runs other people's code.
− One shared secret: no per-user identity or audit; anyone holding it can submit. Rotation is manual.
− The agent still trusts the controller it connects to; a rogue controller on the LAN could send work to an enabled
  agent until V0.7. Documented, warned about, not solved.

## Consequences
- New: `fabric.token` (ARCHITECTURE §23), `meshforge-controller token show|rotate`, `[agent.execution]` settings,
  capability tags `exec`, `exec-docker`, `exec-subprocess`, `exec-gpu`, `FailureType.unauthorized` on the job API.
- V0.7 must supersede this ADR: mTLS identity for agents, and user/API-key identity for REST.
- Revisit earlier if the lab ever runs on a network the team does not control.

## Amendment 2026-11-03 (V1.0 G8 F5, D428) - CAN vs ALLOWS, look only, act later

*Supersedes in part* D45 ("only an enabled agent advertises the `exec` capability"; "`allow_unsandboxed = false`:
`exec-subprocess` is not advertised") and D160 / V0.7 G5 fix r1 ("`policy set`, then restart once").

- **Capabilities say what the agent CAN run**, the owner's live policy (`[agent.policy]` + `policy.toml`) says what it
  **ALLOWS**. An agent advertises `exec*` for the runtimes it found even while `accept_jobs` is off; the controller
  refuses `policy_not_accepting`, the agent refuses every attempt. Turning Run jobs (or Allow jobs without a
  container) on applies live, no restart.
- **Run jobs off = look only** (team decision). With `accept_jobs` off at start the agent performs read-only checks
  only: Docker API (ping, version, info, local image lookup; GPU pass-through assumed from the registered runtimes),
  `uv --version`, NVIDIA GPU discovery/NVML reads, `tc qdisc show`. It does **not** start any container (GPU-probe
  container, orphan sweep) and does **not** write cgroups (`cgroup.subtree_control`, leftover sweep).
- **First Run jobs on = act, once**: the side-effecting probes run, the full setup replaces the look-only one, and
  the session is handed over so a fresh Hello carries the new capabilities (`agent/execution/activation.py`).
- Unchanged: the controller never dispatches to a node without `exec`; an agent never runs an attempt its current
  policy refuses; fail-closed on a broken policy (`DENY_ALL`, no slots); the unpinned-controller warning (now also at
  every off -> on).
