"""V0.9 G2 follow-ups (D225 items 1 and 3).

1. The ML inference checks used to compare only a rerun with the output, so a
   consistently wrong kernel was ``verified``. They now also rebuild the model
   from the seed on the CPU and recompute in float64; ``cnn_train`` checks its
   first loss the same way. Here: honest runs pass, and a timed model that is
   not the seeded one, or that computes something consistently different
   (a forward hook: weights equal, rerun equal), fails.
3. ``compute_ns`` covers the whole loop; ``compute_iterations`` says how many
   iterations, so one iteration is ``compute_ns / compute_iterations``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.bench.workloads import get_family, list_families
from meshforge.bench.workloads.kernels import runner
from meshforge.bench.workloads.kernels._support import Check

pytestmark = pytest.mark.unit

INFER: dict[str, dict[str, Any]] = {
    "ml.cnn_infer": {"batch": 3, "width": 8, "depth": 3, "image": 32, "batches": 3},
    "ml.transformer_infer": {
        "batch": 3,
        "seq_len": 8,
        "width": 64,
        "depth": 2,
        "batches": 3,
    },
}
TRAIN: dict[str, Any] = {"batch": 16, "width": 8, "depth": 2, "image": 32, "steps": 40}


def _ml() -> Any:
    pytest.importorskip("torch", reason="the ML kernels need torch (gpu extra)")
    from meshforge.bench.workloads.kernels import ml

    return ml


def _run(
    family: str,
    params: Mapping[str, Any],
    *,
    seed: int = 5,
    tamper: Callable[[Any], None] | None = None,
) -> Check:
    """prepare -> warmup -> (tamper the timed state) -> compute -> check."""
    kernel = _ml().KERNELS[family]
    state = kernel.prepare(params, seed, "cpu")
    if kernel.warmup is not None:
        kernel.warmup(state)
    if tamper is not None:
        tamper(state)
    check: Check = kernel.check(state, kernel.compute(state))
    return check


def _first_weight(state: Any) -> Any:
    return next(p for p in state["model"].parameters() if p.dim() > 1)


def _nudge_one_weight(state: Any) -> None:
    torch = state["torch"]
    with torch.no_grad():
        _first_weight(state).view(-1)[0] += 1e-3


def _scale_output(factor: float) -> Callable[[Any], None]:
    """A consistently wrong model: same weights, the rerun agrees, the output
    is off by ``factor`` (what a bad device kernel would look like)."""

    def tamper(state: Any) -> None:
        state["model"].register_forward_hook(lambda _m, _i, out: out * factor)

    return tamper


# --------------------------------------------------------- item 1: inference


@pytest.mark.parametrize("family", sorted(INFER))
@pytest.mark.parametrize("seed", [0, 1, 7])
def test_honest_inference_passes_the_float64_reference(family: str, seed: int) -> None:
    check = _run(family, INFER[family], seed=seed)
    assert check.ok, check.detail
    assert "float64 reference" in check.detail
    assert "on 3 row(s)" in check.detail  # tiny batch: the whole last batch


@pytest.mark.parametrize("family", sorted(INFER))
def test_one_weight_changed_in_the_timed_model_fails(family: str) -> None:
    check = _run(family, INFER[family], tamper=_nudge_one_weight)
    assert not check.ok
    assert "differs from the seeded build" in check.detail


@pytest.mark.parametrize("family", sorted(INFER))
@pytest.mark.parametrize("factor", [1.0001, 0.5, -1.0])
def test_consistently_wrong_output_fails(family: str, factor: float) -> None:
    check = _run(family, INFER[family], tamper=_scale_output(factor))
    assert not check.ok, check.detail
    # The rerun agrees (the hook runs there too): only the reference catches it.
    assert "rerun max diff 0.000e+00" in check.detail


@pytest.mark.parametrize("family", sorted(INFER))
def test_wrong_buffer_in_the_timed_model_fails(family: str) -> None:
    def tamper(state: Any) -> None:
        buffers = list(state["model"].buffers())
        if not buffers:  # the transformer has none: change a bias instead
            _nudge_one_weight(state)
            return
        buffers[0].add_(0.25)  # batch-norm running mean

    check = _run(family, INFER[family], tamper=tamper)
    assert not check.ok
    assert "differs from the seeded build" in check.detail


def test_reference_check_leaves_the_rng_state_alone() -> None:
    ml = _ml()
    import torch

    kernel = ml.KERNELS["ml.cnn_infer"]
    state = kernel.prepare(INFER["ml.cnn_infer"], 3, "cpu")
    out = kernel.compute(state)
    torch.manual_seed(1234)
    before = torch.random.get_rng_state()
    assert kernel.check(state, out).ok
    assert torch.equal(before, torch.random.get_rng_state())


# ----------------------------------------------------------- item 1: training


@pytest.mark.parametrize("seed", [0, 3, 9])
def test_honest_training_first_loss_matches_float64(seed: int) -> None:
    check = _run("ml.cnn_train", TRAIN, seed=seed)
    assert check.ok, check.detail
    assert "first loss vs float64 reference" in check.detail


def test_training_with_a_changed_weight_fails() -> None:
    # A head weight: the loss is one scalar, and train-mode batch-norm absorbs
    # much of a tiny change to a conv weight (a known limit of a loss check).
    def tamper(state: Any) -> None:
        torch = state["torch"]
        with torch.no_grad():
            list(state["model"].parameters())[-2].view(-1)[0] += 1e-3

    check = _run("ml.cnn_train", TRAIN, tamper=tamper)
    assert not check.ok, check.detail
    assert "first loss vs float64 reference" in check.detail


def test_training_with_a_consistently_wrong_model_fails() -> None:
    # Loss still goes down (the hook scales every logit), so only the first-loss
    # reference can catch it.
    check = _run("ml.cnn_train", TRAIN, tamper=_scale_output(1.01))
    assert not check.ok, check.detail


# ------------------------------------------------------ item 1: the helpers


def test_reference_rows_whole_batch_when_cheap_else_spread() -> None:
    ml = _ml()
    assert ml.reference_rows(5, 1.0) == [0, 1, 2, 3, 4]
    assert ml.reference_rows(1, 1e15) == [0]
    assert ml.reference_rows(512, 1e15) == [0, 511]  # at least first and last
    rows = ml.reference_rows(512, ml.REFERENCE_FLOPS / 5)
    assert rows[0] == 0 and rows[-1] == 511 and len(rows) == 5
    assert rows == sorted(set(rows))


def test_reference_cost_stays_within_the_budget_for_the_largest_models() -> None:
    ml = _ml()
    # Largest transformer row: about 1.6e12 FLOPs; only first + last are redone.
    per_row = 24 * (24.0 * 2048 * 1024 * 1024 + 4.0 * 2048 * 2048 * 1024)
    assert ml.reference_rows(256, per_row) == [0, 255]
    # Default CNN: the whole last batch fits the budget.
    cnn = sum(2.0 * 9 * a * b * r**2 for a, b, r in ml.cnn_layout(64, 6, 32))
    assert len(ml.reference_rows(64, cnn)) == 64


def _fake_torch(precision: str, matmul_tf32: bool, cudnn_tf32: bool) -> Any:
    return SimpleNamespace(
        get_float32_matmul_precision=lambda: precision,
        backends=SimpleNamespace(
            cuda=SimpleNamespace(matmul=SimpleNamespace(allow_tf32=matmul_tf32)),
            cudnn=SimpleNamespace(allow_tf32=cudnn_tf32),
        ),
    )


@pytest.mark.parametrize(
    ("device", "torch_flags", "conv", "eps"),
    [
        ("cpu", ("medium", True, True), True, 2.0**-24),
        ("cuda", ("highest", False, False), True, 2.0**-24),
        ("cuda", ("highest", False, True), True, 2.0**-11),  # cuDNN default
        ("cuda", ("highest", False, True), False, 2.0**-24),  # matmul only
        ("cuda", ("highest", True, False), False, 2.0**-11),
        ("cuda", ("high", False, False), False, 2.0**-11),
        ("cuda", ("medium", False, False), False, 2.0**-8),
    ],
)
def test_float32_eps_follows_the_tf32_flags(
    device: str, torch_flags: tuple[str, bool, bool], conv: bool, eps: float
) -> None:
    ml = _ml()
    assert ml.float32_eps(_fake_torch(*torch_flags), device, conv=conv) == eps


# ---------------------------------------------------- item 3: iterations


def test_every_family_names_its_iteration_parameter() -> None:
    assert set(runner.ITERATION_PARAM) == set(runner.KERNEL_MODULES)
    for family in list_families():
        name = runner.ITERATION_PARAM[family.name]
        if name is not None:
            assert name in family.params_model.model_fields, family.name
            assert name in runner.PARAM_LIMITS[family.name], family.name


def test_compute_iterations_from_params() -> None:
    assert runner.compute_iterations("cpu.sort", {"n": 10, "repeats": 7}) == 7
    assert runner.compute_iterations("ml.cnn_train", {"steps": 40}) == 40
    assert runner.compute_iterations("ml.cnn_infer", {"batches": 3}) == 3
    assert runner.compute_iterations("cpu.sha256", {"size_mb": 2}) == 1
    assert runner.compute_iterations("cpu.sort", {}) is None
    assert runner.compute_iterations("cpu.sort", {"repeats": True}) is None
    assert runner.compute_iterations("nope", {"repeats": 2}) is None


def test_workload_json_records_the_iterations_of_compute_ns() -> None:
    document = runner.execute("cpu.zlib", {"size_mb": 1, "level": 1, "repeats": 3}, 1)
    assert document["compute_iterations"] == 3
    assert document["compute_ns"] > 0
    family = get_family("cpu.zlib")
    result = family.run(family.smallest_params(), 2)
    assert result.compute_iterations == result.params["repeats"]
    assert family.verify(result).ok


def test_not_run_documents_have_no_iterations(tmp_path: Any) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text("[]", encoding="utf-8")
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS
    import json

    doc = json.loads((tmp_path / runner.RESULT_FILE).read_text(encoding="utf-8"))
    assert doc["compute_iterations"] is None and doc["compute_ns"] is None
