"""D157 (cosmetic): a damaged API token in ``compute --token-file`` is described
as an API token (``compute login``), not as a fabric token (``token show``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.common.api_token import new_api_token
from meshforge.common.fabric_token import (
    FabricTokenError,
    parse_token,
    read_token_file,
)

pytestmark = pytest.mark.unit


def _damaged() -> list[str]:
    good = new_api_token().get_secret_value()
    return [good[:20], good[:-1] + "!", good + "x" * 200, "mfat_"]


@pytest.mark.parametrize("value", _damaged())
def test_a_damaged_api_token_gets_the_api_token_wording(
    tmp_path: Path, value: str
) -> None:
    path = tmp_path / "token"
    path.write_text(value + "\n")
    with pytest.raises(FabricTokenError) as caught:
        read_token_file(path)
    message = caught.value.message
    assert "API token" in message and "compute login" in message
    assert "token show" not in message
    assert value.removeprefix("mfat_") not in message or value == "mfat_"
    assert str(path) in message


def test_a_damaged_fabric_token_keeps_the_fabric_wording() -> None:
    with pytest.raises(FabricTokenError) as caught:
        parse_token("short")
    assert "fabric token" in caught.value.message
    assert "token show" in caught.value.message


def test_a_valid_api_token_and_a_valid_fabric_token_still_parse() -> None:
    token = new_api_token().get_secret_value()
    assert parse_token(token).get_secret_value() == token
    assert parse_token("A" * 43).get_secret_value() == "A" * 43
