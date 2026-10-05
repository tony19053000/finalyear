# ADR-0015: Retries are at-least-once execution, exactly-once commit

> **Status:** Proposed (2026-10-05) — pending team approval · **Deciders:** team + Lead agent

## Context
V0.6 (roadmap 0.6.7, 0.6.8) turns a lost or retryable attempt into a new attempt elsewhere. Success criterion 1 says "0 lost, 0 duplicated executions". A laptop that is **killed** cannot run twice, but a laptop that is **partitioned** (Wi-Fi drops, lease expires) keeps running its workload while the controller, correctly, retries it on another node. No scheduler can prevent that overlap without the workload's cooperation. Brief: `docs/briefs/V0.6-scheduling-reliability.md` D110, D117.

## Decision
1. Guarantee **at-least-once execution, exactly-once commit**. Each attempt has a monotonic fencing token; only the current attempt's reports can change the job; a superseded attempt's reports are fenced and it is told `CancelAttempt{release}` when it reconnects.
2. An attempt is `LOST` only on agent-caused OFFLINE (Goodbye or lease expiry), the orphan sweep after `reconnect_grace_s`, or acked-but-not-held at Reconcile — never on SUSPECT or a controller shutdown (unchanged from V0.4 D66).
3. Ending an attempt and scheduling its retry is **one database transaction**.
4. Retry policy per failure type (ARCHITECTURE §13 table; per-instance `retryable` wins), manifest `retry: {max_attempts, backoff_s, on_lost}`; `on_lost: false` lets a non-idempotent job fail instead of retrying after a loss.
5. Criterion 1 is measured as duplicate **commits** (must be 0) and duplicate executions **under kills** (must be 0); overlaps under partitions are counted and reported, not hidden.

## Alternatives considered
- **Never retry LOST** — no overlaps, but every Wi-Fi drop fails a job; defeats V0.6.
- **Wait for the old node to come back before retrying** — unbounded delay; a laptop that left never returns.
- **Distributed lock / lease held by the agent that it must renew to keep running** (agent self-kills when its lease lapses) — reduces overlap to the lease skew, but kills good work on every blip and still cannot be exact. Kept as a possible later option (`self_fence` setting) if lab data shows overlaps matter.

## Trade-offs
+ Simple, provable invariant (one committed result) with existing fencing. + Honest metric.
− A partitioned non-idempotent workload may run twice unless the user opts out with `on_lost: false`.

## Consequences
ARCHITECTURE §12.1/§13 amended in V0.6 G3; chaos suite (D118) asserts the invariants; lab checklist 0.6.13 reports overlaps separately. Revisit if the lab shows frequent overlaps on Wi-Fi.

## Addendum 2026-10-05 (V0.6 G1, D114)
- Upload-grant refresh on Reconcile fires when there is no live grant **or** the last grant-carrying message (StartAttempt, re-sent StartAttempt, RefreshUploadGrant) was never acked — a safe superset of D114.
- A blob `PUT` answered **401 is retryable** (the grant may have been re-minted); 403 stays final. V0.7 authorization must keep this distinction.
- An in-flight upload finished under a replaced grant is credited to the successor only if it fits the successor's quota.
