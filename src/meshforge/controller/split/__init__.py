"""Mode 3 task splitting on the controller (V0.10 G4, ADR-0019, D259-D267).

A split job runs as one **lane** attempt per node of its node set; each lane
stages the project once and pulls **chunks** (work-queue items of
:mod:`meshforge.workqueue`, one per ``chunk_items`` consecutive items) until
none is left. Modules:

* :mod:`.placement` - the node set and chunk size (placeholder for G3's planner
  Mode 3 - the seam);
* :mod:`.lanes` - lane rows inside the dispatcher's transactions: queue +
  lanes at dispatch, a lane's end (its leases released in the same
  transaction, D267a), re-plan of the remainder when no lane is left (D267e);
* :mod:`.store` - the chunk protocol's database side: grants, results (commit
  + the job's completion in one transaction), renewals, expiry;
* :mod:`.service` - the dispatcher-side handling of the chunk messages
  (fencing checks, upload-grant verification, replies, counters).

Nothing here runs user code or reads chunk *contents*: the controller only
moves digests.
"""
