# ADR-0002: Agent-initiated bidirectional gRPC session

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

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

## Updates

- **2026-09-20 — session epoch, stream fencing and reconnect policy (Proposed, for V0.3 group G1/G3; refines "Reconnect with exponential backoff + jitter, followed by a Reconcile" — does not reverse it).** Closes the `TODO(V0.3)` markers in `proto/meshforge/v1/agent.proto` and the 0.1.5 carry-over in the roadmap design change log.
  1. **Session epoch.** The agent keeps a `session_epoch` counter in its identity file (ADR-0013), incremented and **persisted before** every new stream is opened, and sends it in `Hello`. The controller keeps at most one live session per `node_id`. A `Hello` with a **higher** epoch than the live session's closes the older stream (it is a zombie — the agent that owned it has moved on); a `Hello` with a **lower or equal** epoch is rejected `FAILED_PRECONDITION` (a stale process or a replay). Epochs never compare across different `node_id`s.
  2. **Session id on every envelope.** The controller answers `Hello` with `HelloAck{session_id, heartbeat_interval_ms, lease_ms, controller_time}`; both sides put that `session_id` in envelope field 3 of every later message. Messages carrying another session's id are dropped and counted (`session.stale_message`), never processed. Intervals are controller-owned so a fleet can be retuned without touching agents.
  3. **Reconnect policy.** Full-jitter exponential backoff: `sleep = uniform(0, min(cap, base · 2ⁿ))` with `base = 0.5 s`, `cap = 15 s` (V0.3 criterion 3 requires reconnection within 15 s of a controller restart, so the cap cannot exceed it) — **amended 2026-09-24 (V0.3 G6, D27): `cap = 5 s`**, because criterion 2 (restored link → ONLINE ≤ 10 s) is the tighter bound: after a long outage the next full-jitter sleep can be anything up to the cap, and nothing wakes the agent early when the link returns. 5 s keeps the worst case ≈ 6 s with margin; retrying every ≤ 5 s is negligible load even at 50 agents; `n` resets after a stream that lived ≥ 30 s. gRPC keepalive is enabled as well (`keepalive_time_ms = 10 000`, `keepalive_timeout_ms = 5 000`) so a dead TCP path is torn down by the transport, not only by the application lease.
  4. **Replay after reconnect.** The agent buffers un-acked reports in a **bounded** ring (default 10 000 messages; on overflow drop the oldest and increment `session.replay_dropped`). `Reconcile.replay_from_sequence` tells the controller the first sequence it will replay; the controller dedupes on `message_id` as before. Heartbeats are never buffered — a missed heartbeat is information, not a report.
  5. **Liveness clock.** Leases are measured with the controller's monotonic clock (`loop.time()`); wall-clock timestamps in messages are for RTT and display only. Defaults: heartbeat 2 s, SUSPECT after one missed heartbeat plus a 0.5 s allowance, OFFLINE at 3 × heartbeat = 6 s (criterion 2 asks ≤ 8 s).
  Evidence for the numbers is V0.3's cluster harness (0.3.14) and the 🧑 lab run (0.3.15); until then they are defaults, all of them settings.

*Planned amendment (G6 review, 2026-09-24):* a clean session end is today inferred on the controller from the absence of a handler cancel within `HALF_CLOSE_CONFIRM_S` (0.25 s) — measured grpcio 1.84 behaviour, hence the `grpcio<1.85` cap. Roadmap **0.4.16** replaces it with an explicit agent `Goodbye{reason}` message before the half-close; it must land before V0.6 makes OFFLINE trigger retries.
