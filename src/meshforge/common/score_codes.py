"""Hardware-score reason codes, shared by the agent (which sets them) and the
controller (which validates and stores them) - roadmap 0.4.17 item 4.

A metric that is not ``ok`` carries one of these codes in ``Score.reason``
(``agent.proto``); the controller stores it in ``node.hardware_scores`` and V0.15's
predictors will train on it. The values are therefore **frozen**, like
:class:`~meshforge.common.errors.FailureType`: never rename or remove one, only
add (``tests/unit/common/test_score_codes.py`` pins the full list). The agent's
``meshforge.agent.benchmarks.model.MetricReason`` *is* this enum.

The controller's ingestion check (``controller/session/limits.py``) accepts only
:data:`SCORE_REASON_CODES` (V0.4 D60): an unknown code drops the whole set.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "SCORE_GPU_NOT_DECLARED",
    "SCORE_REASON_CODES",
    "ScoreReason",
]


class ScoreReason(StrEnum):
    """Why a metric is not ``ok``. Values are permanent (DB rows, ML labels)."""

    # --- UNAVAILABLE: this machine cannot measure it ---------------------------------
    NO_BLAS_BACKEND = "no_blas_backend"
    TORCH_NOT_INSTALLED = "torch_not_installed"
    CUDA_UNAVAILABLE = "cuda_unavailable"
    UNSUPPORTED_DTYPE = "unsupported_dtype"
    # --- FAILED: a measurement was attempted and broke --------------------------------
    DEVICE_MISMATCH = "device_mismatch"
    INSUFFICIENT_VRAM = "insufficient_vram"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CHILD_CRASHED = "child_crashed"
    CHILD_NOT_STARTED = "child_not_started"
    BAD_ANSWER = "bad_answer"
    BAD_SAMPLE = "bad_sample"
    GPU_SETUP_FAILED = "gpu_setup_failed"
    GPU_REP_FAILED = "gpu_rep_failed"
    BENCHMARK_ERROR = "benchmark_error"


SCORE_REASON_CODES: Final[frozenset[str]] = frozenset(r.value for r in ScoreReason)
"""Every valid ``Score.reason`` string, for membership checks at the trust boundary."""

SCORE_GPU_NOT_DECLARED: Final = "gpu_index_not_declared"
"""Controller-side *rejection* code (brief D39): a GPU metric names a GPU the
session's Hello did not declare. Not a metric reason - the whole set is refused."""
