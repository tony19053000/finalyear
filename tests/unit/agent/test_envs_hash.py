"""V0.5 G2 (D85): the env hash and requirements normalisation.

Golden values are pinned: the hash is a cache key shared by every node, so a
change to the canonical form must be a deliberate ``ENV_HASH_VERSION`` bump, never
an accident. Nothing here reads the machine (no OS, interpreter or path input).
"""

from __future__ import annotations

import hashlib
import pickle

import pytest

from meshforge.agent.envs import EnvError
from meshforge.agent.envs.hash import (
    ENV_HASH_VERSION,
    EnvInputs,
    canonical_bytes,
    env_hash,
    normalize_requirements,
)
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

REQ = b"# deps\nrequests==2.32.3\nnumpy==2.1.0  # pinned\n\n"
NORMALISED = b"numpy==2.1.0\nrequests==2.32.3\n"
GOLDEN_VENV = "b4d485ceefd111b81e7314c5f0fb384db407a119f43df4d3a356363552d98d33"
GOLDEN_DOCKER = "749a3a9bd78a8f1032048503e3299fc506f26aaa823ef221e50342e1a7f65029"
GOLDEN_EMPTY_SUBPROCESS = (
    "81f73ddbe78f477b534f755aca39b3d6123a9df76f46e18c51c992bbd8676a79"
)


def _venv(requirements: bytes = NORMALISED, **over: str) -> EnvInputs:
    fields = {
        "runtime_kind": "venv",
        "base": "cpython-3.12.3",
        "python": "3.12",
        "platform": "linux-x86_64",
    }
    fields.update(over)
    return EnvInputs(requirements=requirements, **fields)


# ------------------------------------------------------------------ golden
def test_normalised_golden() -> None:
    assert normalize_requirements(REQ) == NORMALISED


def test_canonical_bytes_golden() -> None:
    expected = (
        b'{"base":"cpython-3.12.3","platform":"linux-x86_64","python":"3.12",'
        b'"requirements_sha256":"'
        + hashlib.sha256(NORMALISED).hexdigest().encode()
        + b'","runtime_kind":"venv","v":1}'
    )
    assert canonical_bytes(_venv()) == expected
    assert hashlib.sha256(expected).hexdigest() == GOLDEN_VENV
    assert ENV_HASH_VERSION == 1


def test_env_hash_golden_values() -> None:
    assert env_hash(_venv()) == GOLDEN_VENV
    docker = EnvInputs(
        runtime_kind="docker",
        base="sha256:" + "ab" * 32,
        python="",
        requirements=NORMALISED,
        platform="linux/amd64",
    )
    assert env_hash(docker) == GOLDEN_DOCKER
    empty = EnvInputs(
        runtime_kind="subprocess",
        base="cpython-3.12.3",
        python="",
        requirements=b"",
        platform="win-amd64",
    )
    assert env_hash(empty) == GOLDEN_EMPTY_SUBPROCESS


# ------------------------------------------------------- same input, same hash
@pytest.mark.parametrize(
    "variant",
    [
        b"numpy==2.1.0\nrequests==2.32.3\n",  # already canonical
        b"requests==2.32.3\r\nnumpy==2.1.0\r\n",  # Windows newlines, other order
        b"\xef\xbb\xbfnumpy==2.1.0\nrequests==2.32.3",  # BOM, no final newline
        b"  numpy==2.1.0   \n\n\n# c\n\trequests==2.32.3 # why\n",  # spaces, comments
        b"numpy==2.1.0\nrequests==2.32.3\nnumpy==2.1.0\n",  # duplicate
        b"num\\\npy==2.1.0\nrequests==2.32.3\n",  # continuation line
        b"numpy==2.1.0\rrequests==2.32.3\r",  # old-Mac newlines
    ],
)
def test_comment_order_and_whitespace_changes_keep_the_hash(variant: bytes) -> None:
    assert normalize_requirements(variant) == NORMALISED
    assert env_hash(_venv(normalize_requirements(variant))) == GOLDEN_VENV


def test_url_fragment_hash_sign_is_not_a_comment() -> None:
    # pip: '#' starts a comment only at line start or after whitespace
    line = b"pkg @ https://example.org/pkg-1.0.tar.gz#sha256=abc\n"
    assert normalize_requirements(line) == line


