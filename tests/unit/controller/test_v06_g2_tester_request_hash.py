"""V0.6 G2 tester: an Idempotency-Key retry of a V0.4/V0.5 request keeps meaning
"the same request" - the request hash formula is unchanged for them."""

from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller.api.job_models import Placement
from meshforge.controller.api.job_routes import request_hash

pytestmark = pytest.mark.unit

TREE = "sha256:" + "c" * 64
BUNDLE = "sha256:" + "d" * 64
NODE = "node_01J0000000000000000000000"


def _formula(
    manifest: dict[str, object], source: dict[str, str], placement: dict[str, str]
) -> str:
    return hashlib.sha256(
        json.dumps(
            {"manifest": manifest, **source, "placement": placement},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("mode", ["offload", "local"])
def test_v05_tree_request_hash_is_the_v05_formula(mode: str) -> None:
    manifest = {
        "name": "m",
        "entrypoint": ["python", "main.py"],
        "env": {"base": "python-cpu"},
    }
    got = request_hash(manifest, None, Placement(mode=mode, node_id=NODE), tree_id=TREE)  # type: ignore[arg-type]
    assert got == _formula(manifest, {"tree_id": TREE}, {"mode": mode, "node_id": NODE})


def test_v04_bundle_request_hash_is_the_v04_formula() -> None:
    manifest = {"name": "m"}
    got = request_hash(manifest, BUNDLE, Placement(mode="offload", node_id=NODE))
    assert got == _formula(
        manifest, {"bundle_digest": BUNDLE}, {"mode": "offload", "node_id": NODE}
    )


def test_new_v06_inputs_change_the_hash_so_a_retry_with_other_intent_conflicts() -> (
    None
):
    manifest = {"name": "m"}
    auto = Placement(mode="auto")
    base = request_hash(manifest, BUNDLE, auto)
    assert (
        request_hash(manifest, BUNDLE, Placement(mode="auto", policy="random")) != base
    )
    assert request_hash(manifest, BUNDLE, auto, submitter=(NODE, True)) != base
    assert request_hash(manifest, BUNDLE, auto, submitter=(NODE, True)) != request_hash(
        manifest, BUNDLE, auto, submitter=(NODE, False)
    )
    assert request_hash({"name": "m", "priority": 3}, BUNDLE, auto) != base
    # and an auto request never collides with a pinned one
    assert (
        request_hash(manifest, BUNDLE, Placement(mode="offload", node_id=NODE)) != base
    )
