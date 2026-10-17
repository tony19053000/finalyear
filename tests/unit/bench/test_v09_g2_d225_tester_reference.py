"""Tester attacks on the D225 float64 ML reference (items 1) - independent of
the implementer's tests: consistently wrong models the reference must catch,
false-positive sweeps at window corners, RNG / state leaks, sampled-row holes."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from meshforge.bench.workloads.kernels._support import Check

pytestmark = pytest.mark.unit

CNN = {"batch": 3, "width": 8, "depth": 3, "image": 32, "batches": 3}
TRF = {"batch": 3, "seq_len": 8, "width": 64, "depth": 2, "batches": 3}
TRAIN = {"batch": 16, "width": 8, "depth": 2, "image": 32, "steps": 40}
FAMILY_PARAMS = {"ml.cnn_infer": CNN, "ml.transformer_infer": TRF}


def _ml() -> Any:
    pytest.importorskip("torch", reason="needs torch (gpu extra)")
    from meshforge.bench.workloads.kernels import ml

    return ml


def _run(
    family: str,
    params: Mapping[str, Any],
    seed: int = 5,
    tamper: Callable[[Any], None] | None = None,
    pre_compute: Callable[[Any], None] | None = None,
) -> Check:
    kernel = _ml().KERNELS[family]
    state = kernel.prepare(params, seed, "cpu")
    if kernel.warmup is not None:
        kernel.warmup(state)
    if tamper is not None:
        tamper(state)
    out = kernel.compute(state)
    if pre_compute is not None:
        pre_compute(state)
    return kernel.check(state, out)


def _units(check: Check) -> float:
    match = re.search(r"reference ([0-9.]+) of tolerance", check.detail)
    assert match, check.detail
    return float(match.group(1))


# ------------------------------------------------ consistently wrong models


@pytest.mark.parametrize("family", sorted(FAMILY_PARAMS))
@pytest.mark.parametrize("rounding", ["half", "bfloat16"])
def test_output_rounded_through_low_precision_fails(family: str, rounding: str) -> None:
    """A kernel that computes in fp16/bf16 and widens the result: weights equal,
    rerun equal, dtype float32 - only the float64 reference can tell."""

    def tamper(state: Any) -> None:
        torch = state["torch"]
        dtype = getattr(torch, "float16" if rounding == "half" else "bfloat16")
        state["model"].register_forward_hook(
            lambda _m, _i, out: out.to(dtype).to(torch.float32)
        )

    check = _run(family, FAMILY_PARAMS[family], tamper=tamper)
    assert not check.ok, check.detail


def test_replaced_activation_in_timed_cnn_fails() -> None:
    def tamper(state: Any) -> None:
        torch = state["torch"]
        seq = state["model"]
        for i, m in enumerate(seq):
            if isinstance(m, torch.nn.ReLU):
                seq[i] = torch.nn.LeakyReLU(0.1)

    check = _run("ml.cnn_infer", CNN, tamper=tamper)
    assert not check.ok, check.detail


def test_replaced_activation_in_timed_transformer_fails() -> None:
    def tamper(state: Any) -> None:
        torch = state["torch"]
        for layer in state["model"][1].layers:
            layer.activation = torch.nn.functional.gelu

    check = _run("ml.transformer_infer", TRF, tamper=tamper)
    assert not check.ok, check.detail


def test_dropout_left_active_in_timed_transformer_fails() -> None:
    def tamper(state: Any) -> None:
        for m in state["model"].modules():
            if m.__class__.__name__ == "Dropout":
                m.p = 0.2
        state["model"].train()

    check = _run("ml.transformer_infer", TRF, tamper=tamper)
    assert not check.ok, check.detail


def test_cnn_left_in_train_mode_fails() -> None:
    """BatchNorm in train mode uses batch statistics and moves its buffers."""
    check = _run("ml.cnn_infer", CNN, tamper=lambda s: s["model"].train())
    assert not check.ok, check.detail


def test_weights_rounded_through_bfloat16_fail() -> None:
    def tamper(state: Any) -> None:
        torch = state["torch"]
        with torch.no_grad():
            for p in state["model"].parameters():
                p.copy_(p.to(torch.bfloat16).to(torch.float32))

    for family in FAMILY_PARAMS:
        check = _run(family, FAMILY_PARAMS[family], tamper=tamper)
        assert not check.ok, (family, check.detail)


def test_model_cast_to_double_is_refused_by_the_dtype_guard() -> None:
    def tamper(state: Any) -> None:
        state["model"].double()
        state["xs"] = [x.double() for x in state["xs"]]

    check = _run("ml.cnn_infer", CNN, tamper=tamper)
    assert not check.ok, check.detail


def test_timed_model_from_another_seed_fails() -> None:
    def tamper(state: Any) -> None:
        torch = state["torch"]
        torch.manual_seed(999)
        state["model"] = state["build"]()
        state["model"].eval()

    for family in FAMILY_PARAMS:
        check = _run(family, FAMILY_PARAMS[family], tamper=tamper)
        assert not check.ok, (family, check.detail)


def test_different_inputs_are_not_a_false_positive_or_negative() -> None:
    """The reference uses the timed run's own inputs: replacing xs consistently
    (data differs from the seed) must still pass - output matches those inputs."""

    def tamper(state: Any) -> None:
        state["xs"] = [x * 3.0 + 0.5 for x in state["xs"]]

    check = _run("ml.cnn_infer", CNN, tamper=tamper)
    assert check.ok, check.detail


def test_train_started_from_non_seeded_weights_fails() -> None:
    """Warm-up leakage: one real SGD step before the timed loop."""
    ml = _ml()
    kernel = ml.KERNELS["ml.cnn_train"]
    state = kernel.prepare(TRAIN, 5, "cpu")
    ml._train_step(state, 0)
    check = kernel.check(state, kernel.compute(state))
    assert not check.ok, check.detail


def test_train_loss_scaled_after_the_fact_fails() -> None:
    ml = _ml()
    kernel = ml.KERNELS["ml.cnn_train"]
    state = kernel.prepare(TRAIN, 5, "cpu")
    kernel.warmup(state)
    losses = kernel.compute(state)
    assert kernel.check(state, losses).ok
    bumped = losses.clone()
    bumped[0] *= 1.0001
    assert not kernel.check(state, bumped).ok


# ---------------------------------------------------------- RNG / state leaks


@pytest.mark.parametrize(
    "family", ["ml.cnn_infer", "ml.transformer_infer", "ml.cnn_train"]
)
def test_check_leaves_rng_and_timed_state_alone(family: str) -> None:
    import torch

    ml = _ml()
    params = TRAIN if family == "ml.cnn_train" else FAMILY_PARAMS[family]
    kernel = ml.KERNELS[family]
    state = kernel.prepare(params, 11, "cpu")
    if kernel.warmup:
        kernel.warmup(state)
    out = kernel.compute(state)
    weights = {k: v.clone() for k, v in state["model"].state_dict().items()}
    torch.manual_seed(77)
    before = torch.get_rng_state()
    first = kernel.check(state, out)
    assert torch.equal(before, torch.get_rng_state()), "CPU RNG leaked"
    after = dict(state["model"].state_dict().items())
    assert all(torch.equal(weights[k], after[k]) for k in weights), "model mutated"
    # Idempotent: a second check gives the same verdict, checksum and detail.
    second = kernel.check(state, out)
    assert (first.ok, first.checksum) == (second.ok, second.checksum)
    assert first.ok, first.detail


def test_prepare_with_different_global_rng_state_is_deterministic() -> None:
    """The reference must not depend on whatever the process RNG was doing."""
    import torch

    ml = _ml()
    kernel = ml.KERNELS["ml.cnn_infer"]
    torch.manual_seed(1)
    a = kernel.check(s := kernel.prepare(CNN, 4, "cpu"), kernel.compute(s))
    torch.manual_seed(2)
    torch.randn(1000)
    b = kernel.check(s := kernel.prepare(CNN, 4, "cpu"), kernel.compute(s))
    assert a.ok and b.ok and a.checksum == b.checksum


# -------------------------------------------- false positives at the corners

INFER_CORNERS = [
    ("ml.cnn_infer", {"batch": 1, "width": 16, "depth": 2, "image": 32, "batches": 1}),
    ("ml.cnn_infer", {"batch": 2, "width": 16, "depth": 12, "image": 64, "batches": 2}),
    ("ml.cnn_infer", {"batch": 5, "width": 256, "depth": 2, "image": 32, "batches": 1}),
    ("ml.cnn_infer", {"batch": 1, "width": 64, "depth": 12, "image": 32, "batches": 5}),
    (
        "ml.transformer_infer",
        {"batch": 1, "seq_len": 16, "width": 64, "depth": 1, "batches": 1},
    ),
    (
        "ml.transformer_infer",
        {"batch": 2, "seq_len": 16, "width": 64, "depth": 12, "batches": 2},
    ),
    (
        "ml.transformer_infer",
        {"batch": 1, "seq_len": 16, "width": 1024, "depth": 1, "batches": 1},
    ),
    (
        "ml.transformer_infer",
        {"batch": 1, "seq_len": 2048, "width": 64, "depth": 1, "batches": 1},
    ),
    (
        "ml.transformer_infer",
        {"batch": 2, "seq_len": 64, "width": 256, "depth": 6, "batches": 5},
    ),
]
TRAIN_CORNERS = [
    {"batch": 8, "width": 16, "depth": 2, "image": 32, "steps": 20},
    {"batch": 8, "width": 16, "depth": 12, "image": 64, "steps": 20},
    {"batch": 8, "width": 128, "depth": 3, "image": 32, "steps": 20},
    {"batch": 64, "width": 16, "depth": 4, "image": 32, "steps": 21},
]
SEEDS = [0, 1, 2, 3, 4, 2**32 - 1, 123456789]


@pytest.mark.parametrize(("family", "params"), INFER_CORNERS)
def test_inference_window_corners_never_false_positive(
    family: str, params: dict[str, Any]
) -> None:
    worst = 0.0
    for seed in SEEDS:
        check = _run(family, params, seed=seed)
        assert check.ok, (seed, check.detail)
        worst = max(worst, _units(check))
    assert worst < 0.5, f"less than 2x headroom: {worst}"


@pytest.mark.parametrize("params", TRAIN_CORNERS)
def test_training_window_corners_never_false_positive(params: dict[str, Any]) -> None:
    worst = 0.0
    for seed in SEEDS:
        check = _run("ml.cnn_train", params, seed=seed)
        assert check.ok, (seed, check.detail)
        worst = max(worst, _units(check))
    assert worst < 0.5, f"less than 2x headroom: {worst}"


# ------------------------------------------------------- sampled-row coverage


def test_sampled_rows_catch_an_edge_row_fault_when_budget_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ml = _ml()
    monkeypatch.setattr(ml, "REFERENCE_FLOPS", 1.0)  # first+last (+2 seeded, T1)
    params = {**CNN, "batch": 8}

    def tamper(state: Any) -> None:
        def hook(_m: Any, _i: Any, out: Any) -> Any:
            out = out.clone()
            out[-1] += 0.01
            return out

        state["model"].register_forward_hook(hook)

    check = _run("ml.cnn_infer", params, tamper=tamper)
    assert not check.ok and "on 4 row(s)" in check.detail


@pytest.mark.parametrize("seed", [0, 1, 2, 5, 11])
def test_fault_only_in_a_seeded_middle_row_is_caught(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 fixed: when sampling, seeded random rows are added to first + last.

    The faulty row is taken from the same selector the check uses (a middle row
    the run's seed samples), so the test is deterministic; the next test makes
    the campaign-level claim that every middle row is reached by some seed.
    """
    ml = _ml()
    monkeypatch.setattr(ml, "REFERENCE_FLOPS", 1.0)  # first + last + seeded rows
    params = {**CNN, "batch": 8}
    middle = [r for r in ml.reference_rows(8, 1e30, seed) if r not in (0, 7)]
    assert len(middle) == ml.SAMPLED_ROWS

    def tamper(state: Any) -> None:
        def hook(_m: Any, _i: Any, out: Any) -> Any:
            out = out.clone()
            out[middle[0]] += 0.01
            return out

        state["model"].register_forward_hook(hook)

    check = _run("ml.cnn_infer", params, seed=seed, tamper=tamper)
    assert not check.ok, check.detail
    assert "on 4 row(s)" in check.detail


