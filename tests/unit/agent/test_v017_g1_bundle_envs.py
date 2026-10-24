"""V0.17 G1, SP3 findings F1/F2/F3 in the environment builder.

* F1: from a bundle, venvs for the agent's own Python are built on the bundled
  interpreter - ``uv python find`` (which finds a system Python, or nothing on a
  fresh Windows laptop) is not even asked.
* F2: the interpreter path given to ``uv venv`` is the upgrade-stable ``current``
  one, and a venv whose interpreter link dangles is ``damaged``, never ``ready``.
* F3: the tool environment is an exact allow-list (pinned here), so nothing else
  from the agent's environment - a build host's variables included - reaches a
  job environment build.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from meshforge.agent.envs import _tool
from meshforge.agent.envs.builder import (
    EnvBuilder,
    EnvsConfig,
    bundled_interpreter,
)
from meshforge.agent.envs.fsutil import remove_tree
from meshforge.common.buildinfo import BUILDINFO_NAME

pytestmark = pytest.mark.unit

_OWN_MINOR = f"{sys.version_info[0]}.{sys.version_info[1]}"
_OWN_FULL = ".".join(str(p) for p in sys.version_info[:3])
_PROBE = json.dumps(["cpython", _OWN_FULL, "linux-x86_64"]).encode()


def _builder(
    tmp_path: Path, bundled: Path | None
) -> tuple[EnvBuilder, list[list[str]]]:
    builder = EnvBuilder(
        EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0),
        uv_argv=("uv",),
        bundled_python=bundled,
    )
    calls: list[list[str]] = []

    async def fake_query(argv: list[str], env: dict[str, str]) -> tuple[int, bytes]:
        calls.append(list(argv))
        if argv[1:3] == ["python", "find"]:
            return 0, b"/usr/bin/python3.12\n"
        return 0, _PROBE

    builder._query = fake_query  # type: ignore[method-assign]
    return builder, calls


@pytest.mark.parametrize("request_", [None, _OWN_MINOR, _OWN_FULL])
def test_bundle_builds_on_its_own_interpreter(
    tmp_path: Path, request_: str | None
) -> None:
    bundled = tmp_path / "opt" / "current" / "python" / "bin" / "python3.12"
    builder, calls = _builder(tmp_path, bundled)
    found = asyncio.run(builder.find_interpreter(request_))
    assert found.path == bundled
    assert not any(c[1:3] == ["python", "find"] for c in calls), calls
    assert calls[-1][0] == str(bundled)  # the probe ran the bundled interpreter


def test_other_versions_still_use_uv_find(tmp_path: Path) -> None:
    bundled = tmp_path / "python3.12"
    builder, calls = _builder(tmp_path, bundled)
    found = asyncio.run(builder.find_interpreter("3.11"))
    assert found.path == Path("/usr/bin/python3.12")
    assert calls[0][1:3] == ["python", "find"]


def test_dev_venv_behaviour_is_unchanged(tmp_path: Path) -> None:
    builder, calls = _builder(tmp_path, None)
    asyncio.run(builder.find_interpreter(None))
    assert calls[0] == ["uv", "python", "find", "--no-config", "--system", _OWN_MINOR]


def _fake_bundle(root: Path) -> Path:
    exe = root / "python" / "bin" / "python3.12"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (root / BUILDINFO_NAME).write_text("{}", encoding="utf-8")
    return exe


def test_bundled_interpreter_none_in_a_dev_venv(tmp_path: Path) -> None:
    exe = tmp_path / "venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert bundled_interpreter(str(exe)) is None


@pytest.mark.skipif(
    os.name != "posix", reason="the current symlink is the Linux layout"
)
def test_bundled_interpreter_uses_the_current_pointer(tmp_path: Path) -> None:
    exe = _fake_bundle(tmp_path / "opt" / "0.17.0")
    assert bundled_interpreter(str(exe)) == exe  # unpacked elsewhere: as is
    (tmp_path / "opt" / "current").symlink_to("0.17.0")
    assert bundled_interpreter(str(exe)) == (
        tmp_path / "opt" / "current" / "python" / "bin" / "python3.12"
    )


@pytest.mark.skipif(
    os.name != "posix", reason="a venv's bin/python is a symlink on POSIX"
)
def test_a_venv_with_a_vanished_interpreter_is_damaged(tmp_path: Path) -> None:
    interpreter = tmp_path / "old-version" / "python3.12"
    interpreter.parent.mkdir()
    interpreter.write_bytes(b"")
    builder = EnvBuilder(
        EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0),
        uv_argv=("uv",),
        bundled_python=None,
    )

    async def build(target: Path) -> None:
        (target / "bin").mkdir(parents=True)
        (target / "bin" / "python").symlink_to(interpreter)

    built = asyncio.run(builder.ensure_dir_env("venv", "h1", build, deadline=1e18))
    assert builder.env_state("venv", "h1") == "ready"
    assert built.cache == "built"
    interpreter.unlink()  # the upgrade removed the old bundle folder
    try:
        assert builder.env_state("venv", "h1") == "damaged"
    finally:
        remove_tree(tmp_path / "envs")  # the finished env is read-only


# ------------------------------------------------------------------- F3


_ALLOWED = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "PROGRAMDATA",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}


def test_the_tool_env_allow_list_is_pinned() -> None:
    """Adding a name to the allow-list is a security decision (the build runs
    third-party code): it must change this test too. ``SSL_CERT_*`` are the
    *node's* own trust settings (corporate proxies), deliberately kept."""
    assert set(_tool._PASS_THROUGH) == _ALLOWED


def test_nothing_outside_the_allow_list_reaches_a_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaky = {
        "MESHFORGE_AGENT__TOKEN": "secret",
        "MESHFORGE_INSTALL_MODE": "system",
        "UV_INDEX_URL": "http://evil",
        "UV_PYTHON": "/build/host/python",
        "PIP_INDEX_URL": "http://evil",
        "PYTHONPATH": "/build/host",
        "PYTHONHOME": "/build/host",
        "VIRTUAL_ENV": "/build/host/.venv",
        "SOURCE_DATE_EPOCH": "1",
        "REQUESTS_CA_BUNDLE": "/build/host/ca.pem",
        "CURL_CA_BUNDLE": "/build/host/ca.pem",
        "CI": "true",
    }
    for name, value in leaky.items():
        monkeypatch.setenv(name, value)
    env = _tool.tool_env({"UV_CACHE_DIR": "/x"})
    assert not set(leaky) & set(env)
    assert set(env) <= _ALLOWED | {"UV_CACHE_DIR"}
