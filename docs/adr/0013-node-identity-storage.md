# ADR-0013: Node identity — a per-node ECDSA P-256 keypair and a ULID `node_id`, minted once on first start and stored OS-protected

> **Status:** Proposed (2026-09-20) — pending team approval · **Deciders:** team + Lead agent

## Context

Roadmap task **0.3.2**: *"Agent identity: keypair + `node_id` generated on first start, stored in OS-appropriate data dir with restricted permissions."* V0.3 success criterion 4: *an agent restart keeps the same `node_id`.* ADR-0008 (Accepted) already commits that *"node keypairs exist from V0.3 so V0.7 adds certificates without redesign"*: at pairing the agent sends a **CSR** signed with this key and receives a node certificate; every plane then authenticates with mTLS.

So the V0.3 identity material has three consumers with different needs: the **control plane** now (a stable `node_id` to key the registry, sessions and telemetry on); **pairing** in V0.7 (a key that can sign a CSR and that every TLS stack we use will accept in a client certificate); and **the operator** (the file must survive upgrades, be recoverable if the agent is reinstalled, and never leak).

Constraints:

- **gRPC's TLS is BoringSSL**, not the system OpenSSL. BoringSSL negotiates Ed25519 signatures in TLS 1.3 but does not verify Ed25519 *certificates* by default; RSA and ECDSA P-256 are accepted everywhere (grpcio on Linux and Windows, Python `ssl`, uvicorn, browsers for the dashboard). Choosing a key type that one of our TLS stacks rejects would force a key change at V0.7 — exactly the redesign ADR-0008 wants to avoid.
- The Python standard library has **no** asymmetric cryptography. Generating a keypair needs a dependency.
- Three of four team laptops are **Windows** (ADR-0009: native Windows agent). POSIX `0600` does not exist there; file protection is an ACL.
- `node_id` is used in logs, URLs, the DB and the dashboard. A key-derived identifier (fingerprint) would change if the key is ever rotated (ADR-0008 supports rotation), and a 64-hex fingerprint is unpleasant everywhere a ULID is already the house format (`common/ids.py`).

## Decision

1. **Key: ECDSA on P-256 (secp256r1)**, generated with the `cryptography` package on first agent start, stored as an unencrypted **PKCS#8 PEM** file. It is the key the V0.7 CSR is signed with and the key inside the node certificate; nothing about it changes at pairing.
2. **`node_id`: a ULID** from `common.ids.new_node_id()`, **minted once** when the key is created and stored beside it. It is *not* derived from the key. V0.7 binds `node_id` → key → certificate at pairing (the certificate carries `node_id` in its SAN/CN); until then the controller trusts `Hello.node_id` on the **trusted LAN only**, and the code says so at the trust point.
3. **`session_epoch`: an unsigned counter** stored in the same file, incremented and persisted **before** each new stream is opened (see ADR-0002 Updates 2026-09-20). It is part of identity because it must survive a process restart.
4. **Location:** `platform_dirs().data / "identity" /` — `node.key` (PEM) and `node.json` (`{"node_id", "session_epoch", "created_at", "key_fingerprint_sha256", "format": 1}`). The fingerprint is a cross-check so a swapped key file is detected, not the identity.
5. **Protection:**
   - POSIX: directory `0700`, files `0600`, created with `os.open(..., O_CREAT|O_EXCL, 0o600)` — never `chmod` after the fact.
   - Windows: after writing, `icacls <path> /inheritance:r /grant:r "*<current user SID>:(F)"` (stdlib `subprocess`; SID obtained via `whoami /user /fo csv` or `win32security` if pywin32 is present) so only the current user has access; failure to restrict is a **fatal `IdentityError`**, not a warning.
   - Both: writes are atomic (temp file in the same directory + `os.replace`), so a crash mid-write leaves either the old identity or none — never a half-written one.
