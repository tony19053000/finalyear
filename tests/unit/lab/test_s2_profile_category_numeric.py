"""Lab L2 2026-11-01: PowerShell 5.1 serialises NetworkCategory as a number."""

import json

import pytest
from lab.spikes._s2.env import non_private_profiles, parse_connection_profiles

pytestmark = pytest.mark.unit


def _profiles(category: object) -> dict[str, object]:
    text = json.dumps(
        {"InterfaceAlias": "Wi-Fi", "Name": "JARVIS", "NetworkCategory": category}
    )
    return {"supported": True, "profiles": parse_connection_profiles(text)}


@pytest.mark.parametrize("category", [1, "1", "Private", 2, "DomainAuthenticated"])
def test_private_or_domain_is_not_reported(category: object) -> None:
    assert non_private_profiles(_profiles(category)) == []


@pytest.mark.parametrize("category", [0, "0", "Public", None, 7])
def test_public_or_unknown_is_reported(category: object) -> None:
    assert non_private_profiles(_profiles(category)) == ["Wi-Fi"]


def test_numeric_category_is_stored_by_name() -> None:
    rows = parse_connection_profiles(
        json.dumps([{"InterfaceAlias": "Wi-Fi", "NetworkCategory": 1}])
    )
    assert rows[0]["category"] == "Private"
