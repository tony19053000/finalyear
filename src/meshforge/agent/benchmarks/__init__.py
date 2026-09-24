"""Hardware scores (roadmap 0.3.4, brief §5g D30-D38).

The agent measures CPU GFLOP/s, host memory bandwidth and GPU TFLOP/s in an
isolated **child process** (:mod:`.child`), plans the cases from what that child
can import (:mod:`.plan`), runs them with budgets and tree kills (:mod:`.runner`)
and records the result as a typed :class:`~.model.ScoreSet`.

Modules: :mod:`.scores` (formulas, ``SCORE_VERSION``), :mod:`.sizing` (problem
sizes), :mod:`.kernels` (child-only), :mod:`.child`, :mod:`.plan`, :mod:`.runner`,
:mod:`.model`. Nothing in this package imports torch or numpy at import time; only
the child process ever imports them.
"""