def test_every_middle_row_is_sampled_by_some_seed_of_a_campaign() -> None:
    """A fault confined to fixed middle rows cannot hide from a whole campaign:
    across seeds the sampled rows cover the batch; one seed is reproducible."""
    ml = _ml()
    for batch in (8, 64, 512):
        covered: set[int] = set()
        for seed in range(40 * batch):
            covered.update(ml.reference_rows(batch, 1e30, seed))
        assert covered == set(range(batch)), batch
    assert ml.reference_rows(512, 1e30, 9) == ml.reference_rows(512, 1e30, 9)
    assert len({tuple(ml.reference_rows(512, 1e30, s)) for s in range(20)}) > 1


def test_fault_only_in_an_earlier_batch_than_the_last_is_not_seen() -> None:
    """Documents the scope: only the LAST batch is recomputed; a transient fault
    in an earlier batch passes (the sampled batch is not the timed loop)."""
    calls = {"n": 0}

    def tamper(state: Any) -> None:
        def hook(_m: Any, _i: Any, out: Any) -> Any:
            calls["n"] += 1
            return out * 2.0 if calls["n"] == 1 else out

        state["model"].register_forward_hook(hook)

    check = _run("ml.cnn_infer", CNN, tamper=tamper)
    assert check.ok  # documents the limit; flip if the scope is widened


