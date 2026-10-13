"""V0.7 G6 fix round 1 (D162, T-9): ``as_env`` refuses every ``LD_*`` name.

OPERATIONS 0a1b says a secret may not become ``PATH``, ``HOME``,
``PYTHONPATH``, ``LD_*`` or ``MESHFORGE_*``; the code refused only two ``LD_``
names. The rule holds wherever a manifest is read (submit and the agent): it
is part of :class:`~meshforge.common.manifest.SecretSpec`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meshforge.common.manifest import Manifest
from meshforge.common.secret_names import SecretNameError, check_env_name

pytestmark = pytest.mark.unit

LD_NAMES = [
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_BIND_NOW",
    "LD_ORIGIN_PATH",
    "LD_PROFILE",
    "LD_SHOW_AUXV",
    "LD_USE_LOAD_BIAS",
    "LD_",
    "LD_ANYTHING_NEW_IN_GLIBC",
]


@pytest.mark.parametrize("name", LD_NAMES)
def test_every_ld_name_is_refused_as_an_environment_variable(name: str) -> None:
    with pytest.raises(SecretNameError) as exc:
        check_env_name(name)
    assert "reserved" in str(exc.value)
    for spelling in (name, name.lower()):
        with pytest.raises(ValidationError):
            Manifest.model_validate(
                {
                    "name": "j",
                    "entrypoint": ["python", "main.py"],
                    "secrets": [{"name": spelling, "as_env": True}],
                }
            )
    # As a file (the default) the same name is fine: no process reads it.
    manifest = Manifest.model_validate(
        {"name": "j", "entrypoint": ["python", "main.py"], "secrets": [name]}
    )
    assert [item.name for item in manifest.secrets] == [name]


@pytest.mark.parametrize(
    "name", ["LDFLAGS", "LD", "OLD_KEY", "MY_LD_PRELOAD", "API_KEY"]
)
def test_names_that_only_look_similar_are_still_allowed(name: str) -> None:
    assert check_env_name(name) == name
    manifest = Manifest.model_validate(
        {
            "name": "j",
            "entrypoint": ["python", "main.py"],
            "secrets": [{"name": name, "as_env": True}],
        }
    )
    assert manifest.secrets[0].as_env is True


@pytest.mark.parametrize(
    "name", ["PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "MESHFORGE_X"]
)
def test_the_other_reserved_names_are_unchanged(name: str) -> None:
    with pytest.raises(SecretNameError):
        check_env_name(name)
