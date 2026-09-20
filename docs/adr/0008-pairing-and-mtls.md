# ADR-0008: Pairing with a fabric CA and mTLS

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
The system executes code remotely by design. Only enrolled devices may participate; the network is untrusted; a short human pairing code must not be the permanent trust model.

## Decision
`compute fabric create` generates a **fabric CA**. `invite` produces a short-lived single-use **join code** (address + token + CA fingerprint). The joining agent generates its own keypair, verifies the CA fingerprint, sends a **CSR + token**, and receives a **node certificate**. All planes use **mTLS** / node-cert auth; GPU and blob access use short-lived scoped tokens; revocation list checked on every session.

## Alternatives considered
Shared secret for all nodes; SSH keys; account-based cloud identity; PAKE-only.

## Trade-offs
+ standard, strong, offline-capable. − certificate lifecycle to manage (rotation, revocation).

## Consequences
Node keypairs exist from V0.3 so V0.7 adds certificates without redesign.

## Updates
- 2026-09-20: the node keypair is **ECDSA P-256**, created at first agent start and stored OS-protected — [ADR-0013](0013-node-identity-storage.md). Pairing signs the CSR with that key unchanged; the certificate binds `node_id` (a ULID, not a key fingerprint) in its SAN.
