"""V0.6 G2: the manifest's scheduling keys (D106 priority, D107 policy, D110 retry).

Every bound is tested at max accepted and max+1 refused (checklist 9), and the
canonical form of a manifest that does not use the keys is byte-identical to
V0.5's - a V0.5 job keeps its idempotency request hash.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest

from meshforge.common.manifest import (
    MAX_ATTEMPTS,
    MAX_BACKOFF_S,
    MAX_PRIORITY,
    MIN_PRIORITY,
    POLICIES,
    ManifestError,
    PolicyName,
    RetrySpec,
    parse_manifest_data,
)

pytestmark = pytest.mark.unit


def _m(**extra: Any) -> dict[str, Any]:
    return {"name": "j", "entrypoint": ["python", "main.py"], **extra}


def test_policy_names_match_the_literal_type() -> None:
    assert tuple(typing.get_args(PolicyName.__value__)) == POLICIES
    assert len(set(POLICIES)) == 7


@pytest.mark.parametrize("value", [MIN_PRIORITY, 0, MAX_PRIORITY])
def test_priority_bounds_accepted(value: int) -> None:
    assert parse_manifest_data(_m(priority=value)).priority == value


@pytest.mark.parametrize(
    "value", [MIN_PRIORITY - 1, MAX_PRIORITY + 1, True, 1.5, "3", None]
)
def test_priority_out_of_bounds_or_wrong_type_refused(value: object) -> None:
    with pytest.raises(ManifestError) as caught:
        parse_manifest_data(_m(priority=value))
    assert caught.value.problems[0][0] == "priority"


@pytest.mark.parametrize("name", POLICIES)
def test_every_policy_name_is_accepted(name: str) -> None:
    assert parse_manifest_data(_m(policy=name)).policy == name


@pytest.mark.parametrize("name", ["", "fastest", "Random", "oracle", 3])
def test_unknown_policy_refused(name: object) -> None:
    with pytest.raises(ManifestError) as caught:
        parse_manifest_data(_m(policy=name))
    assert caught.value.problems[0][0] == "policy"


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        ("max_attempts", [1, MAX_ATTEMPTS]),
        ("backoff_s", [0, MAX_BACKOFF_S]),
    ],
)
def test_retry_bounds_accepted(key: str, ok: list[int]) -> None:
    for value in ok:
        spec = parse_manifest_data(_m(retry={key: value})).retry
        assert getattr(spec, key) == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_attempts", 0),
        ("max_attempts", MAX_ATTEMPTS + 1),
        ("max_attempts", True),
        ("max_attempts", 2.0),
        ("backoff_s", -1),
        ("backoff_s", MAX_BACKOFF_S + 1),
        ("on_lost", "no"),
        ("on_lost", 0),
        ("extra", 1),
    ],
)
def test_retry_out_of_bounds_refused(key: str, value: object) -> None:
    with pytest.raises(ManifestError) as caught:
        parse_manifest_data(_m(retry={key: value}))
    assert caught.value.problems[0][0] == f"retry.{key}"


def test_retry_must_be_a_mapping() -> None:
    with pytest.raises(ManifestError):
        parse_manifest_data(_m(retry=3))


def test_defaults_are_left_out_of_the_canonical_form() -> None:
    plain = parse_manifest_data(_m())
    explicit = parse_manifest_data(
        _m(priority=0, retry={"max_attempts": 3, "backoff_s": 5, "on_lost": True})
    )
    assert plain.canonical_json() == explicit.canonical_json()
    # Exactly the V0.5 key set: a pre-V0.6 request keeps its hash.
    assert set(plain.canonical_dict()) == {
        "name",
        "entrypoint",
        "env",
        "resources",
        "outputs",
        "limits",
    }
    assert plain.retry == RetrySpec() and plain.priority == 0 and plain.policy is None


def test_non_defaults_are_in_the_canonical_form_and_round_trip() -> None:
    manifest = parse_manifest_data(
        _m(priority=-4, policy="least_loaded", retry={"on_lost": False})
    )
    data = manifest.canonical_dict()
    assert data["priority"] == -4
    assert data["policy"] == "least_loaded"
    assert data["retry"] == {"max_attempts": 3, "backoff_s": 5, "on_lost": False}
    again = parse_manifest_data(data)
    assert again == manifest
    assert again.canonical_json() == manifest.canonical_json()
