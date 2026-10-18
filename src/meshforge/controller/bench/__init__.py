"""Benchmark campaigns on the controller (V0.9 G3, roadmap 0.9.3/0.9.4, D215/D216/D232).

* :mod:`.store` - ``bench_campaign`` / ``bench_run`` (migration 0022): a
  campaign's expanded runs and their states;
* :mod:`.submit` - forced runs: one planned run -> one ordinary job, pinned to
  its node, lowest priority, through the jobs API's own path.

The spec and its expansion are pure (:mod:`meshforge.bench.campaign`); the
controller imports ``meshforge.bench``, never the other way round (D232).
"""
