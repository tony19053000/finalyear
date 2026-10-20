"""V0.10 G6 tester r1: speculation settings reject non-finite / wrong-typed values
and accept the exact bounds."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from meshforge.controller.settings import SplitSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("v", [math.nan, math.inf, -math.inf, -1e-9, 1.0000001])
def test_max_frac_rejects(v: float) -> None:
    with pytest.raises(ValidationError):
        SplitSettings(speculative_max_frac=v)


@pytest.mark.parametrize("v", [math.nan, math.inf, 0.0, 0.999999, 100.0001, -1.5])
def test_factor_rejects(v: float) -> None:
    with pytest.raises(ValidationError):
        SplitSettings(speculation_factor=v)


def test_exact_bounds_accepted() -> None:
    assert SplitSettings(speculative_max_frac=0.0).speculative_max_frac == 0.0
    assert SplitSettings(speculative_max_frac=1.0).speculative_max_frac == 1.0
    assert SplitSettings(speculation_factor=1.0).speculation_factor == 1.0
    assert SplitSettings(speculation_factor=100.0).speculation_factor == 100.0


@pytest.mark.parametrize("v", ["0.1x", None, [0.1]])
def test_wrong_types_rejected(v: object) -> None:
    with pytest.raises(ValidationError):
        SplitSettings(speculative_max_frac=v)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SplitSettings(speculation_factor=v)  # type: ignore[arg-type]
