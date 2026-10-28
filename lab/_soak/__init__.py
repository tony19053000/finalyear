"""The V1.0 soak + chaos kit (roadmap 1.0.2, brief V1.0 G1, D409-D411).

Three CLIs share this package:

* ``lab/soak.py run|report`` - per-laptop resource samplers (RSS, FDs / Windows
  handles, threads, CPU per process; controller DB size per table + WAL; disk
  folders) and the D410 analyzer (:mod:`.rules`) that turns every file of a run
  into one verdict JSON + a markdown report.
* ``lab/soak_load.py plan|run|collect`` - the seeded mixed job load (D409) with
  its submitted-jobs ledger (:mod:`.load`, :mod:`.ledger`).
* ``lab/chaos.py plan|run|show`` (+ ``lab/chaos_win.ps1`` on Windows) - the
  seeded chaos schedule and its action log (D411, :mod:`.chaos`).

Modules:

* :mod:`.stats` - medians, least-squares slope, time windows (pure).
* :mod:`.jsonl` - crash-safe append-only JSON-lines files (samples, ledger,
  action log): a torn last line after a power cut is skipped, never fatal.
* :mod:`.sampler` - the sampling loop (injected clock, process and DB sources).
* :mod:`.db` - the controller database probe (asyncpg, controller extra).
* :mod:`.load`, :mod:`.ledger` - the load plan and the ledger (pure + file).
* :mod:`.chaos` - catalogue, schedule, command templates, executors, log.
* :mod:`.rules` - the D410 pass rules with attribution (pure).
* :mod:`.report` - the verdict's markdown rendering (pure).

Process discovery reuses ``_fabric.rss`` (extended for the controller and
PostgreSQL); the fabric heartbeat record (D410 rule 7) is the existing
``fabric_check.py soak`` result, read by the analyzer - not re-implemented.
"""
