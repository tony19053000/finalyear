"""V0.9 G3 lane A item 0.6 (D228.4): the inference reference uses
:func:`ml_tolerance` - the one tolerance rule the training check uses too - so a
change to the rule cannot leave one of the two checks behind."""

from __future__ import annotations

from typing import Any

import pytest

torch = pytest.importorskip("torch")

from meshforge.bench.workloads.kernels import ml  # noqa: E402

pytestmark = pytest.mark.unit

CASES = {
    "ml.cnn_infer": {"batch": 4, "width": 16, "depth": 2, "image": 32, "batches": 2},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 16,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}


def _run(family: str) -> tuple[Any, Any]:
    kernel = ml.KERNELS[family]
    state = kernel.prepare(CASES[family], 5, "cpu")
    kernel.warmup(state)
    return state, kernel.compute(state)


@pytest.mark.parametrize("family", sorted(CASES))
def test_inference_reference_goes_through_ml_tolerance(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, out = _run(family)
    calls: list[tuple[float, int]] = []
    real = ml.ml_tolerance

    def spy(eps: float, layers: int, scale: Any) -> Any:
        calls.append((eps, layers))
        return real(eps, layers, scale)

    monkeypatch.setattr(ml, "ml_tolerance", spy)
    result = ml._inference_reference(state, out)
    assert not isinstance(result, ml.Check)
    assert calls == [(2.0**-24, state["layers"])]


@pytest.mark.parametrize("family", sorted(CASES))
def test_the_units_scale_with_the_tolerance_rule(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doubling the rule's answer halves the reported units: the bound really is
    ``ml_tolerance``, not a private copy of the formula."""
    state, out = _run(family)
    perturbed = out + 1e-5 * out.abs().amax()
    units, _rows = ml._inference_reference(state, perturbed)
    real = ml.ml_tolerance
    monkeypatch.setattr(ml, "ml_tolerance", lambda e, n, s: 2 * real(e, n, s))
    halved, _rows = ml._inference_reference(state, perturbed)
    assert units > 0
    assert halved == pytest.approx(units / 2, rel=1e-9)


def test_a_factor_change_moves_the_inference_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, out = _run("ml.cnn_infer")
    perturbed = out + 1e-5 * out.abs().amax()
    before, _ = ml._inference_reference(state, perturbed)
    monkeypatch.setattr(ml, "ML_FACTOR", ml.ML_FACTOR * 4)
    after, _ = ml._inference_reference(state, perturbed)
    assert after == pytest.approx(before / 4, rel=1e-9)
    assert ml.ml_tolerance(2.0**-24, 4, 1.0) == pytest.approx(4 * 16.0 * 2.0**-24 * 2.0)
