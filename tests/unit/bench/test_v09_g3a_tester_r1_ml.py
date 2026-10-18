"""V0.9 G3 lane A tester r1 (items 0.3, 0.6): the cnn_train float64 reference
against a straightforward float64 forward at odd small sizes, the combination cap
(text and boundaries), and the inference check through ``ml_tolerance``."""

from __future__ import annotations

from typing import Any

import pytest

torch = pytest.importorskip("torch")

from meshforge.bench.workloads import ParamsError, get_family  # noqa: E402
from meshforge.bench.workloads.kernels import ml  # noqa: E402
from meshforge.bench.workloads.materialise import job_files  # noqa: E402

pytestmark = pytest.mark.unit

CAP = ml.TRAIN_REFERENCE_ELEMENTS


def _straightforward(state: Any) -> float:
    """float64 module forward on a fresh seeded build, nothing chunked."""
    model = ml._seeded_model(state).double().train()
    x = state["xs"][0].detach().cpu().double().clone()
    with torch.no_grad():
        logits = model(x)
        return float(torch.nn.functional.cross_entropy(logits, state["ys"][0].cpu()))


@pytest.mark.parametrize(
    ("batch", "width", "depth", "image"),
    [(8, 16, 2, 32), (9, 17, 3, 32), (11, 31, 2, 64), (8, 16, 12, 32), (13, 16, 4, 32)],
)
@pytest.mark.parametrize("chunk", [1, 1_000_003, 10**9])
def test_chunked_reference_matches_a_straightforward_float64_forward(
    monkeypatch: pytest.MonkeyPatch,
    batch: int,
    width: int,
    depth: int,
    image: int,
    chunk: int,
) -> None:
    params = {
        "batch": batch,
        "width": width,
        "depth": depth,
        "image": image,
        "steps": 20,
    }
    state = ml._train_prepare(params, 11, "cpu")
    before = state["xs"][0].clone()
    expected = _straightforward(state)
    monkeypatch.setattr(ml, "REFERENCE_CHUNK_BYTES", chunk)
    got = ml._first_loss_reference(state)
    assert got == pytest.approx(expected, rel=1e-12, abs=1e-12)
    # the in-place ReLU must never touch the timed run's input batch
    assert torch.equal(state["xs"][0], before)
    # and it is repeatable
    assert ml._first_loss_reference(state) == got


def test_a_real_run_passes_its_own_check_and_a_wrong_first_loss_fails() -> None:
    kernel = ml.KERNELS["ml.cnn_train"]
    params = {"batch": 8, "width": 16, "depth": 2, "image": 32, "steps": 20}
    state = kernel.prepare(params, 5, "cpu")
    kernel.warmup(state)
    # warm-up must not have changed the first-loss reference's inputs
    losses = kernel.compute(state)
    assert kernel.check(state, losses).ok
    bad = losses.clone()
    bad[0] = bad[0] * 1.01
    assert not kernel.check(state, bad).ok


@pytest.mark.parametrize(
    "fields",
    [
        {"batch": 65, "width": 256, "image": 32},
        {"batch": 17, "width": 256, "image": 64},
        {"batch": 512, "width": 64, "image": 32},
        {"batch": 513 - 1, "width": 256, "image": 64},
    ],
)
def test_one_over_the_cap_is_refused_with_the_numbers_and_the_advice(
    fields: dict[str, int],
) -> None:
    fam = get_family("ml.cnn_train")
    params = {"depth": 2, "steps": 100, **fields}
    values = fields["batch"] * fields["width"] * fields["image"] ** 2
    assert values > CAP
    with pytest.raises(ParamsError) as refused:
        fam.parse_params(params)
    text = str(refused.value)
    assert f"{values:,}" in text and f"{CAP:,}" in text
    assert "lower batch, width or image" in text
    # also through the job builder (the only way a campaign reaches it)
    with pytest.raises(ParamsError):
        job_files("ml.cnn_train", params, 1)


def test_the_exact_cap_is_accepted_at_every_factorisation() -> None:
    fam = get_family("ml.cnn_train")
    for batch, width, image in [(64, 256, 32), (16, 256, 64), (512, 32, 32)]:
        assert batch * width * image * image == CAP
        params = {"batch": batch, "width": width, "image": image, "depth": 2}
        # any window-valid steps will do; the cap rule must not be the problem
        assert not fam.combination_problems(fam._model({**params, "steps": 100}))
        steps = next(
            s
            for s in (100, 300, 1000, 3000, 10_000)
            if 5 <= fam.expected_runtime_s(fam._model({**params, "steps": s})) <= 180
        )
        fam.parse_params({**params, "steps": steps})


def test_the_hint_of_a_refused_combination_is_still_computable() -> None:
    fam = get_family("ml.cnn_train")
    model = fam._model({"batch": 512, "width": 256, "image": 64, "depth": 12})
    assert fam.combination_problems(model)
    assert fam.expected_runtime_s(model) > 0  # sizing code never breaks


def test_other_families_have_no_combination_rule() -> None:
    from meshforge.bench.workloads import list_families

    for fam in list_families():
        if fam.name != "ml.cnn_train":
            assert fam.combination_problems(fam.smallest_params()) == []


# ------------------------------------------------------------ ml_tolerance (0.6)

INFER = {
    "ml.cnn_infer": {"batch": 4, "width": 16, "depth": 2, "image": 32, "batches": 2},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 16,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}


@pytest.mark.parametrize("family", sorted(INFER))
def test_inference_check_accepts_the_truth_and_rejects_a_10x_tolerance_error(
    family: str,
) -> None:
    kernel = ml.KERNELS[family]
    state = kernel.prepare(INFER[family], 9, "cpu")
    kernel.warmup(state)
    out = kernel.compute(state)
    assert kernel.check(state, out).ok
    eps = ml.float32_eps(torch, "cpu", conv=state["conv"])
    per_row_scale = out.detach().double().flatten(1).abs().amax(dim=1)
    # an error of 20x the rule on every element of every row: must fail
    big = out + (20 * ml.ml_tolerance(eps, state["layers"], per_row_scale)).reshape(
        -1, *([1] * (out.dim() - 1))
    ).to(out.dtype)
    assert not kernel.check(state, big).ok
