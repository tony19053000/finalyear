"""Garbage collection of the agent's caches (V0.5 G5, D91).

* :mod:`.index` - the SQLite last-use index, pins and leases (cross-process).
* :mod:`.collector` - one LRU pass per kind (blobs, environments) under quota.
* :mod:`.leases` - what a running attempt holds (never evicted).
* :mod:`.service` - the periodic / on-demand task and controller pins.

Import the submodules directly (no re-exports: :mod:`.leases` is imported by
the environment builder, which :mod:`.collector` depends on).
"""
