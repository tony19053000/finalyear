"""V0.9 G4 lane A items 0.1 + 0.2 (D233.1/D233.2).

0.1: the ``ml.transformer_infer`` float64 reference is the embedding plus one
seeded layer applied ``depth`` times by hand, one row at a time - it must give
the same numbers as the whole seeded model run in float64 by torch, and still
catch a timed model whose weights are not the seeded ones (any layer).
(Peak memory at the largest shape: the component test
``tests/component/bench/test_v09_g4a_transformer_reference_memory.py``.)

0.2: the job-side runner refuses the ``ml.cnn_train`` corners the library
refuses (``batch * width * image**2 <= 2**24``).
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.bench.workloads import ParamsError, get_family
from meshforge.bench.workloads.kernels import runner
from meshforge.bench.workloads.kernels._support import KernelParamsError

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- 0.2 job side


def test_the_job_side_cap_is_the_library_cap() -> None:
    from meshforge.bench.workloads.kernels.ml import TRAIN_REFERENCE_ELEMENTS

    assert runner.CNN_TRAIN_FIRST_BATCH_MAX == TRAIN_REFERENCE_ELEMENTS == 2**24


CORNERS = [
    # (batch, width, image): over the cap by the smallest step of each field
    (512, 256, 64),
    (65, 256, 32),
    (66, 254, 32),
    (17, 256, 64),
    (512, 33, 32),
]


@pytest.mark.parametrize(("batch", "width", "image"), CORNERS)
def test_the_job_refuses_a_corner_the_library_refuses(
    batch: int, width: int, image: int
) -> None:
    params = {"batch": batch, "width": width, "depth": 2, "image": image, "steps": 20}
    with pytest.raises(ParamsError, match="first-loss check"):
        get_family("ml.cnn_train").parse_params(params)
    with pytest.raises(KernelParamsError, match="first-loss check"):
        runner.check_params("ml.cnn_train", params)


@pytest.mark.parametrize(
    ("batch", "width", "image"), [(64, 256, 32), (16, 256, 64), (512, 32, 32)]
)
def test_the_job_accepts_exactly_the_cap(batch: int, width: int, image: int) -> None:
    assert batch * width * image**2 == 2**24
    runner.check_params(
        "ml.cnn_train",
        {"batch": batch, "width": width, "depth": 2, "image": image, "steps": 20},
    )


def test_the_job_refuses_the_corner_as_bad_params_exit_2(tmp_path: Any) -> None:
    """A hand-edited job folder (the library never writes this corner): exit 2
    and a ``bad_params`` result, before anything is built or timed."""
    import json

    folder = tmp_path / "job"
    folder.mkdir()
    (folder / runner.PARAMS_FILE).write_text(
        json.dumps(
            {
                "family": "ml.cnn_train",
                "seed": 1,
                "params": {
                    "batch": 512,
                    "width": 256,
                    "depth": 12,
                    "image": 64,
                    "steps": 20,
                },
            }
        ),
        encoding="utf-8",
    )
    assert runner.main(folder) == runner.EXIT_BAD_PARAMS
    result = json.loads((folder / runner.RESULT_FILE).read_text(encoding="utf-8"))
    assert result["status"] == "bad_params"
    assert result["compute_ns"] is None


def test_other_families_have_no_combination_rule() -> None:
    runner.check_params("cpu.sort", {"n": 50_000_000, "repeats": 500})


# ------------------------------------------------------- 0.1 transformer ref

torch = pytest.importorskip("torch")
from meshforge.bench.workloads.kernels import ml  # noqa: E402

SHAPES = [
    {"batch": 2, "seq_len": 16, "width": 64, "depth": 1, "batches": 3},
    {"batch": 3, "seq_len": 40, "width": 128, "depth": 3, "batches": 3},
    {"batch": 1, "seq_len": 33, "width": 192, "depth": 2, "batches": 3},
]


def _state(params: dict[str, int], seed: int = 7) -> Any:
    return ml._transformer_prepare(params, seed, "cpu")


@pytest.mark.parametrize(
    "params", SHAPES, ids=lambda p: "w{width}-d{depth}".format(**p)
)
def test_the_one_layer_reference_equals_the_whole_seeded_model_in_float64(
    params: dict[str, int],
) -> None:
    state = _state(params)
    reference = ml._transformer_reference(state, state["model"])
    assert callable(reference), reference
    tokens = state["xs"][0]
    with torch.no_grad():
        expected = ml._seeded_model(state).double().eval()(tokens)
    got = reference(tokens)
    assert got.dtype == torch.float64 and got.shape == expected.shape
    assert float((got - expected).abs().max()) < 1e-12


def test_the_seeded_layers_really_start_equal() -> None:
    """The premise of the one-layer rebuild: TransformerEncoder deep-copies its
    layer (no random draw), so every layer equals layer 0 bit for bit."""
    state = _state(SHAPES[1])
    layers = state["model"][1].layers
    first = layers[0].state_dict()
    for layer in layers[1:]:
        for name, value in layer.state_dict().items():
            assert torch.equal(value, first[name]), name


@pytest.mark.parametrize("layer_index", [0, 2])
def test_a_changed_weight_in_any_layer_is_caught_and_named(layer_index: int) -> None:
    state = _state(SHAPES[1])
    with torch.no_grad():
        state["model"][1].layers[layer_index].linear2.bias[0] += 1e-3
    found = ml._transformer_reference(state, state["model"])
    assert found == f"1.layers.{layer_index}.linear2.bias"
    out = ml._infer_compute(state)
    check = ml._transformer_check(state, out)
    assert not check.ok and "differs from the seeded build" in check.detail


def test_a_changed_embedding_is_caught() -> None:
    state = _state(SHAPES[0])
    with torch.no_grad():
        state["model"][0].weight[3, 1] += 1e-3
    assert ml._transformer_reference(state, state["model"]) == "0.weight"


def test_a_model_with_another_depth_is_refused_by_name() -> None:
    state = _state(SHAPES[1])
    state["depth"] = 2  # the timed model has 3 layers
    assert ml._transformer_reference(state, state["model"]) == "the parameter names"


def test_an_honest_run_passes_and_a_scaled_output_fails() -> None:
    state = _state(SHAPES[1])
    out = ml._infer_compute(state)
    honest = ml._transformer_check(state, out)
    assert honest.ok, honest.detail
    assert "float64 reference" in honest.detail
    wrong = ml._transformer_check(state, out * 1.0001)
    assert not wrong.ok


def test_chunks_are_bounded_and_never_empty() -> None:
    assert ml._chunk_rows(16 * 2048 * 8, 2048) * 16 * 2048 * 8 <= (
        ml.TRANSFORMER_CHUNK_BYTES
    )
    assert ml._chunk_rows(ml.TRANSFORMER_CHUNK_BYTES * 4, 10) == 1
    assert ml._chunk_rows(8, 10) == 10


def test_tiny_chunks_give_the_same_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunking is exact: one query row / one feed-forward row per chunk gives
    the same result as the unchunked layer."""
    state = _state(SHAPES[1])
    tokens = state["xs"][0][:1]
    whole = ml._transformer_reference(state, state["model"])(tokens)
    monkeypatch.setattr(ml, "TRANSFORMER_CHUNK_BYTES", 1)
    chunked = ml._transformer_reference(state, state["model"])(tokens)
    assert float((whole - chunked).abs().max()) < 1e-12
