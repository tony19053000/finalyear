"""Limits of the planner evaluator's ML part shared by the controller and
the CLI (V0.15 G5a, D368; the CLI may import only ``common``).

``POST /v1/planner/eval-reports`` trains one fold model set per evaluated
family before it replays the records. The controller stops training folds
once ``[controller.planner] ml_eval_timeout_s`` is spent (default
:data:`DEFAULT_ML_EVAL_TIMEOUT_S`); the CLI waits that long plus
:data:`EVAL_REPLAY_MARGIN_S` for the replay and the database writes.
"""

from __future__ import annotations

from typing import Final

__all__ = ["DEFAULT_ML_EVAL_TIMEOUT_S", "EVAL_REPLAY_MARGIN_S"]

DEFAULT_ML_EVAL_TIMEOUT_S: Final = 3000.0
"""Total fold-training budget of one evaluation, seconds."""
EVAL_REPLAY_MARGIN_S: Final = 600.0
"""What the CLI adds on top of the fold budget: dataset build, replay of
every record under every policy, storing the report."""