def test_real_window_extremes_sample_few_rows() -> None:
    """Quantify the sampled fraction at the window edge so the Lead can judge."""
    ml = _ml()
    cnn = sum(2.0 * 9 * a * b * r**2 for a, b, r in ml.cnn_layout(256, 12, 64))
    trf = 24 * (24.0 * 2048 * 1024 * 1024 + 4.0 * 2048 * 2048 * 1024)
    assert len(ml.reference_rows(512, cnn)) >= 2
    trf_rows = ml.reference_rows(256, trf)
    assert trf_rows[0] == 0 and trf_rows[-1] == 255
    assert len(trf_rows) == 2 + ml.SAMPLED_ROWS  # first, last + seeded (T1)
    for batch, flops in [(512, cnn), (256, trf), (2, 1e30), (1, 1e30)]:
        rows = ml.reference_rows(batch, flops)
        assert rows == sorted(set(rows)) and rows[0] == 0 and rows[-1] == batch - 1
        assert all(0 <= r < batch for r in rows)


@pytest.mark.parametrize("batch", [*list(range(1, 40)), 255, 256, 511, 512])
@pytest.mark.parametrize("budget_rows", [1, 2, 3, 7, 50])
def test_reference_rows_invariants(
    batch: int, budget_rows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    ml = _ml()
    rows = ml.reference_rows(batch, ml.REFERENCE_FLOPS / budget_rows)
    assert rows == sorted(set(rows))
    assert rows[0] == 0 and rows[-1] == batch - 1
    # T1: sampling adds SAMPLED_ROWS seeded rows on top of the evenly spaced ones.
    assert len(rows) == min(batch, max(2, budget_rows) + ml.SAMPLED_ROWS)
    assert all(0 <= r < batch for r in rows)


def test_reference_rows_degenerate_inputs() -> None:
    ml = _ml()
    assert ml.reference_rows(1, 0.0) == [0]
    assert ml.reference_rows(3, 0.0) == [0, 1, 2]
    assert ml.reference_rows(3, float("inf")) == [0, 1, 2]  # 1 is the seeded row
    assert ml.reference_rows(2, float("inf")) == [0, 1]
