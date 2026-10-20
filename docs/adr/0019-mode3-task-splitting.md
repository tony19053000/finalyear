# ADR-0019: Mode 3 Task Splitting — lanes, leased chunks, deterministic combine

> **Status:** Proposed (2026-10-19) — Lead draft; team said proceed, review on return · **Deciders:** team + Lead agent
> Brief: `docs/briefs/V0.10-task-splitting.md` (D259–D276). Builds on ADR-0006, ADR-0007, ADR-0015, ADR-0018.

## Context
Mode 3 is the flagship mode (ADR-0012): one task, divided across several heterogeneous laptops, results combined. ADR-0006
fixed the principle (pull-based leased work queue, speculative tail copies, shared with Mode 4) but not the execution
shape, the manifest contract, how the planner compares splitting with not splitting, where combine runs, or what
"identical output" and the speed criteria mean on unequal hardware.

## Decision
1. **One job → lanes → chunks.** The planner picks a node set; each node runs one **lane attempt** (normal attempt:
   fencing token, slot, sandbox, grants, owner guard) that stages the project and builds the env once, then **pulls**
   chunks and runs the entrypoint as a **fresh process per chunk**. A final **combine attempt** on an agent produces the
   job output.
2. **`split:` manifest** (`over` glob/list/list_file/range/lines, `chunk` auto/items, `combine`
   concat/merge-csv/merge-json/script). Items are resolved **once, at submit**, sorted bytewise, stored as a CAS blob.
3. **Shared `meshforge.workqueue`** (pure core + Postgres store, `SKIP LOCKED`): per-item lease sequence numbers,
   visibility timeout, ≤ 2 live leases (speculation), **idempotent completion — first committed result wins**.
4. **Pull protocol on the existing session stream**; results are buffered/acked reports; every message fenced by lane
   token and lease seq. Lane LOST releases its leases in the same transaction; visibility timeout covers partitions.
5. **Planner:** candidates Local, Mode 1 per node ("don't split" = one lane, one chunk, same code path) and Mode 3 over
   rate-ordered prefix sets; split chosen only if predicted ≥ 10 % faster; chunk size from target chunk time, overhead
   bound and ≥ 4 chunks per node.
6. **Combine** in index order on an agent (never on the controller); built-ins refuse ambiguity (header mismatch,
   duplicate keys) instead of guessing. A failing item fails the job — no partial output labelled success.
7. **Criteria calibrated** (brief D274): identity = byte-identical output on a deterministic workload; makespan bound
   includes unavoidable env/stage/combine; speedup judged relative to Σ rates / best rate.

## Alternatives
- **Child job per chunk** — maximal reuse, but per-chunk planning/staging/env checks; overhead dominates small chunks.
- **Static proportional shares** — no balancing for stragglers or slowdowns (rejected in ADR-0006).
- **Controller push assignment** — controller must model every node's speed precisely; pull does it for free.
- **Persistent user worker process** — amortises model load, but needs user code to speak a protocol; deferred option.
- **Combine on the controller** — saves one dispatch, but parses untrusted data and could run user scripts there.

## Trade-offs
+ Every Mode 1 guarantee (fencing, LOST, security, owner guard) applies to lanes unchanged; + one queue for Mode 3 and
Mode 4; + deterministic, testable output. − Process start + model load per chunk (mitigated by chunk sizing);
− a malicious lane can return wrong bytes for its chunk (only cross-checked when speculation duplicates it; owner trust
model documented); − fixed node set per job in V0.10 (re-plan only when all lanes are lost).

## Consequences
New tables `work_queue`/`work_item`/`work_lease` (migration 0025) and split/lane/chunk-record fields; proto chunk
messages; telemetry schema v3 with `chunk_record`; ARCHITECTURE §6.1, §7, §9, §12.2, §13 amended as groups land;
THREAT_MODEL gains a Mode 3 section; V0.12 (Mode 4 inference) reuses `meshforge.workqueue` without changes.

## Addendum (G2 review, 2026-10-19): work-queue lock protocol — binding for Mode 3 and Mode 4

- Every work-queue operation locks **queue rows in ascending `queue_id`, each once, then item rows in ascending `id`**.
- Queue row mode: `lease_next`/`renew`/`release` = FOR KEY SHARE (items via SKIP LOCKED — pulls never wait);
  `complete`/`speculate`/`release_holder` = FOR NO KEY UPDATE (serialises one queue's commits so the last
  commit sees `queue_done`); `fail`/`expire_due`/`cancel` = FOR UPDATE (no lease granted while closing).
- **Cross-module order:** a caller that also locks controller rows (job, attempt) in the same transaction locks
  **controller rows first, work-queue rows second** — e.g. node loss: attempt rows, then `release_holder`.
- Tables live in their own MetaData (same naming convention); `migrations/env.py` merges them for Alembic.
- Keep result transactions short: commits within one queue serialise on its row.

## Addendum (G5 review, 2026-10-20): the combine attempt — owed, placed, refused

- **I1 "combine owed" (D283 pin 1 refined).** The last chunk commit writes the parts index and moves the job
  `RUNNING -> COLLECTING`, but does **not** insert the combine attempt: placement belongs to the dispatcher
  (feasibility, owner policies, waiting when nothing fits — pin 2), and `attempt.node_id` is NOT NULL. The
  durable fact is "COLLECTING + parts index + no live `role='combine'` attempt" = the job owes a combine
  (`split/combine.py::owed_jobs`); the dispatch tick creates it under its advisory lock with the next fencing
  token. A crash, a lost combine or a retry all lead back to the same query; the service wakes the dispatcher
  in the commit handler so the gap is milliseconds.
- **I12 own draining lanes free their slots for the job's combine.** Every chunk is committed when the combine
  is owed, so the job's still-live lanes only drain. Placement (planner snapshot) and the tick's defensive
  over-booking check treat the slots of *that job's* live lanes as free — **only lanes of the combine's own slot
  kind** (a combine needs no GPU; a draining GPU lane frees no CPU slot and stays in `live_attempts`).
- **B1 a combine refused `resource_busy` is not a combine failure.** The agent may still count the draining
  lane against `max_concurrent_attempts` and refuse. Such a refusal (FAILED, not LOST) is **not counted** against
  `retry.max_attempts`, is **not** a soft exclusion of the node (`combine_failures`), and the combine is placed
  again after the fixed `[controller.split] combine_busy_backoff_s` (1 s), one `controller.split.combine_busy`
  log per refusal. A job has no deadline of its own, so the free refusals are bounded by
  `combine_busy_max` (100) per job; later refusals count like any retryable failure. A cancel request still
  wins over a busy re-placement.
- **N1 the parts-index blob is written inside the result transaction.** `ChunkStore._finish` stores the parts
  index in the controller CAS before the transaction commits (it must exist before any combine can read it).
  A rollback after the write leaves an unreferenced, content-addressed blob that GC reclaims; a retry of the
  same commit writes the same bytes (same digest). The blob write is small (one line per chunk, <= 10 000) and
  is the only file I/O in that transaction.
- **N3 brief over-booking with a stale-lease process.** I12 frees a draining lane's slot at once; if that lane's
  chunk process outlived its lease (a hung chunk the visibility timeout fenced, still being killed), the node can
  briefly run that process and the combine together — one slot over its advertised count until the lane ends.
  Accepted: bounded by the lane's end (its kill path), never more than one extra process per job, and the agent's
  own `max_concurrent_attempts` check (refused `resource_busy`, handled above) is the hard limit.
