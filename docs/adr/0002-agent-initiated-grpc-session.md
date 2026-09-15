# ADR-0002: Agent-initiated bidirectional gRPC session

> **Status:** Proposed (2026-09-15) — pending team approval · **Deciders:** team + architect agent

## Context
Windows firewalls, WSL2 NAT and home routers make inbound connections to laptops unreliable. The controller must push commands and receive heartbeats, metrics and logs.

## Decision
Each agent **dials out** to the controller and keeps one long-lived **bidirectional gRPC stream** (`AgentControl.Session`). Commands flow down, status/heartbeats/logs flow up. Liveness = heartbeat every 2 s, lease = 3 heartbeats. Reconnect with exponential backoff + jitter, followed by a `Reconcile` message.

## Alternatives considered
Controller polls agents (needs inbound ports); REST long-polling; message broker (NATS/Redis) — extra infrastructure.

## Trade-offs
+ works through NAT/firewalls; + one connection per node; + typed contracts. − stream management and backpressure must be implemented carefully.

## Consequences
Control plane needs no inbound ports on agents. Data plane and GPU plane still need inbound ports (ADR-0003, ADR-0005) — handled by firewall rules/installer.
