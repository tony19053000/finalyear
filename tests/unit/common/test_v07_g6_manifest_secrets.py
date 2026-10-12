"""V0.7 G6 deliverable 3: manifest ``secrets: [NAME | {name, as_env}]``.

Names follow ``common/secret_names`` (normalised, ASCII before folding), each
once, at most 32; ``as_env`` is strict bool and may not shadow a variable the
agent sets; unknown keys refused; the canonical form omits an empty list (a
job without secrets hashes exactly as before G6); the stored-manifest helper
reads the names back.
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.manifest import (
    MAX_SECRETS,
    ManifestError,
    SecretSpec,
    parse_manifest_data,
)

pytestmark = pytest.mark.unit


def _parse(secrets: Any) -> Any:
    return parse_manifest_data({"entrypoint": "python main.py", "secrets": secrets})


def test_forms_and_normalisation() -> None:
    m = _parse(["api_key", {"name": "Db_Pass", "as_env": True}])
    assert m.secrets == (
        SecretSpec(name="API_KEY", as_env=False),
        SecretSpec(name="DB_PASS", as_env=True),
    )
    assert m.canonical_dict()["secrets"] == [
        {"name": "API_KEY", "as_env": False},
        {"name": "DB_PASS", "as_env": True},
    ]


@pytest.mark.parametrize(
    "secrets",
    [
        ["A", "a"],  # the same name twice
        ["1X"],
        [chr(0x212A) + "EY"],
        [{"name": "PATH", "as_env": True}],
        [{"name": "MESHFORGE_SECRETS_DIR", "as_env": True}],
        [{"name": "X1", "as_env": "yes"}],
        [{"name": "X1", "value": "inline"}],  # never a value in the manifest
        [{"as_env": True}],
        [7],
        "API_KEY",  # not a list
        [f"S{i}" for i in range(MAX_SECRETS + 1)],
    ],
)
def test_refused(secrets: Any) -> None:
    with pytest.raises(ManifestError):
        _parse(secrets)


def test_a_reserved_name_is_fine_as_a_file() -> None:
    assert _parse(["PATH"]).secrets[0].name == "PATH"
    assert len(_parse([f"S{i}" for i in range(MAX_SECRETS)]).secrets) == MAX_SECRETS


def test_no_secrets_keeps_the_old_canonical_form() -> None:
    without = parse_manifest_data({"entrypoint": "python main.py"})
    empty = _parse([])
    assert "secrets" not in without.canonical_dict()
    assert without.canonical_json() == empty.canonical_json()


def test_stored_manifest_names() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.secret_store import secret_names

    stored = _parse(["a_b", {"name": "c", "as_env": True}]).canonical_dict()
    assert secret_names(stored) == ["A_B", "C"]
    assert secret_names({}) == [] and secret_names({"secrets": "x"}) == []
