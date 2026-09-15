# ADR-0001: Centralized control plane

> **Status:** Proposed (2026-09-15) — pending team approval · **Deciders:** team + architect agent

## Context
A four-person project needs a fabric that is easy to reason about, debug and test. Jobs, placement decisions, telemetry and node state need one consistent view.

## Decision
One **controller** process (FastAPI + gRPC in one asyncio loop) with **PostgreSQL** is the single source of truth for fabric state. It runs on one laptop (the one that created the fabric). Agents are the source of truth for what is *actually* running and reconcile on reconnect.

## Alternatives considered
Fully decentralized/peer-to-peer membership (gossip, Raft); multiple controllers with leader election.

## Trade-offs
+ simple consistency, easy debugging, straightforward planner. − controller laptop is a single point of failure; − controller must be reachable.

## Consequences
Controller failover is future work (documented). Agents keep running attempts and buffer reports while the controller is down, so a controller restart loses no work.