6. **Failure semantics:** a missing directory → create; a missing `node.json` **with** an existing `node.key` (or the reverse) → `IdentityError("partial identity")` and stop — never silently mint a new `node_id` next to an old key; unreadable / corrupt / wrong-`format` files → `IdentityError`. A fresh identity is only ever created when **neither** file exists. Every `IdentityError` names the path and the remedy (`meshforge-agent identity reset` — V0.3 provides it as an explicit, confirmed command; deleting the directory by hand has the same effect).
7. **Logging:** `node_id` and the key fingerprint are logged at start; key material never is (`common.redaction` already scrubs PEM blocks — keep the test that proves it).

## Alternatives considered

- **Ed25519.** Smaller, faster, simpler. Rejected for now because of BoringSSL's certificate-verification default in grpcio: it would work for signing and might be made to work for mTLS with extra configuration on both ends, but "might" is the wrong word for the trust anchor. Revisit if grpcio's verification defaults change.
- **RSA-2048/3072.** Universally accepted; 10× slower keygen and larger handshakes for no gain over P-256. Rejected.
- **`node_id` = key fingerprint.** Self-certifying and needs no second file. Rejected: breaks on key rotation (ADR-0008), and clashes with the ULID house format that ids, URLs and the DB already use.
- **`node_id` = hostname or MAC.** Not stable (Wi-Fi vs Ethernet MACs, hostname changes, two laptops named `DESKTOP-…`). Rejected.
- **Controller-assigned `node_id` at first Hello.** Makes first contact stateful and creates a duplicate-on-retry hazard before the id is persisted. Rejected; the agent owns its identity, the controller owns enrollment (V0.7).
- **Encrypted key file (passphrase).** A headless service cannot type a passphrase; a passphrase stored next to the key protects nothing. OS file protection is the honest layer; disk encryption is the operator's.
- **OS keystores** (Windows DPAPI / CNG key containers, Linux keyring). Stronger at rest, but two platform code paths, awkward for CSR generation with `cryptography`, and hard to back up or inspect. Deferred: the file layout above can be swapped for a keystore later without changing `node_id`.

## Trade-offs

+ One key type that every TLS stack in the system accepts; no key change at V0.7.
+ Stable, log-friendly `node_id`; rotation-safe; house-format ids.
+ Explicit failure on partial or tampered identity — no accidental identity forks.
− Adds `cryptography` to the **core** dependencies (agent runs everywhere). It ships binary wheels for all our platforms and is already a transitive dependency of common tooling; still, it is a new supply-chain item.
− Windows protection depends on `icacls` (locale-independent when the SID form is used) and is tested only on Windows CI; a wrong ACL is fatal by design, which can block a first start on a misconfigured machine.
− Until V0.7, `node_id` is a claim, not a proof. Acceptable on a trusted LAN in development; the trust point is one function and is marked.

## Consequences

- `src/meshforge/agent/identity.py` (roadmap **0.3.2**): `load_or_create(dir) -> NodeIdentity{node_id, private_key, session_epoch, fingerprint}`, `IdentityError`, `next_epoch()`, `reset()`. Tests: same `node_id` across two loads; partial identity refused; POSIX modes asserted; Windows ACL asserted (skipped elsewhere with a reason); corrupt files refused; atomicity under a killed writer.
- `pyproject.toml`: `cryptography` moves into core `dependencies` (record the version floor and why in the commit).
- `docs/ARCHITECTURE.md` §16: one sentence that the node keypair is P-256, created at first start (this ADR), used unchanged by pairing (ADR-0008); §3 node-agent row unchanged.
- ADR-0008 gains an *Updates* line pointing here for the key type.
- CLI: `meshforge-agent identity show|reset` (V0.3, group G1/G5).
- **Revisit** if: grpcio/BoringSSL accepts Ed25519 certificates by default; the team adopts an OS keystore; or V0.7 pairing needs a different binding than SAN = `node_id`.
