"""Helpers for the V0.3 lab kit (roadmap 0.3.15): ``lab/fabric_check.py`` and
``lab/agent_rss.py``.

* :mod:`.api` - a stdlib (``urllib``) client for the controller's read-only REST
  ``/v1`` surface, with typed failures (never a traceback for "controller down").
* :mod:`.clock` - the injectable monotonic + wall clock, sleep and operator marks.
* :mod:`.timeline` - pure change tracking and detection-latency maths.
* :mod:`.watch`, :mod:`.restart`, :mod:`.soak`, :mod:`.inventory` - one module per
  ``fabric_check`` subcommand, plain functions with injected I/O.
* :mod:`.rss` - finding the agent process and sampling its RSS (psutil, agent extra).
* :mod:`.reporting` - result envelopes and markdown reports (``spikes._harness``).
* :mod:`.checkpoint` - crash-safe JSON checkpoints for the long runs.

Nothing here imports ``meshforge``: the kit observes the fabric only through the
product's REST surface, like an operator would (decision D24).
"""
