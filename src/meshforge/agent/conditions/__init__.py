"""Benchmark condition controllers on the agent (V0.9 G4, roadmap 0.9.5, D217/D241).

``runner`` (the executor's entry point: prepare / apply+verify / after_stage /
finish = revert + report, start-up sweep, owner-pause hook), ``load``
(``cpu_load`` / ``gpu_load`` burner processes), ``netem`` (``tc netem`` through
one fixed command), ``cache`` (``cache:cold`` eviction, cache verification),
``marker`` (the persisted "applied" record), ``burn`` (the load child process).

Kept import-free on purpose: ``python -m meshforge.agent.conditions.burn``
must start fast and load nothing of the agent.
"""
