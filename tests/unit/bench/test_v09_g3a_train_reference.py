"""V0.9 G3 lane A item 0.3 (D228.1): the ``ml.cnn_train`` float64 first-loss
reference runs layer by layer with chunked convolutions, and the family refuses a
first batch whose reference would not fit 512 MB.

The peak memory itself is measured in a real process:
``tests/component/bench/test_v09_g3a_train_reference_memory.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

torch = pytest.importorskip("torch")

from meshforge.bench.workloads import ParamsError, get_family  # noqa: E402
from meshforge.bench.workloads.kernels import ml  # noqa: E402

pytestmark = pytest.mark.unit

FAM = "ml.cnn_train"


def _state(**params: Any) -> Any:
    full = {"batch": 8, "width": 16, "depth": 4, "image": 32, "steps": 20, **params}
    return ml._train_prepare(full, 7, "cpu")


def _plain_loss(state: Any) -> float:
    """The previous whole-batch module forward, as the oracle."""
    reference = ml._seeded_model(state).double().train()
    with torch.no_grad():
        logits = reference(state["xs"][0].cpu().double())
        return float(torch.nn.functional.cross_entropy(logits, state["ys"][0].cpu()))


@pytest.mark.parametrize("chunk_bytes", [1, 200_000, ml.REFERENCE_CHUNK_BYTES])
@pytest.mark.parametrize(("depth", "image"), [(2, 32), (5, 32), (4, 64)])
def test_chunked_reference_equals_the_whole_batch_forward(
    monkeypatch: pytest.MonkeyPatch, chunk_bytes: int, depth: int, image: int
) -> None:
    state = _state(depth=depth, image=image, batch=12)
    expected = _plain_loss(state)
    monkeypatch.setattr(ml, "REFERENCE_CHUNK_BYTES", chunk_bytes)
    got = ml._first_loss_reference(state)
    # same float64 maths, only the GEMM blocking differs: far below the
    # float32 tolerance the check uses (~1e-6 relative)
    assert got == pytest.approx(expected, rel=1e-12, abs=1e-12)


def test_chunking_really_splits_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows per chunk = 1 when the budget is tiny: the conv is called per row."""
    state = _state(batch=5)
    calls: list[int] = []
    real = torch.nn.Conv2d.forward

    def counted(self: Any, x: Any) -> Any:
        calls.append(int(x.shape[0]))
        return real(self, x)

    monkeypatch.setattr(ml, "REFERENCE_CHUNK_BYTES", 1)
    monkeypatch.setattr(torch.nn.Conv2d, "forward", counted)
    ml._first_loss_reference(state)
    assert calls and set(calls) == {1} and len(calls) == 5 * 4


def test_the_reference_still_catches_a_wrong_first_loss() -> None:
    state = _state()
    kernel = ml.KERNELS[FAM]
    kernel.warmup(state)
    losses = kernel.compute(state)
    assert kernel.check(state, losses).ok
    tampered = losses.clone()
    tampered[0] = tampered[0] * 1.001
    check = kernel.check(state, tampered)
    assert not check.ok and "float64 reference" in check.detail


def test_the_family_refuses_a_first_batch_above_the_cap() -> None:
    family = get_family(FAM)
    over = {"batch": 128, "width": 256, "depth": 2, "image": 32, "steps": 20}
    assert ml.TRAIN_REFERENCE_ELEMENTS < 128 * 256 * 32**2
    with pytest.raises(ParamsError) as info:
        family.parse_params(over)
    assert "float64 first-loss check" in str(info.value)
    assert "512 MB" in str(info.value)
    # the hint of an in-bounds combination stays computable (campaign planning)
    assert family.expected_runtime_s(over) > 0


@pytest.mark.parametrize(
    "params",
    [
        {"batch": 64, "width": 256, "depth": 2, "image": 32, "steps": 100},
        {"batch": 16, "width": 256, "depth": 2, "image": 64, "steps": 100},
        {"batch": 512, "width": 32, "depth": 12, "image": 32, "steps": 100},
    ],
)
def test_the_cap_itself_is_accepted(params: dict[str, Any]) -> None:
    assert (
        params["batch"] * params["width"] * params["image"] ** 2
        == ml.TRAIN_REFERENCE_ELEMENTS
    )
    get_family(FAM).parse_params(params)


def test_one_error_lists_the_cap_and_the_window_together() -> None:
    family = get_family(FAM)
    huge = {"batch": 512, "width": 256, "depth": 12, "image": 64, "steps": 50_000}
    with pytest.raises(ParamsError) as info:
        family.parse_params(huge)
    text = str(info.value)
    assert "float64 first-loss check" in text and "reference mid laptop" in text
