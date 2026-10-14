"""V0.5 G5 D92: an attempt's fetch token covers its project tree and every named
input tree - nothing else."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.controller.jobs.dispatcher import fetch_scope

pytestmark = [pytest.mark.unit, pytest.mark.security]

A, B, C = ("sha256:" + c * 64 for c in "abc")


def _job(tree: str | None, bundle: str | None = None) -> Any:
    return SimpleNamespace(id="job_x", tree_id=tree, bundle_digest=bundle)


def test_scope_is_the_tree_plus_its_inputs_deduplicated() -> None:
    assert [str(d) for d in fetch_scope(_job(A), [B, C, B, A])] == [A, B, C]
    assert [str(d) for d in fetch_scope(_job(A))] == [A]


def test_a_bundle_job_never_gets_input_trees() -> None:
    assert [str(d) for d in fetch_scope(_job(None, B), [C])] == [B]
