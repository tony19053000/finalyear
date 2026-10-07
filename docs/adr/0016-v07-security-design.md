# ADR-0016: V0.7 security design — fabric CA, code pairing, mTLS on every plane, per-user tokens, owner policy, job secrets

> **Status:** Accepted (2026-10-07, team approved the V0.7 brief "go with your recommendations") · **Deciders:** team + Lead agent
> Supersedes [ADR-0014](0014-interim-execution-trust.md) (interim fabric token). Refines [ADR-0008](0008-pairing-and-mtls.md).

## Context
V0.7 (roadmap 0.7.1–0.7.12) makes "only enrolled devices participate; all traffic authenticated and encrypted;
workloads constrained" true. Today (V0.6) every plane is plaintext, the controller trusts `Hello.node_id`, REST uses
one shared fabric token, and an agent trusts whichever controller it reaches (ADR-0014's accepted gaps).
Full detail and per-decision reasoning: `docs/briefs/V0.7-security-pairing.md` §2 (D133–D146).

## Decision
1. **Threat model** `docs/THREAT_MODEL.md` (STRIDE per plane, each row → test or accepted with reason).
2. **Fabric CA**: self-signed ECDSA P-256 root (10 y) on the controller host only, no intermediate; controller server cert 1 y, auto-renew at ⅔.
3. **Pairing**: join code `mf1-…` (≈ 60 chars) = controller address + 128-bit single-use token (stored hashed, TTL 15 min default) + 128-bit CA fingerprint. `Enroll(token, CSR)` is the only unauthenticated RPC; the agent checks the CA fingerprint before sending the token. CSR signed by the ADR-0013 key; node cert 30 d, SAN URI `meshforge://fabric/<fabric_id>/node/<node_id>`.
4. **mTLS** on agent gRPC (node_id taken from the cert, must equal Hello), HTTPS on controller REST/blobs, TLS + required client cert on peer servers. Plain HTTP only in `insecure-dev` mode, loopback-only.
5. **Revocation**: DB flag + immediate live-session kill; `RevocationUpdate` pushed to agents for the peer plane; no CRL/OCSP. **Rotation**: node certs renew at ⅔ life over mTLS; expired → explicit re-join. **CA rotation deferred to V0.17**; a leaked CA in V0.7 = `fabric create --force` + re-join all.
6. **Users**: `app_user` (scrypt hashes), `compute login` → opaque 256-bit API tokens (hashed, 90 d, revocable); jobs owned by users; admin role for nodes/invites/users. **Fabric token**: off-by-default fallback in V0.7, deleted in V0.8.
7. **Owner policy** is authoritative on the agent (`[agent.policy]`), advertised to the planner and re-checked at `StartAttempt`; caps enforced by runtime limits.
8. **Sandbox**: V0.4 hardening + gaps (seccomp, pids, tmpfs, output cap, network opt-in by job and owner), proven by escape tests. **Subprocess runtime (incl. Windows) stays "unsandboxed, owner opt-in"**; no AppContainer work.
9. **Job secrets**: AES-256-GCM at rest with a controller master key, delivered only inside `StartAttempt` over mTLS, tmpfs files by default, exact-value (+ encoded forms) redaction before any log write.
10. **Browser dashboard (V0.8)**: users install `ca.crt` once via a guided step. Join QR only in the V0.8 dashboard (no `qrcode` dependency now).

## Alternatives considered
PAKE short codes (ADR-0008 rejected PAKE-only); CRL/OCSP distribution (no outside verifier needs it); intermediate/offline CA (one controller, no gain); keeping the shared fabric token (no per-user identity/audit); public-CA certs for the dashboard (LAN hosts have no public names); AppContainer sandbox on Windows (large Windows-only effort); argon2 (new dependency; stdlib scrypt suffices).

## Trade-offs
+ No new dependencies; standard X.509/TLS; revocation immediate on the control plane; owner keeps final say on their laptop.
− Every lab laptop must re-join once; certificate lifecycle to operate; CA compromise recovery is a full re-join until V0.17; a workload can still deliberately leak its own secret; Windows/subprocess jobs are not sandboxed.

## Consequences
New migrations (join_token, node cert columns, app_user, api_token, job owner, secret), proto RPCs (`Enroll`, `RenewCertificate`, `RevocationUpdate`, `PolicyUpdate`), CLI commands (`fabric create/invite/join`, `node revoke`, `login`, `tokens`, `secret`), ARCHITECTURE §16/§23 updates, new task 0.7.12 🧑 lab. Revisit if the fabric ever spans networks the team does not control, or if multiple controllers are introduced.
