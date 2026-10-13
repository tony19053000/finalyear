"""V0.7 G6 fix round 1 (D162, T-5): a job's own requirements file cannot switch
wheels-only off.

``normalize_requirements(..., wheels_only=True)`` (what every env build on a
node with ``[agent.envs] only_binary = true`` uses - uv venv, uv ``--target``,
the Docker ``pip`` build and ``compute node prepare``):

* every option line is refused, typed ``requirements_rejected``, naming the
  line number and the line;
* after a requirement only ``--hash`` is accepted;
* a direct reference must be the http(s) URL of a ``.whl``;
* what is accepted normalises exactly as without the flag (no hash changes);
* with ``wheels_only=False`` (the owner's escape hatch) nothing new is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements

pytestmark = pytest.mark.unit

HASH = "sha256:" + "ab" * 32

REFUSED = [
    "--no-binary mfsrc",
    "--no-binary=mfsrc",
    "--no-binary :all:",
    "--only-binary :none:",
    "--only-binary=:none:",
    "--prefer-binary",
    "--pre",
    "--index-url https://example.org/simple",
    "--extra-index-url https://example.org/simple",
    "-i https://example.org/simple",
    "-f https://example.org/wheels",
    "--find-links https://example.org/wheels",
    "--trusted-host example.org",
    "--use-feature fast-deps",
    "--config-settings=x=y",
    "-C x=y",
    "--global-option=build_ext",
    "--no-build-isolation",
    "--no-deps",
    "--hash=" + HASH,  # a line of its own is an option line too
    "-",
    "--",
]
REFUSED_OPTIONS = [
    "pkg==1.0 --config-settings=x=y",
    "pkg==1.0 --config-settings x=y",
    "pkg==1.0 -C x=y",
    "pkg==1.0 --global-option=build_ext",
    "pkg==1.0 --no-binary pkg",
    f"pkg==1.0 --hash={HASH} --no-binary=pkg",
    "pkg==1.0 --hash",
    f"pkg==1.0 --hash --no-binary=pkg {HASH}",
]
REFUSED_REFERENCES = [
    "pkg @ https://example.org/pkg-1.0.tar.gz",
    "pkg@https://example.org/pkg-1.0.tar.gz",
    "https://example.org/pkg-1.0.tar.gz",
    "https://example.org/pkg-1.0.zip",
    "pkg @ git+https://example.org/repo.git",
    "git+https://example.org/repo.git#egg=pkg",
    "pkg @ git+https://example.org/repo.git@v1#subdirectory=x.whl",
    "https://example.org/download?file=pkg-1.0-py3-none-any.whl",
    "pkg @ https://example.org/pkg.whl.tar.gz",
    "pkg @ https://[bad/pkg-1.0-py3-none-any.whl",
]
ACCEPTED = [
    "pkg==1.0",
    "Pkg [b,a] >= 1.0 , < 2 ; python_version >= '3.9'",
    f"pkg==1.0 --hash={HASH}",
    f"pkg==1.0 --hash {HASH} --hash=sha256:{'cd' * 32}",
    "pkg @ https://example.org/dist/pkg-1.0-py3-none-any.whl",
    "pkg@https://example.org/dist/pkg-1.0-py3-none-any.whl",
    "https://user@example.org/dist/pkg-1.0-py3-none-any.WHL?x=1#sha256=" + "ab" * 32,
    f"pkg @ https://example.org/pkg-1.0-py3-none-any.whl --hash={HASH}",
    "pre-release-pkg==1.0",
]


@pytest.mark.parametrize("line", REFUSED + REFUSED_OPTIONS + REFUSED_REFERENCES)
def test_refused_on_a_wheels_only_node_naming_the_line(line: str) -> None:
    text = f"good==1.0\n# comment\n{line}\nother==2.0\n".encode()
    with pytest.raises(EnvError) as exc:
        normalize_requirements(text, wheels_only=True)
    assert exc.value.reason == "requirements_rejected"
    message = str(exc.value)
    assert "requirements line 3" in message
    assert "only_binary" in message
    if len(line) > 2:
        assert line.split()[0].split("=")[0] in message or line[:20] in message


@pytest.mark.parametrize("line", REFUSED)
def test_an_option_line_is_refused_wherever_it_stands(line: str) -> None:
    for text in (f"{line}\npkg==1.0\n", f"pkg==1.0\n{line}\n", f"  {line}  \n"):
        with pytest.raises(EnvError):
            normalize_requirements(text.encode(), wheels_only=True)
    if line.startswith("--hash="):
        return  # joined to a requirement it is the one allowed option
    # A continuation cannot hide it either.
    with pytest.raises(EnvError):
        normalize_requirements(f"pkg==1.0 \\\n {line}\n".encode(), wheels_only=True)


@pytest.mark.parametrize("line", ACCEPTED)
def test_accepted_lines_normalise_exactly_as_before(line: str) -> None:
    text = f"{line}\n".encode()
    assert normalize_requirements(text, wheels_only=True) == normalize_requirements(
        text
    )


@pytest.mark.parametrize(
    "line",
    [
        line
        for line in REFUSED + REFUSED_OPTIONS + REFUSED_REFERENCES
        if not line.startswith(("-f", "--find", "-i", "--index", "--extra"))
        and "[bad" not in line
    ],
)
def test_the_owners_escape_hatch_refuses_nothing_new(line: str) -> None:
    assert normalize_requirements(f"{line}\n".encode(), wheels_only=False)


def test_the_builder_applies_its_only_binary_setting(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "req.txt").write_text("--no-binary mfsrc\nmfsrc==1.0\n")
    strict = EnvBuilder(EnvsConfig(root=tmp_path / "a", only_binary=True))
    with pytest.raises(EnvError) as exc:
        strict.read_requirements(project, "req.txt")
    assert exc.value.reason == "requirements_rejected"
    assert "'--no-binary mfsrc'" in str(exc.value)
    with pytest.raises(EnvError):
        strict._normalised(b"--no-binary mfsrc\nmfsrc==1.0\n")
    hatch = EnvBuilder(EnvsConfig(root=tmp_path / "b", only_binary=False))
    assert hatch.read_requirements(project, "req.txt") == (
        b"--no-binary mfsrc\nmfsrc==1.0\n"
    )
