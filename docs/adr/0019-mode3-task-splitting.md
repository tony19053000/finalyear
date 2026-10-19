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
