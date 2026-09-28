"""Roadmap 0.4.17 item 4: the score reason codes are shared and frozen.

The golden list below is the contract (DB rows in ``node.hardware_scores``, V0.15
ML labels). Adding a code means adding it here too; renaming or removing one must
fail this test.
"""

from __future__ import annotations

from meshforge.agent.benchmarks.model import MetricReason
from meshforge.common.score_codes import (
    SCORE_GPU_NOT_DECLARED,
    SCORE_REASON_CODES,
    ScoreReason,
)

FROZEN: tuple[tuple[str, str], ...] = (
    ("NO_BLAS_BACKEND", "no_blas_backend"),
    ("TORCH_NOT_INSTALLED", "torch_not_installed"),
    ("CUDA_UNAVAILABLE", "cuda_unavailable"),
    ("UNSUPPORTED_DTYPE", "unsupported_dtype"),
    ("DEVICE_MISMATCH", "device_mismatch"),
    ("INSUFFICIENT_VRAM", "insufficient_vram"),
    ("TIMEOUT", "timeout"),
    ("BUDGET_EXHAUSTED", "budget_exhausted"),
    ("CHILD_CRASHED", "child_crashed"),
    ("CHILD_NOT_STARTED", "child_not_started"),
    ("BAD_ANSWER", "bad_answer"),
    ("BAD_SAMPLE", "bad_sample"),
    ("GPU_SETUP_FAILED", "gpu_setup_failed"),
    ("GPU_REP_FAILED", "gpu_rep_failed"),
    ("BENCHMARK_ERROR", "benchmark_error"),
)


def test_every_frozen_code_is_present_with_its_value() -> None:
    have = {m.name: m.value for m in ScoreReason}
    for name, value in FROZEN:
        assert have.get(name) == value, f"{name} was renamed or removed"


def test_no_code_was_added_without_freezing_it() -> None:
    assert len(ScoreReason) == len(FROZEN)


def test_the_agent_uses_the_shared_enum() -> None:
    assert MetricReason is ScoreReason


def test_the_membership_set_matches_and_codes_are_wire_safe() -> None:
    assert frozenset(v for _, v in FROZEN) == SCORE_REASON_CODES
    for code in (*SCORE_REASON_CODES, SCORE_GPU_NOT_DECLARED):
        assert code.isascii() and code == code.lower() and 1 <= len(code) <= 64
        assert set(code) <= set("abcdefghijklmnopqrstuvwxyz0123456789_")
    assert SCORE_GPU_NOT_DECLARED not in SCORE_REASON_CODES