# ------------------------------------------------------- any change, new hash
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_kind", "docker"),
        ("base", "cpython-3.12.4"),
        ("python", "3.11"),
        ("python", ""),
        ("platform", "win-amd64"),
    ],
)
def test_each_input_changes_the_hash(field: str, value: str) -> None:
    assert env_hash(_venv(NORMALISED, **{field: value})) != GOLDEN_VENV


def test_a_version_change_changes_the_hash() -> None:
    bumped = normalize_requirements(b"requests==2.32.4\nnumpy==2.1.0\n")
    assert bumped != NORMALISED
    assert env_hash(_venv(bumped)) != GOLDEN_VENV


def test_adding_a_package_changes_the_hash() -> None:
    more = normalize_requirements(REQ + b"tqdm\n")
    assert env_hash(_venv(more)) != GOLDEN_VENV


def test_empty_requirements_are_empty_bytes() -> None:
    assert normalize_requirements(b"") == b""
    assert normalize_requirements(b"# only a comment\n\n   \n") == b""


# ------------------------------------------------------------- rejections
@pytest.mark.parametrize(
    "line",
    [
        b"-r other.txt",
        b"--requirement=other.txt",
        b"--requirement other.txt",
        b"-rother.txt",
        b"-c constraints.txt",
        b"--constraint c.txt",
        b"-e .",
        b"--editable ./pkg",
        b"./pkg",
        b"../pkg",
        b"/abs/pkg",
        b"~/pkg",
        b"C:\\pkg",
        b"c:/pkg",
        b"\\\\server\\share\\pkg",
        b"file:///tmp/x.whl",
        b"pkg @ file:///tmp/x.whl",
        b"pkg@file:///tmp/x.whl",
        b"pkg \x07bell",
    ],
)
def test_lines_the_hash_cannot_cover_are_rejected(line: bytes) -> None:
    with pytest.raises(EnvError) as info:
        normalize_requirements(b"numpy\n" + line + b"\n")
    assert info.value.reason == "requirements_rejected"
    assert info.value.failure_type is FailureType.ENV_ERROR


@pytest.mark.parametrize(
    "line",
    [
        b"--index-url https://pypi.org/simple",
        b"--extra-index-url https://example.org/simple",
        b"requests[socks]>=2 ; python_version >= '3.10'",
        b"pkg @ https://example.org/pkg.whl",
    ],
)
def test_ordinary_lines_are_accepted(line: bytes) -> None:
    assert normalize_requirements(line + b"\n") == line + b"\n"


def test_not_utf8_is_rejected() -> None:
    with pytest.raises(EnvError, match="not UTF-8"):
        normalize_requirements(b"numpy\n\xff\xfe\n")


def test_size_bound_max_accepted_and_max_plus_one_rejected() -> None:
    data = b"a" * 99 + b"\n"  # 100 bytes
    assert normalize_requirements(data, max_bytes=100) == data
    with pytest.raises(EnvError) as info:
        normalize_requirements(data + b"\n", max_bytes=100)
    assert info.value.reason == "requirements_rejected"


def test_line_bound_max_accepted_and_max_plus_one_rejected() -> None:
    lines = b"".join(b"pkg%d\n" % i for i in range(10))
    assert normalize_requirements(lines, max_lines=10).count(b"\n") == 10
    with pytest.raises(EnvError, match="more than 9 entries"):
        normalize_requirements(lines, max_lines=9)


# ------------------------------------------------------------- input checks
@pytest.mark.parametrize(
    "over",
    [
        {"runtime_kind": "conda"},
        {"base": ""},
        {"platform": ""},
        {"base": "x" * 257},
        {"python": "3.12\n"},
        {"platform": "linux-é"},
    ],
)
def test_bad_inputs_are_refused(over: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _venv(NORMALISED, **over)


def test_env_error_survives_pickling() -> None:
    err = EnvError("x", reason="disk_full", failure_type=FailureType.DISK_FULL)
    back = pickle.loads(pickle.dumps(err))
    assert back.reason == "disk_full"
    assert back.failure_type is FailureType.DISK_FULL
